"""The bounded tool-use loop, and the envelope a model uses to ask for a tool.

One node invocation may now take several model turns::

    model turn -> optional tool request -> validated tool execution
               -> tool result returned to the same invocation -> next turn
               -> ... -> final structured domain output

It is a loop in one function, not a graph and not a second orchestrator. The
runner still owns control flow *between* nodes; this owns the turns *within* one
node, which is the only place the distinction is needed. It lives in the
workflow layer rather than the model layer because it is node behaviour: the
model boundary stays "one request, one response", and only a node knows what to
do with a turn that is not an answer.

**Why an envelope and not native tool-calling.** A provider's native tool-call
protocol would put a second, provider-specific vocabulary on the model boundary
and would mean the transport, not this module, decided what a tool request is.
The request is therefore a plain JSON object the model writes as its whole
response::

    {"tool_request": {"tool": "search_contract", "arguments": {"query": "..."}}}

which keeps :class:`~pilot01.model.client.ModelRequest` exactly as it was -- two
or three text messages and a parameter set -- and keeps every provider on the
same footing. The same envelope is described, in the same words, in both role
prompts; only the permission block around it differs between conditions.

**Bounded.** ``MAX_TOOL_ROUNDS`` caps the number of tool *requests* one
invocation may make. A model that asks for a tool on every turn is cut off with
a protocol error rather than being allowed to spend indefinitely: a runaway loop
is a failed run, not an experiment.

**A refused tool is data, not a crash.** An unknown tool, a malformed argument
set, an id from another contract -- each becomes a failed tool result the model
reads on its next turn. That is deliberate: whether an agent recovers from a
refusal is part of what the pilot measures, and killing the run would erase the
observation. Only a loop that never stops asking is fatal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ...model import (
    ModelCallLog,
    ModelClient,
    ModelClientError,
    ModelMessage,
    ModelRequest,
    RepairSpec,
    StructuredCallResult,
    call_structured,
    extract_json_object,
    record_call,
)
from ...source.tools import (
    TOOL_ARGUMENTS,
    SourceTools,
    ToolOutcome,
    ToolProtocolError,
    ToolRequest,
)
from ..transitions import ProtocolError

__all__ = [
    "MAX_TOOL_ROUNDS",
    "TOOL_REQUEST_KEY",
    "ToolLoopError",
    "ToolLoopResult",
    "extract_tool_request",
    "render_tool_result",
    "render_tool_surface",
    "run_tool_loop",
]

MAX_TOOL_ROUNDS = 4
"""Tool requests permitted per node invocation. A fifth aborts the run.

