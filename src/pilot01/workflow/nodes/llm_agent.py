"""Shared plumbing for the LLM-backed agent nodes.

This module is **infrastructure only**. It contains no rule about the contract,
the memo, the policy or the decision. Its whole job is the mechanical path

    restricted view -> render messages -> model turns -> structured output

and every step of that path is a pure function of the view plus the versioned
prompt files. There is deliberately no branch anywhere in it that could set a
``clause_status``, choose a ``decision``, or supply a missing field: if the
model does not produce a usable object, the node fails rather than filling the
gap. That is the Gate 1.5 lesson applied to the real agents -- an inference like
"no target claim in the memo means the clause is absent" is the phenomenon under
study, so it cannot be written in Python here.

Five protections are enforced at call time:

1. The view is audited for hidden-shaped keys before anything is rendered.
2. The renderer is handed the view and nothing else, so the request cannot
   depend on state the agent is not permitted to see.
3. Every turn's request -- not merely the first -- is audited for
   hidden-shaped keys before it is sent. This matters once tool results are
   being appended to the message list: the audit is what proves that nothing a
   tool returned introduced a hidden-shaped key into an agent's context.
4. The parsed object must declare this node's own role, so a model cannot
   relabel itself into the other agent's position.
5. Under V1, the verification the model claims is re-derived from the tool log
   before the output is accepted, so ``"verified"`` is a claim the runtime
   checks rather than one it believes.

**Source access is bound per invocation, from the view.** The node reads the
contract id from the state (an agent has no business being handed one) and the
``source_access`` flag from its own view, and binds its tool layer accordingly.
The same node object therefore serves A0 and A1 unchanged, which is what makes
the two conditions comparable: nothing about the node differs between them but
whether its tools answer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from ...model import (
    ModelCallLog,
    ModelClient,
    ModelClientError,
    ModelMessage,
    ModelOutputError,
    ModelParams,
    ModelRequest,
    RepairSpec,
    call_structured,
)
from ...prompts import PromptTemplate
from ...source.tools import SourceTools
from ...source.verification import VerificationLog, evaluate_verification
from ..transitions import Node, NodeResult, ProtocolError
from ..views import RestrictedView, assert_view_clean, audit_keys
from .tool_loop import MAX_TOOL_ROUNDS, run_tool_loop

__all__ = [
    "AgentPromptSpec",
    "make_llm_agent_node",
    "render_output_fields",
    "build_repair_spec",
]

ModelT = TypeVar("ModelT", bound=BaseModel)

_ROLE_FIELD = "role"
"""Filled by the node from the workflow position, never by the model."""


def _resolve(schema: dict, node: dict) -> dict:
    """Resolve a ``$ref`` against the schema's own ``$defs``."""
    while "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        node = schema.get("$defs", {}).get(name, {})
    return node


def _describe(schema: dict, node: dict) -> str:
    node = _resolve(schema, node)
    if "enum" in node:
        return "one of " + ", ".join(f'"{value}"' for value in node["enum"])
    if "anyOf" in node:
        return " or ".join(_describe(schema, option) for option in node["anyOf"])
    kind = node.get("type")
    if kind == "array":
        return f"array of {_describe(schema, node.get('items', {}))}"
    if kind in ("number", "integer"):
        low, high = node.get("minimum"), node.get("maximum")
        if low is not None and high is not None:
            return f"{kind} between {low} and {high}"
        return kind
    if kind == "null":
        return "null"
    return kind or "value"


def render_output_fields(output_model: type[BaseModel]) -> str:
    """Describe an output schema's fields, derived from the schema itself.

    Mechanical: the text is read off ``model_json_schema()``, so the repair
    prompt cannot drift from the model the node actually validates against.
    """
    schema = output_model.model_json_schema()
    lines: list[str] = []
    for name, prop in schema.get("properties", {}).items():
        if name == _ROLE_FIELD:
            continue
        lines.append(f"- `{name}`: {_describe(schema, prop)}")
    return "\n".join(lines)


def build_repair_spec(
    repair_prompt: PromptTemplate, output_model: type[BaseModel]
) -> RepairSpec:
    """Build the one permitted format-repair turn.

    The repair turn receives the model's own previous response and the
    validation error, and nothing else. It is not given the view, the policy,
    the memo, the handoff, or any indication of what the answer should be.
    """
    fields = render_output_fields(output_model)

    def build_messages(previous_response: str, parse_error: str) -> tuple[ModelMessage, ...]:
        return (
            ModelMessage(
                role="user",
                content=repair_prompt.render(
                    output_fields=fields,
                    parse_error=parse_error,
                    previous_response=previous_response,
                ),
            ),
        )

    return RepairSpec(prompt_version=repair_prompt.ref, build_messages=build_messages)


@dataclass(frozen=True)
class AgentPromptSpec:
    """How one agent role turns its restricted view into messages.

    ``build_messages`` is the *only* thing that sees the view, and it must
    depend on nothing but its single argument -- the isolation tests assert that
    signature, so a future edit cannot quietly widen it.
    """

    system_prompt: PromptTemplate
    build_messages: Callable[[Any], tuple[ModelMessage, ...]]


