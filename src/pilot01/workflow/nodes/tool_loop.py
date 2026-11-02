"""The bounded tool-use loop for native and frozen legacy tool protocols.

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

Frozen experiments retain their historical JSON text envelope::

    {"tool_request": {"tool": "search_contract", "arguments": {"query": "..."}}}

New experiments use provider-native structured tool calls normalized by the
model adapter.  Both paths converge on the same validated :class:`ToolRequest`
and the same tool execution/logging layer; the compatibility path is explicit
rather than silently inferred.

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
    ModelOutputError,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    RepairSpec,
    StructuredCallResult,
    call_structured,
    estimate_model_request_tokens,
    extract_json_object,
    prepare_model_request,
    record_call,
)
from ...model.tool_protocol import ToolTransport
from ...source.tools import (
    TOOL_ARGUMENTS,
    SourceTools,
    ToolOutcome,
    ToolProtocolError,
    ToolRequest,
)
from ..transitions import ProtocolError
from .agent_protocol import (
    FinalSubmissionDecision,
    ToolCallDecision,
    ToolProtocolViolation,
    decide_agent_response,
)

__all__ = [
    "MAX_TOOL_ROUNDS",
    "TOOL_REQUEST_KEY",
    "ToolLoopError",
    "ToolLoopResult",
    "extract_tool_request",
    "render_tool_result",
    "tool_result_payload",
    "render_tool_surface",
    "run_tool_loop",
]

MAX_TOOL_ROUNDS = 16
"""Tool requests permitted per node invocation. A seventeenth aborts the run.