Chosen to be comfortably more than a realistic search-open-recheck sequence
needs, and small enough that a model stuck in a request loop cannot spend
without bound. It bounds *tool requests*, not turns: after the last permitted
request the model still gets a turn to answer, which is why the check fires
before dispatch rather than after. What the prompt advertises ("at most
``MAX_TOOL_ROUNDS`` tools") is therefore exactly what the runtime allows.
"""

TOOL_REQUEST_KEY = "tool_request"


class ToolLoopError(ProtocolError):
    """The tool loop could not produce a final answer within its budget.

    A :class:`~pilot01.workflow.transitions.ProtocolError` so the runner treats
    it as what it is -- a structural failure that aborts and is recorded -- and
    not as an observation to be salvaged.
    """


def extract_tool_request(text: str) -> Any | None:
    """Return the ``tool_request`` body if this response is a tool request.

    ``None`` means "not a tool request", which is the signal to treat the
    response as a candidate final answer. A response with no JSON object at all
    is also ``None``: it is a malformed answer, and the structured-output path
    is where malformed answers are diagnosed and repaired.

    A response whose ``tool_request`` is present but not an object is returned
    as it is, so the refusal can say what was wrong and be logged against
    whatever tool name could be salvaged. See :func:`run_tool_loop`.
    """
    try:
        payload = json.loads(extract_json_object(text))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or TOOL_REQUEST_KEY not in payload:
        return None
    return payload[TOOL_REQUEST_KEY]


def render_tool_surface(*, names: Sequence[str], available: bool) -> str:
    """Describe the tool surface a node is being offered.

    ``names`` comes from the tool layer itself and ``available`` from the view's
    own permission flag, so the surface a prompt advertises is derived from the
    same two facts the runtime enforces. Nothing here is written out by hand: a
    prompt cannot advertise a tool that does not exist, omit one that does, or
    disagree with what the tool layer will actually do when asked.

    In A0 the surface is empty, and this says so explicitly rather than staying
    silent -- an agent that is not told it lacks tools is an agent whose
    behaviour under that condition means nothing.
    """
    if not names or not available:
        return "No source tools are available to you in this workflow."
    lines = ["You may use these source tools:"]
    for name in names:
        arguments = ", ".join(TOOL_ARGUMENTS[name])
        lines.append(f"- `{name}({arguments})`")
    lines.extend(
        [
            "",
            "To use one, reply with exactly this JSON object and nothing else:",
            f'{{"{TOOL_REQUEST_KEY}": {{"tool": "<tool name>", '
            '"arguments": {"<argument>": "<value>"}}}}',
            "",
            "You will then receive the tool's result, and may either request "
            "another tool or reply with your final answer. You may request at "
            f"most {MAX_TOOL_ROUNDS} tools.",
        ]
    )
    return "\n".join(lines)


def render_tool_result(outcome: ToolOutcome) -> str:
    """Format a tool outcome for the model's next turn.

    A refusal is rendered as plainly as a success: the model is told what it
    asked for, that it failed, and why. Nothing is softened and nothing is
    added -- in particular, no hint about what it should have asked for
    instead, which would be the experimenter steering the agent.
    """
    lines = [
        "TOOL RESULT",
        f"tool: {outcome.tool}",
        f"status: {'ok' if outcome.ok else 'error'}",
    ]
    if not outcome.ok:
        lines.append(f"error: {outcome.error}")
        return "\n".join(lines)

    if outcome.tool == "search_contract":
        results = outcome.payload.get("results", [])
        lines.append(f"results: {len(results)}")
        if not results:
            lines.append("(no paragraph of this contract matched the query)")
            return "\n".join(lines)
        for hit in results:
            lines.extend(
                [
                    "",
                    f"  paragraph_id: {hit['paragraph_id']}",
                    f"  score: {hit['score']}",
                    f"  excerpt: {hit['excerpt']}",
                ]
            )
        return "\n".join(lines)

    lines.extend(
        [
            f"paragraph_id: {outcome.payload.get('paragraph_id')}",
            f"offsets: {outcome.payload.get('start_char')}.."
            f"{outcome.payload.get('end_char')}",
            "text:",
            str(outcome.payload.get("text", "")),
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class ToolLoopResult:
    """The outcome of one node's turns: the final object plus everything it did."""

    output: BaseModel
    """The validated domain object."""
    records: tuple[Any, ...]
    """Every raw-call record the invocation produced, in order."""
    tool_outcomes: tuple[ToolOutcome, ...]
    """Every tool outcome, in order -- refusals included."""
    rounds: int
    """How many tool requests were executed."""
    repaired: bool
    """Whether the final answer needed the one format repair."""

    @property
    def structured(self) -> StructuredCallResult:
        """The final answer's own call result, for callers that want it."""
        return StructuredCallResult(output=self.output, records=tuple(self.records[-1:]))

    @property
    def final_record(self) -> Any:
        return self.records[-1]


def run_tool_loop(
    *,
    client: ModelClient,
    build_request: Callable[[tuple[ModelMessage, ...]], ModelRequest],
    initial_messages: tuple[ModelMessage, ...],
    output_model: type[Any],
    repair: RepairSpec | None,
    call_log: ModelCallLog | None,
    expected_role: str | None,
    tools: SourceTools | None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    audit: Callable[[ModelRequest], None] | None = None,
) -> ToolLoopResult:
    """Run one node invocation to a final structured answer.

    ``build_request`` is supplied by the node rather than built here, so the
    provenance fields -- role, prompt version, invocation, run id -- come from
    the node's own view and this module never needs to know them. ``audit`` is
    called on each turn's request before it is sent, which is what extends the
    node's hidden-key audit to every turn rather than only the first.

    ``call_log`` is appended to directly here rather than through
    ``call_structured``: this function already owns the sequence of call
    indices, and two writers numbering the same log would be a bug waiting to
    happen.
    """
    messages = list(initial_messages)
    records: list[Any] = []
    outcomes: list[ToolOutcome] = []
    call_index = 0
    rounds = 0

    def emit(record: Any) -> None:
        records.append(record)
        if call_log is not None:
            call_log.append(record)

    while True:
        request = build_request(tuple(messages))
        if audit is not None:
            audit(request)

        try:
            response = client.generate(request)
        except ModelClientError as exc:
            emit(
                record_call(
                    index=call_index,
                    request=request,
                    prompt_version=request.prompt_version,
                    attempt=0,
                    is_repair=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise

        envelope = extract_tool_request(response.text)

        if envelope is None:
            # Not a tool request: this is the answer, or a malformed attempt at
            # one. Hand the response to the structured path, which owns
            # validation and the single format-only repair. It writes into a
            # local sink so its records can be re-emitted here, keeping the
            # numbering of the shared log in this one place -- including on the
            # failure path, where the records that explain the failure are the
            # ones worth keeping.
            sink = ModelCallLog()
            try:
                result = call_structured(
                    client,
                    request,
                    output_model,
                    repair=repair,
                    call_log=sink,
                    expected_role=expected_role,
                    first_index=call_index,
                    first_response=response,
                )
            finally:
                for record in sink.records:
                    emit(record)
            return ToolLoopResult(
                output=result.output,
                records=tuple(records),
                tool_outcomes=tuple(outcomes),
                rounds=rounds,
                repaired=result.repaired,
            )

        # A tool request. Record the turn, then act on it.
        emit(
            record_call(
                index=call_index,
                request=request,
                prompt_version=request.prompt_version,
                attempt=0,
                is_repair=False,
                response=response,
                tool_request=envelope if isinstance(envelope, dict) else {"raw": envelope},
            )
        )
        call_index += 1

        # Checked *before* dispatching, so the limit is the number of tools the
        # model may actually use. Checking after would make the advertised
        # budget unreachable: the surface says "at most N", and a model that
        # used exactly N would be aborted instead of being allowed the turn that
        # answers.
        if rounds >= max_tool_rounds:
            raise ToolLoopError(
                f"{request.role} requested more than {max_tool_rounds} tool(s) "
                f"without producing a final answer; the limit is {max_tool_rounds}. "
                "Aborting rather than letting the loop run on."
            )

        outcome = _dispatch(tools, envelope, call_index=call_index - 1, request=request)
        outcomes.append(outcome)
        rounds += 1

        messages.append(ModelMessage(role="assistant", content=response.text))
        messages.append(ModelMessage(role="user", content=render_tool_result(outcome)))


def _dispatch(
    tools: SourceTools | None,
    envelope: Any,
    *,
    call_index: int,
    request: ModelRequest,
) -> ToolOutcome:
    """Turn an envelope into a tool outcome, refusing anything malformed."""
    if tools is None:
        raise ToolLoopError(
            f"{request.role} requested a tool but this node was given no tool layer"
        )
    try:
        tool_request = ToolRequest.from_payload(envelope)
    except ToolProtocolError as exc:
        # Malformed envelope: log it against whatever tool name could be
        # salvaged, and return the refusal to the model.
        return tools.reject(
            tool=exc.tool,
            arguments=exc.arguments,
            error=str(exc),
            call_index=call_index,
            invocation=request.invocation,
            run_id=request.run_id,
        )
    return tools.execute(
        tool_request,
        call_index=call_index,
        invocation=request.invocation,
        run_id=request.run_id,
    )