@dataclass
class AgentContext:
    """Per-node state the node needs but an agent must not be handed.

    A separate object rather than a closure over mutable locals, so the tests
    can reach it: the evidence ledger and the verification outcome of the last
    invocation are observations about the run, and an observation that cannot be
    read is not one.

    ``tools`` is per node, never shared. That is what keeps the Manager's
    evidence access out of Compliance's ledger.
    """

    tools: SourceTools | None = None
    verification_log: VerificationLog | None = None
    run_id: str | None = None
    contract_id: str | None = None
    last_verification: Any = None
    last_loop: Any = None
    seed_upstream: Callable[[Any], Iterable[str]] = field(default=lambda view: ())


def make_llm_agent_node(
    *,
    name: str,
    role: str,
    view_model: type[RestrictedView],
    build_view: Callable[[Any, int], RestrictedView],
    prompt: AgentPromptSpec,
    repair_prompt: PromptTemplate,
    output_model: type[ModelT],
    params: ModelParams,
    client: ModelClient,
    call_log: ModelCallLog,
    apply: Callable[[Any, Any], None],
    tools: SourceTools | None = None,
    verification_log: VerificationLog | None = None,
    seed_upstream: Callable[[Any], Iterable[str]] | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[Node, AgentContext]:
    """Build an LLM-backed agent node, and the context that observes it.

    ``call_log`` is required rather than optional: a live call that is not
    recorded is an unreproducible observation, so the wiring makes it impossible
    to run one without a log.

    Returns the node *and* an :class:`AgentContext`. The node is a frozen
    dataclass with a fixed field set -- the runner's contract with it -- so the
    things a test needs to inspect afterwards (the ledger, the verification
    verdict) live beside it rather than being smuggled onto it.
    """
    repair_spec = build_repair_spec(repair_prompt, output_model)
    context = AgentContext(
        tools=tools,
        verification_log=verification_log,
        seed_upstream=seed_upstream if seed_upstream is not None else (lambda view: ()),
    )

    def build_input(state: Any, invocation: int) -> RestrictedView:
        # Read from the state here, where the state is legitimately available,
        # and never put on the view: an agent does not need to be told which
        # contract it is looking at, because its tools are already scoped to it.
        context.run_id = state.run_id
        context.contract_id = state.contract_id
        return build_view(state, invocation=invocation)

    def build_request(messages: tuple[ModelMessage, ...], view: RestrictedView) -> ModelRequest:
        return ModelRequest(
            messages=messages,
            params=params,
            role=role,
            prompt_version=prompt.system_prompt.ref,
            invocation=view.invocation,
            run_id=context.run_id,
        )

    def audit_request(request: ModelRequest) -> None:
        violations = audit_keys(request.model_dump(mode="json"))
        if violations:
            raise ProtocolError(
                f"{name} model request would carry hidden-shaped data at: "
                f"{'; '.join(violations)}"
            )

    def run(view: RestrictedView) -> NodeResult:
        if not isinstance(view, view_model):
            raise ProtocolError(
                f"{name} node received {type(view).__name__}; expected "
                f"{view_model.__name__}"
            )
        assert_view_clean(view)

        # Bind the tool layer to this invocation. The availability flag comes
        # from the view -- the agent's own permission -- so what the model is
        # told and what the tools will do cannot disagree.
        if context.tools is not None:
            context.tools.begin_invocation(
                contract_id=context.contract_id,
                available=view.source_access,
                invocation=view.invocation,
                run_id=context.run_id,
            )
            context.tools.ledger.record_upstream(context.seed_upstream(view))

        try:
            loop = run_tool_loop(
                client=client,
                build_request=lambda messages: build_request(messages, view),
                initial_messages=prompt.build_messages(view),
                output_model=output_model,
                repair=repair_spec,
                call_log=call_log,
                expected_role=role,
                tools=context.tools,
                max_tool_rounds=max_tool_rounds,
                audit=audit_request,
            )
        except ModelClientError as exc:
            raise ProtocolError(f"{name} model call failed: {exc}") from exc
        except ModelOutputError as exc:
            raise ProtocolError(
                f"{name} produced no usable {output_model.__name__}: {exc}"
            ) from exc

        context.last_loop = loop
        outcome = _verify(context, view, loop.output)
        context.last_verification = outcome
        if outcome is not None and not outcome.satisfied:
            raise ProtocolError(
                f"{name} did not meet the verification requirement of this "
                f"condition: {outcome.describe()}"
            )
        return NodeResult(output=loop.output)

    node = Node(
        name=name,
        build_input=build_input,
        run=run,
        apply=apply,
        requires_restricted_view=True,
        output_model=output_model,
    )
    return node, context


def _verify(context: AgentContext, view: RestrictedView, output: BaseModel) -> Any:
    """Re-derive the verification verdict from what the node actually did.

    Returns ``None`` when the node has no tool layer at all, which is the
    honest answer: with no source layer wired there is nothing to derive a
    verdict from, and inventing one would be exactly the fake verification path
    the degenerate A0V1 cell must not get.
    """
    if context.tools is None:
        return None
    outcome = evaluate_verification(
        output=output,
        policy=view.policy,
        ledger=context.tools.ledger,
        document=context.tools.document,
        verification_required=view.verification_required,
        source_tools_available=context.tools.available,
        node=context.tools.node,
        invocation=view.invocation,
    )
    if context.verification_log is not None:
        context.verification_log.append(outcome)
    return outcome