Raised from four for the separately frozen 2026-10-06 budget experiment;
historical experiments retain their recorded four-request budget. Search and
open share this ceiling, including failed requests. It bounds *tool requests*,
not turns: after the last permitted
request the model still gets a turn to answer, which is why the check fires
before dispatch rather than after. What the prompt advertises ("at most
``MAX_TOOL_ROUNDS`` tools") is therefore exactly what the runtime allows.
"""

TOOL_REQUEST_KEY = "tool_request"
NO_TOOL_REQUEST = object()
"""Distinguish an absent request from an explicitly malformed null request."""

SEARCH_TOOLS = frozenset({"search_contract", "next_search_page"})


@dataclass
class _HistorySegment:
    """One complete assistant-tool exchange kept or compacted atomically."""

    messages: tuple[ModelMessage, ...]
    outcome: ToolOutcome
    used: int
    new_result_ids: tuple[str, ...]
    compacted: bool = False
    exposed: bool = False

    @property
    def is_search(self) -> bool:
        return self.outcome.tool in SEARCH_TOOLS

    @property
    def added_no_information(self) -> bool:
        return self.is_search and (not self.outcome.ok or not self.new_result_ids)


def _ledger_lines(segments: Sequence[_HistorySegment]) -> list[str]:
    lines = [
        "RUNTIME RETRIEVAL LEDGER",
        "Older complete tool-call/result pairs were removed only from model context; "
        "the append-only experiment logs remain unchanged.",
    ]
    for segment in segments:
        outcome = segment.outcome
        query = outcome.payload.get("query") or outcome.arguments.get("query")
        target = outcome.payload.get("target_category") or outcome.arguments.get("target_category")
        label = f"#{segment.used} {outcome.tool}"
        if query:
            label += f" query={json.dumps(str(query), ensure_ascii=False)}"
        if target:
            label += f" target={target}"
        if not outcome.ok:
            detail = str(outcome.error or "unknown error").replace("\n", " ")[:240]
            lines.append(f"- {label}: error; {detail}")
        elif not outcome.result_ids:
            lines.append(f"- {label}: no paragraph ids returned")
        elif not segment.new_result_ids:
            lines.append(
                f"- {label}: no new paragraph ids; repeated={','.join(outcome.result_ids)}"
            )
        else:
            lines.append(
                f"- {label}: new candidate ids={','.join(segment.new_result_ids)}; "
                f"all returned ids={','.join(outcome.result_ids)}"
            )
    return lines


def _history_messages(
    initial_messages: tuple[ModelMessage, ...],
    history: Sequence[_HistorySegment],
    *,
    rounds: int,
    max_tool_rounds: int,
    search_disabled: bool,
) -> tuple[ModelMessage, ...]:
    """Render history in order, replacing compacted runs with short ledgers."""
    rendered = list(initial_messages)
    pending: list[_HistorySegment] = []

    def flush() -> None:
        if not pending:
            return
        lines = _ledger_lines(pending)
        lines.append(
            f"Tool requests used: {rounds}/{max_tool_rounds}; "
            f"remaining: {max_tool_rounds - rounds}."
        )
        rendered.append(ModelMessage(role="user", content="\n".join(lines)))
        pending.clear()

    for segment in history:
        if segment.compacted:
            pending.append(segment)
        else:
            flush()
            rendered.extend(segment.messages)
    flush()
    if search_disabled:
        rendered.append(
            ModelMessage(
                role="user",
                content=(
                    "RUNTIME SEARCH STOP: recent searches added no new paragraph ids. "
                    "Search and pagination are no longer available in this invocation. "
                    "Open an already eligible paragraph id or submit the final answer; "
                    "mark unresolved targets unknown."
                ),
            )
        )
    return tuple(rendered)


class ToolLoopError(ProtocolError):
    """The tool loop could not produce a final answer within its budget.

    A :class:`~pilot01.workflow.transitions.ProtocolError` so the runner treats
    it as what it is -- a structural failure that aborts and is recorded -- and
    not as an observation to be salvaged.
    """


def extract_tool_request(text: str) -> Any | None:
    """Return the ``tool_request`` body if this response is a tool request.

    ``NO_TOOL_REQUEST`` means "not a tool request", which is the signal to treat the
    response as a candidate final answer. A response with no JSON object at all
    is also ``NO_TOOL_REQUEST``: it is a malformed answer, and the structured-output path
    is where malformed answers are diagnosed and repaired.

    A response whose ``tool_request`` is present but not an object is returned
    as it is, so the refusal can say what was wrong and be logged against
    whatever tool name could be salvaged. See :func:`run_tool_loop`.
    """
    try:
        payload = json.loads(extract_json_object(text))
    except (ValueError, json.JSONDecodeError):
        return NO_TOOL_REQUEST
    if not isinstance(payload, dict) or TOOL_REQUEST_KEY not in payload:
        return NO_TOOL_REQUEST
    return payload[TOOL_REQUEST_KEY]


def render_tool_surface(*, names: Sequence[str], available: bool, max_tool_rounds: int = MAX_TOOL_ROUNDS, protocol_version: str = "1") -> str:
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
        if protocol_version == "2" and name == "search_contract":
            arguments = "query, target_category, cursor (optional)"
        elif protocol_version in {"native-v1", "native-v2"} and name == "search_contract":
            arguments = "query, target_category"
        elif protocol_version in {"native-v1", "native-v2"} and name == "next_search_page":
            arguments = "cursor"
        else:
            arguments = ", ".join(TOOL_ARGUMENTS[name])
        lines.append(f"- `{name}({arguments})`")
    if protocol_version in {"native-v1", "native-v2"}:
        lines.extend(
            [
                "",
                "Use the provided native tool interface for a tool call; do not write a `tool_request` JSON envelope in normal content.",
                "A tool call and a final answer are mutually exclusive. When ready to submit, return the final answer fields directly without a `final_answer` wrapper.",
                (
                    f"You may request at most {max_tool_rounds} tools. Each assistant response may request exactly one tool."
                    if protocol_version == "native-v2"
                    else f"You may request at most {max_tool_rounds} tools."
                ),
                "Search returns five ranked snippets by default. Continue only with next_search_page(next_cursor); the cursor already binds the original query and target category.",
                *(
                    [
                        "In native-v2, a continuation cursor is single-use and next_search_page is callable only while the native tool interface exposes it.",
                        "The listed tools describe capabilities; on each response, call only the exact schemas currently exposed by the native interface.",
                        "In native-v2, open_source_span appears only after search has returned an eligible paragraph id, and it may open only IDs enumerated by the current native schema.",
                        "Search is broad lexical recall with an exact-phrase ranking bonus, not a strict phrase filter. Quotation marks do not require an exact match, and scores are query-local ranking signals rather than probabilities.",
                    ]
                    if protocol_version == "native-v2" else []
                ),
                "Search results expose verbatim contract excerpts. For formal full-paragraph reading and verification provenance, use open_source_span(paragraph_id) on a valid paragraph in the current authorized contract.",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "To use one, reply with exactly this JSON object and nothing else:",
            f'{{"{TOOL_REQUEST_KEY}": {{"tool": "<tool name>", '
            '"arguments": {"<argument>": "<value>"}}}',
            "",
            "You will then receive the tool's result, and may either request "
            "another tool or reply with your final answer. You may request at "
            f"most {max_tool_rounds} tools.",
        ]
    )
    if protocol_version == "2":
        lines.extend([
            "Search returns five ranked snippets by default. Use the returned next_cursor with the same query and target_category to read the next page.",
            "One request names one policy target. A snippet is not a full source opening. Repeating the first-page query returns a notice, not new passages.",
            "If several candidates are irrelevant, consider a more specific reformulation rather than repeatedly requesting the same page. Empty results do not establish absence.",
            "Search, paging, open and failed tool requests all consume the shared allowance. Open with open_source_span(paragraph_id).",
        ])
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

    if outcome.tool in {"search_contract", "next_search_page"}:
        # Protocol v2 pagination metadata is part of the tool result, including
        # empty/repeated pages. Preserve it before any early return.
        if "has_more" in outcome.payload:
            lines.append(json.dumps(outcome.payload, ensure_ascii=False))
            return "\n".join(lines)
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


def tool_result_payload(
    outcome: ToolOutcome,
    *,
    used: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Machine-readable tool result returned through a native tool message."""
    payload: dict[str, Any] = {
        "tool": outcome.tool,
        "status": "ok" if outcome.ok else "error",
    }
    if outcome.ok:
        payload["data"] = outcome.payload
    else:
        payload["error"] = outcome.error
    if used is not None or limit is not None:
        if used is None or limit is None:
            raise ValueError("native tool-budget telemetry needs both used and limit")
        payload["tool_budget"] = {
            "used": used,
            "limit": limit,
            "remaining": limit - used,
        }
    return payload


def _assert_native_call_is_offered(call: ModelToolCall, request: ModelRequest) -> None:
    """Fail closed if a strict native call is outside this turn's schema.

    Providers normally enforce function schemas, but the workflow also accepts
    normalized responses from adapters and scripted clients.  Rechecking the
    small closed schema subset here makes the state machine authoritative in
    every execution path.
    """
    definition = next((tool for tool in request.tools if tool.name == call.name), None)
    if definition is None:
        raise ToolProtocolViolation(f"tool {call.name!r} was not offered on this turn")
    schema = definition.input_schema
    properties = schema.get("properties", {})
    required = set(schema.get("required", ()))
    keys = set(call.arguments)
    if keys != required or (schema.get("additionalProperties") is False and keys - set(properties)):
        raise ToolProtocolViolation(
            f"tool {call.name!r} arguments do not match the offered closed schema"
        )
    for name, value in call.arguments.items():
        rule = properties[name]
        if rule.get("type") == "string" and not isinstance(value, str):
            raise ToolProtocolViolation(f"tool {call.name!r}.{name} must be a string")
        if "enum" in rule and value not in rule["enum"]:
            raise ToolProtocolViolation(
                f"tool {call.name!r}.{name} is not one of the currently offered values"
            )


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
    compacted_tool_turns: int = 0
    """Tool exchanges removed from model context but retained in raw logs."""
    search_disabled: bool = False
    """Whether the deterministic no-new-information stop rule fired."""

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
    tool_transport: ToolTransport = "legacy-text",
    strict_native_state_machine: bool = False,
    native_output_format: str | None = None,
    tool_turn_max_output_tokens: int | None = None,
    final_max_output_tokens: int | None = None,
    repair_max_output_tokens: int | None = None,
    context_compaction_version: str | None = None,
    context_compaction_trigger_tokens: int | None = None,
    context_compaction_target_tokens: int | None = None,
    search_disable_after_no_new: int | None = None,
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
    if native_output_format is not None:
        if native_output_format != "closed-final-or-tool-v1" or not strict_native_state_machine or tool_transport != "native":
            raise ValueError("closed output constraints require strict native-v2")
        from ...model.native_constraints import schema_without_metadata
        final_output_schema = schema_without_metadata(output_model.model_json_schema())
    else:
        final_output_schema = None
    if context_compaction_version not in (None, "retrieval-ledger-v1"):
        raise ValueError("unknown context compaction version")
    if context_compaction_version is not None:
        if not strict_native_state_machine or tool_transport != "native":
            raise ValueError("retrieval compaction requires the strict native state machine")
        if (context_compaction_trigger_tokens is None
                or context_compaction_target_tokens is None
                or search_disable_after_no_new is None):
            raise ValueError("retrieval compaction policy is incomplete")
        if context_compaction_target_tokens >= context_compaction_trigger_tokens:
            raise ValueError("context compaction target must be below its trigger")
    records: list[Any] = []
    outcomes: list[ToolOutcome] = []
    history: list[_HistorySegment] = []
    call_index = 0
    rounds = 0
    seen_tool_call_ids: set[str] = set()
    seen_result_ids: set[str] = set()
    consecutive_no_new_searches = 0
    search_disabled = False

    def emit(record: Any) -> None:
        records.append(record)
        if call_log is not None:
            call_log.append(record)

    def observe_outcome(outcome: ToolOutcome) -> tuple[str, ...]:
        nonlocal consecutive_no_new_searches, search_disabled
        new_ids = tuple(pid for pid in outcome.result_ids if pid not in seen_result_ids)
        seen_result_ids.update(outcome.result_ids)
        if outcome.tool in SEARCH_TOOLS:
            if not outcome.ok or not new_ids:
                consecutive_no_new_searches += 1
            else:
                consecutive_no_new_searches = 0
            if (search_disable_after_no_new is not None
                    and consecutive_no_new_searches >= search_disable_after_no_new):
                search_disabled = True
        return new_ids

    def make_request(*, final_only: bool) -> ModelRequest:
        request_messages = _history_messages(
            initial_messages,
            history,
            rounds=rounds,
            max_tool_rounds=max_tool_rounds,
            search_disabled=search_disabled,
        )
        if final_only:
            request_messages += (
                ModelMessage(
                    role="user",
                    content=(
                        "The source-tool budget is exhausted. No more tools are available. "
                        "Submit the final answer now using only evidence already returned; "
                        "mark any unresolved policy target as unknown."
                    ),
                ),
            )
        request = build_request(request_messages)
        if native_output_format is not None:
            request = request.model_copy(update={"native_output_format": native_output_format,
                                                 "final_output_schema": final_output_schema})
        if strict_native_state_machine and not final_only:
            if tool_transport != "native":
                raise ValueError("strict native state machine requires native tool transport")
            definitions = tools.tool_definitions if tools is not None and tools.available else ()
            if search_disabled:
                definitions = tuple(
                    definition for definition in definitions
                    if definition.name not in SEARCH_TOOLS
                )
            request = request.model_copy(
                update={
                    "tools": definitions,
                    "tool_choice": "auto" if definitions else None,
                    "parallel_tool_calls": False if definitions else None,
                }
            )
        if final_only:
            request = request.model_copy(
                update={"tools": (), "tool_choice": None, "parallel_tool_calls": None}
            )
        ceiling = final_max_output_tokens if final_only else tool_turn_max_output_tokens
        if ceiling is not None and request.params.max_output_tokens > ceiling:
            request = request.model_copy(update={"params": request.params.model_copy(
                update={"max_output_tokens": ceiling})})
        return request

    while True:
        final_only = tool_transport == "native" and rounds >= max_tool_rounds
        request = make_request(final_only=final_only)
        try:
            if context_compaction_version is not None:
                estimated = estimate_model_request_tokens(client, request)
                if estimated is None:
                    raise ModelClientError(
                        "retrieval-ledger-v1 requires a local request token estimator"
                    )
                if estimated > context_compaction_trigger_tokens:
                    for segment in history:
                        if (segment.is_search and not segment.compacted
                                and (segment.exposed or segment.added_no_information)):
                            segment.compacted = True
                            request = make_request(final_only=final_only)
                            estimated = estimate_model_request_tokens(client, request)
                            if estimated <= context_compaction_target_tokens:
                                break
            if audit is not None:
                audit(request)
            request = prepare_model_request(client, request)
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
                    response=getattr(exc, "partial_response", None),
                )
            )
            raise

        for segment in history:
            if not segment.compacted:
                segment.exposed = True

        # A truncated tool-shaped response is not a valid instruction to dispatch.
        if getattr(response, "finish_reason", None) == "length":
            emit(record_call(index=call_index, request=request, prompt_version=request.prompt_version,
                             attempt=0, is_repair=False, response=response, parse_error="output_truncated"))
            usage = response.usage
            raise ModelOutputError(
                "output limit (finish_reason='length'; "
                f"requested_max_output_tokens={request.params.max_output_tokens}, "
                f"actual_prompt_tokens={usage.prompt_tokens if usage else None}, "
                f"actual_completion_tokens={usage.completion_tokens if usage else None}); "
                "the provider's effective limit may differ from the requested maximum; no repair attempted"
            )
        native_calls: tuple[ModelToolCall, ...] = ()
        if tool_transport == "native":
            try:
                decision = decide_agent_response(response, transport=tool_transport)
                if isinstance(decision, ToolCallDecision):
                    if strict_native_state_machine and len(decision.calls) != 1:
                        raise ToolProtocolViolation(
                            "native-v2 permits exactly one tool call per assistant response; "
                            f"received {len(decision.calls)}"
                        )
                    if strict_native_state_machine:
                        _assert_native_call_is_offered(decision.calls[0], request)
                    response_ids = [call.call_id for call in decision.calls]
                    duplicate_ids = {
                        call_id
                        for call_id in response_ids
                        if response_ids.count(call_id) > 1
                        or call_id in seen_tool_call_ids
                    }
                    if duplicate_ids:
                        raise ToolProtocolViolation(
                            "duplicate native tool call id(s): "
                            + ", ".join(sorted(duplicate_ids))
                        )
            except ToolProtocolViolation as exc:
                emit(
                    record_call(
                        index=call_index,
                        request=request,
                        prompt_version=request.prompt_version,
                        attempt=0,
                        is_repair=False,
                        response=response,
                        parse_error=f"tool_protocol_violation: {exc}",
                    )
                )
                raise ToolLoopError(str(exc)) from exc
            if isinstance(decision, FinalSubmissionDecision):
                envelope = NO_TOOL_REQUEST
            else:
                native_calls = decision.calls
                seen_tool_call_ids.update(call.call_id for call in native_calls)
                envelope = [
                    {"tool": call.name, "arguments": call.arguments}
                    for call in native_calls
                ]
        else:
            envelope = extract_tool_request(response.text)

        if envelope is NO_TOOL_REQUEST:
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
                    repair_max_output_tokens=repair_max_output_tokens,
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
                compacted_tool_turns=sum(segment.compacted for segment in history),
                search_disabled=search_disabled,
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
                tool_request=(
                    (
                        {
                            "call_id": native_calls[0].call_id,
                            "call_id_origin": native_calls[0].call_id_origin,
                            "tool": native_calls[0].name,
                            "arguments": native_calls[0].arguments,
                        }
                        if len(native_calls) == 1
                        else {
                            "calls": [
                                {
                                    "call_id": call.call_id,
                                    "call_id_origin": call.call_id_origin,
                                    "tool": call.name,
                                    "arguments": call.arguments,
                                }
                                for call in native_calls
                            ]
                        }
                    )
                    if native_calls
                    else envelope if isinstance(envelope, dict) else {"raw": envelope}
                ),
                tool_call_id=native_calls[0].call_id if len(native_calls) == 1 else None,
            )
        )
        call_index += 1

        # Checked *before* dispatching, so the limit is the number of tools the
        # model may actually use. Checking after would make the advertised
        # budget unreachable: the surface says "at most N", and a model that
        # used exactly N would be aborted instead of being allowed the turn that
        # answers.
        requested_count = len(native_calls) if native_calls else 1
        if rounds + requested_count > max_tool_rounds:
            raise ToolLoopError(
                f"{request.role} requested {requested_count} tool(s) with only "
                f"{max_tool_rounds - rounds} remaining in its {max_tool_rounds}-tool budget "
                f"without producing a final answer; the limit is {max_tool_rounds}. "
                "No tool from this response was executed."
            )

        if native_calls:
            turn_outcomes = []
            for native_call, native_envelope in zip(native_calls, envelope, strict=True):
                outcome = _dispatch(
                    tools,
                    native_envelope,
                    call_index=call_index - 1,
                    request=request,
                    tool_call_id=native_call.call_id,
                )
                outcomes.append(outcome)
                rounds += 1
                turn_outcomes.append((outcome, rounds, observe_outcome(outcome)))
            turn_messages = [ModelMessage(role="assistant", tool_calls=native_calls)]
            for native_call, (outcome, used, _new_ids) in zip(
                native_calls, turn_outcomes, strict=True
            ):
                turn_messages.append(
                    ModelMessage(
                        role="tool",
                        name=native_call.name,
                        tool_call_id=native_call.call_id,
                        content=json.dumps(
                            tool_result_payload(
                                outcome,
                                used=used,
                                limit=max_tool_rounds,
                            ),
                            ensure_ascii=False,
                        ),
                    )
                )
            primary, used, new_ids = turn_outcomes[0]
            segment = _HistorySegment(
                messages=tuple(turn_messages), outcome=primary, used=used,
                new_result_ids=new_ids,
            )
            if context_compaction_version is not None and segment.added_no_information:
                segment.compacted = True
            history.append(segment)
        else:
            outcome = _dispatch(
                tools,
                envelope,
                call_index=call_index - 1,
                request=request,
            )
            outcomes.append(outcome)
            rounds += 1
            new_ids = observe_outcome(outcome)
            rendered = render_tool_result(outcome)
            if getattr(tools, "protocol_version", "1") == "2":
                rendered += f"\nTool requests used: {rounds}/{max_tool_rounds}; remaining: {max_tool_rounds - rounds}."
            history.append(_HistorySegment(
                messages=(
                    ModelMessage(role="assistant", content=response.text),
                    ModelMessage(role="user", content=rendered),
                ),
                outcome=outcome,
                used=rounds,
                new_result_ids=new_ids,
            ))


def _dispatch(
    tools: SourceTools | None,
    envelope: Any,
    *,
    call_index: int,
    request: ModelRequest,
    tool_call_id: str | None = None,
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
            parent_model_call_id=request.model_call_id,
            tool_call_id=tool_call_id,
        )
    return tools.execute(
        tool_request,
        call_index=call_index,
        invocation=request.invocation,
        run_id=request.run_id,
        parent_model_call_id=request.model_call_id,
        tool_call_id=tool_call_id,
    )
