"""The Compliance node: a fixture double, and a real model-backed agent.

:class:`FakeCompliance` is a **pipeline fixture**, like
:class:`~pilot01.workflow.nodes.manager.FakeManager`. Compliance sees **only**
the Manager handoff: not the analyst memo, not the Manager's internal history,
not any prior message history, and no treatment labels. It replays the
:class:`~pilot01.schemas.ComplianceOutput` a test declared and infers nothing.

In particular it does **not** decide whether to use the source tools. An earlier
revision relayed the Manager's assessment when verification was not required and
reported ``unverifiable`` when it was, which fixed A1V1's outcome by
construction. Whether an agent that *may* consult the contract actually does,
and what it concludes, is a measurement, not a fixture.

:func:`make_llm_compliance_node` is the real agent. Its permitted input is
exactly the field set of :class:`~pilot01.workflow.views.ComplianceInput`: the
case id, the policy, the Manager's handoff, and the two governance flags. It
does **not** receive the analyst memo merely because the runner still holds it
in ``ExperimentState`` -- :func:`~pilot01.workflow.views.build_compliance_view`
never reads that field, so there is no code path by which it could arrive. It
also never sees hidden gold, the expected decision, the treatment label, the
Manager's prompt or raw response, or any conversation history. Each invocation
builds a fresh message list.
"""

from __future__ import annotations

from ...model import ModelCallLog, ModelClient, ModelMessage, ModelParams
from ...prompts import load_compliance_prompt, load_repair_prompt
from ...schemas import ComplianceOutput
from ...source.tools import SOURCE_TOOL_NAMES, SourceTools
from ...source.verification import VerificationLog, VerificationOutcome
from ..transitions import Node, NodeResult, ProtocolError
from ..views import ComplianceInput, build_compliance_view
from .llm_agent import AgentContext, AgentPromptSpec, make_llm_agent_node
from .render import (
    render_governance_block,
    render_manager_handoff_block,
    render_policy_block,
)
from .script import FakeAgentScript
from .tool_loop import MAX_TOOL_ROUNDS, render_tool_surface

__all__ = [
    "VerificationOutcome",
    "FakeCompliance",
    "make_compliance_node",
    "build_compliance_messages",
    "compliance_upstream_ids",
    "make_llm_compliance_node",
]


class FakeCompliance:
    """Fixture-controlled stand-in for the Compliance agent."""

    def __init__(self, script: FakeAgentScript) -> None:
        self._script = script

    @property
    def script(self) -> FakeAgentScript:
        return self._script

    def run(self, view: ComplianceInput) -> NodeResult:
        if not isinstance(view, ComplianceInput):
            raise ProtocolError(
                f"compliance node received {type(view).__name__}; expected ComplianceInput"
            )
        output = self._script.at(
            view.invocation, expected=ComplianceOutput, node="compliance"
        )
        return NodeResult(output=output)


def make_compliance_node(script: FakeAgentScript) -> Node:
    """Build the Compliance node around a fixture-declared script."""

    def apply(state, output) -> None:
        state.compliance_output = output

    return Node(
        name="compliance",
        build_input=lambda state, invocation: build_compliance_view(
            state, invocation=invocation
        ),
        run=FakeCompliance(script=script).run,
        apply=apply,
        requires_restricted_view=True,
        output_model=ComplianceOutput,
    )


# --------------------------------------------------------------------------
# The real, model-backed Compliance
# --------------------------------------------------------------------------


def build_compliance_messages(view: ComplianceInput) -> tuple[ModelMessage, ...]:
    """Render Compliance's permitted input into one fresh message list.

    Takes a :class:`ComplianceInput` and nothing else. The memo is not merely
    omitted from the rendered text -- it is not reachable from this function's
    only argument, because ``ComplianceInput`` has no such field. Nor is the
    Manager's tool history: Compliance is told what the handoff cites, and
    nothing about what the Manager opened.

    As in the Manager's renderer, the advertised tool surface is derived from
    ``view.source_access`` rather than passed in, so there is no argument by
    which a caller could advertise a tool this run does not provide.
    """
    system = load_compliance_prompt().render(policy=render_policy_block(view.policy))
    user = "\n\n".join(
        [
            f"CASE: {view.case_id}",
            render_manager_handoff_block(view.manager_output),
            render_governance_block(
                source_access=view.source_access,
                verification_required=view.verification_required,
                tool_surface=render_tool_surface(
                    names=SOURCE_TOOL_NAMES, available=view.source_access
                ),
            ),
        ]
    )
    return (
        ModelMessage(role="system", content=system),
        ModelMessage(role="user", content=user),
    )


def compliance_upstream_ids(view: ComplianceInput) -> tuple[str, ...]:
    """The ids Compliance was *given*, rather than gathered.

    Exactly the handoff's own ``evidence_ids``. Compliance may legitimately cite
    them -- they are what the Manager handed over -- but citing one is not
    verification, and the ledger keeps it in a different class from a paragraph
    Compliance opened itself. This is the whole of the UPSTREAM-CITED /
    SELF-VERIFIED distinction, expressed as one function with one source.
    """
    return tuple(view.manager_output.evidence_ids)


def make_llm_compliance_node(
    *,
    client: ModelClient,
    params: ModelParams,
    call_log: ModelCallLog,
    tools: SourceTools | None = None,
    verification_log: VerificationLog | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[Node, AgentContext]:
    """Build the real Compliance node, and the context that observes it.

    ``client`` is passed in explicitly, and it is a separate object from the
    Manager's: each node owns its transport, and nothing is shared between them
    but the log. ``tools`` is separate for the same reason -- see
    :func:`~pilot01.workflow.nodes.manager.make_llm_manager_node`.
    """

    def apply(state, output) -> None:
        state.compliance_output = output

    return make_llm_agent_node(
        name="compliance",
        role="compliance",
        view_model=ComplianceInput,
        build_view=build_compliance_view,
        prompt=AgentPromptSpec(
            system_prompt=load_compliance_prompt(),
            build_messages=build_compliance_messages,
        ),
        repair_prompt=load_repair_prompt(),
        output_model=ComplianceOutput,
        params=params,
        client=client,
        call_log=call_log,
        apply=apply,
        tools=tools,
        verification_log=verification_log,
        seed_upstream=compliance_upstream_ids,
        max_tool_rounds=max_tool_rounds,
    )

