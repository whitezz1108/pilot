"""The Manager node: a fixture double, and a real model-backed agent.

Two implementations share the name and nothing else.

:class:`FakeManager` is a **pipeline fixture**. It is not a simulation of legal
reasoning and must not be read as one. It replays the
:class:`~pilot01.schemas.ManagerOutput` a test declared and infers nothing: not
from the analyst memo, not from the policy, not from the governance flags.

In particular it does **not** decide what a missing target claim means. An
earlier revision read "no claim in the target categories" as ``absent``, which
made an upstream omission propagate to an ACCEPT downstream. That inference is
exactly the phenomenon under study, so it cannot live in production code: a real
Manager might read the gap as ``absent``, as ``unknown``, or notice it and go
looking. Which one it does is measured, not assumed.

:func:`make_llm_manager_node` is the real agent. It is infrastructure only --
restricted view, rendered messages, one model call, structured output -- and
contains no rule that could set a status or a decision. The Manager's permitted
inputs are exactly the fields of :class:`~pilot01.workflow.views.ManagerInput`:
the case id, the policy, the frozen memo, and the two governance flags. It never
sees the contract, the hidden gold, the expected decision, the treatment label,
or anything Compliance did. Each invocation builds its messages from scratch, so
there is no conversation history and no state carried between calls.
"""

from __future__ import annotations

from ...model import ModelCallLog, ModelClient, ModelMessage, ModelParams
from ...prompts import load_manager_prompt, load_repair_prompt
from ...schemas import ManagerOutput
from ...source.tools import SOURCE_TOOL_NAMES, SourceTools
from ...source.verification import VerificationLog
from ..transitions import Node, NodeResult, ProtocolError
from ..views import ManagerInput, build_manager_view
from .llm_agent import AgentContext, AgentPromptSpec, make_llm_agent_node
from .render import render_governance_block, render_memo_block, render_policy_block
from .script import FakeAgentScript
from .tool_loop import MAX_TOOL_ROUNDS, render_tool_surface

__all__ = [
    "FakeManager",
    "make_manager_node",
    "build_manager_messages",
    "manager_upstream_ids",
    "make_llm_manager_node",
]


class FakeManager:
    """Fixture-controlled stand-in for the Manager agent."""

    def __init__(self, script: FakeAgentScript) -> None:
        self._script = script

    @property
    def script(self) -> FakeAgentScript:
        return self._script

    def run(self, view: ManagerInput) -> NodeResult:
        if not isinstance(view, ManagerInput):
            raise ProtocolError(
                f"manager node received {type(view).__name__}; expected ManagerInput"
            )
        output = self._script.at(view.invocation, expected=ManagerOutput, node="manager")
        return NodeResult(output=output)


def make_manager_node(script: FakeAgentScript) -> Node:
    """Build the Manager node around a fixture-declared script."""

    def apply(state, output) -> None:
        state.manager_output = output

    return Node(
        name="manager",
        build_input=lambda state, invocation: build_manager_view(state, invocation=invocation),
        run=FakeManager(script=script).run,
        apply=apply,
        requires_restricted_view=True,
        output_model=ManagerOutput,
    )


# --------------------------------------------------------------------------
# The real, model-backed Manager
# --------------------------------------------------------------------------


def build_manager_messages(view: ManagerInput) -> tuple[ModelMessage, ...]:
    """Render the Manager's permitted input into one fresh message list.

    Takes a :class:`ManagerInput` and nothing else. The system prompt is the
    versioned file with the run's own policy substituted in; the user message is
    the memo and the governance block, formatted. Nothing here selects, filters
    or comments on the content.

    The advertised tool surface is derived from ``view.source_access`` rather
    than passed in, so the permission the agent is told about and the permission
    the runtime enforces are the same flag read once. There is no argument by
    which a caller could advertise a tool this run does not provide.
    """
    system = load_manager_prompt().render(policy=render_policy_block(view.policy))
    user = "\n\n".join(
        [
            f"CASE: {view.case_id}",
            render_memo_block(view.analyst_memo),
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


def manager_upstream_ids(view: ManagerInput) -> tuple[str, ...]:
    """The source ids the Manager was *given*, rather than gathered.

    The analyst memo's cited source ids. Recording them as upstream is what
    keeps "the memo told me this" distinguishable from "I opened this myself",
    which is the distinction the later adoption and independent-verification
    measures depend on. Nothing here reads the contract, and nothing here can
    turn an upstream id into a self-verified one.
    """
    return tuple(
        source_id for claim in view.analyst_memo.claims for source_id in claim.source_ids
    )


def make_llm_manager_node(
    *,
    client: ModelClient,
    params: ModelParams,
    call_log: ModelCallLog,
    tools: SourceTools | None = None,
    verification_log: VerificationLog | None = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[Node, AgentContext]:
    """Build the real Manager node, and the context that observes it.

    ``client`` is passed in explicitly. There is no default client and no
    fallback: a run either names the transport it uses or it does not run.

    ``tools`` is this node's own tool layer. It is never shared with Compliance:
    two nodes sharing one ledger would make it impossible to say which of them
    had opened a passage, which is precisely what V1 has to be able to say.
    """

    def apply(state, output) -> None:
        state.manager_output = output

    return make_llm_agent_node(
        name="manager",
        role="manager",
        view_model=ManagerInput,
        build_view=build_manager_view,
        prompt=AgentPromptSpec(
            system_prompt=load_manager_prompt(),
            build_messages=build_manager_messages,
        ),
        repair_prompt=load_repair_prompt(),
        output_model=ManagerOutput,
        params=params,
        client=client,
        call_log=call_log,
        apply=apply,
        tools=tools,
        verification_log=verification_log,
        seed_upstream=manager_upstream_ids,
        max_tool_rounds=max_tool_rounds,
    )

