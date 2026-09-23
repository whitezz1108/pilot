"""Routing, transitions, and end-to-end execution of the fake workflow.

The route is asserted from the *event log*, not from the config, so these tests
check what the runner actually did rather than what it was told to do.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import sample_case
from pilot01.config import WorkflowConfig
from pilot01.events import EventType
from pilot01.schemas import ClauseStatus, Decision, ErrorCondition, VerificationStatus
from pilot01.workflow.nodes import FakeAgentScript, FakeCompliance, FakeManager
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.state import ExperimentState, RunStatus, TreatmentIntegrityError
from pilot01.workflow.transitions import (
    END,
    FeedbackLoopDisabledError,
    Node,
    NodeRegistry,
    NodeResult,
    ProtocolError,
    TransitionKind,
    UnknownTransitionError,
)
from pilot01.workflow.views import (
    ManagerInput,
    build_compliance_view,
    build_manager_view,
)

ALL_CELLS = [
    ("A0V0", ErrorCondition.E0),
    ("A0V0", ErrorCondition.E1),
    ("A1V0", ErrorCondition.E0),
    ("A1V0", ErrorCondition.E1),
    ("A1V1", ErrorCondition.E0),
    ("A1V1", ErrorCondition.E1),
]


def route_of(outcome) -> list[tuple[str, str]]:
    return [
        (event.node, event.payload["target"])
        for event in outcome.events
        if event.event_type is EventType.TRANSITION
    ]


def loop_workflow(max_revision_rounds: int, *, with_revision_edge: bool) -> WorkflowConfig:
    compliance_edges = {TransitionKind.DEFAULT: END}
    if with_revision_edge:
        compliance_edges[TransitionKind.REQUEST_REVISION] = "manager"
    return WorkflowConfig(
        workflow_version="2",
        start="analyst",
        nodes=("analyst", "manager", "compliance"),
        edges={
            "analyst": {TransitionKind.DEFAULT: "manager"},
            "manager": {TransitionKind.DEFAULT: "compliance"},
            "compliance": compliance_edges,
        },
        max_revision_rounds=max_revision_rounds,
        max_steps=64,
    )


# -- the v1 route ----------------------------------------------------------


def test_sequential_route_is_analyst_manager_compliance_end(make_state, runner):
    outcome = runner.run(make_state("A0V0"))
    assert route_of(outcome) == [
        ("analyst", "manager"),
        ("manager", "compliance"),
        ("compliance", END),
    ]
    assert [event.node for event in outcome.events if event.event_type is EventType.NODE_STARTED] == [
        "analyst",
        "manager",
        "compliance",
    ]
    assert outcome.steps_executed == 3
    assert outcome.final_node == END
    assert outcome.status is RunStatus.COMPLETED


def test_feedback_loop_disabled_in_v1(make_state, workflow, conditions, make_registry):
    """A revision request must abort the run while max_revision_rounds is 0."""
    real = make_registry()
    compliance = real.get("compliance")

    def run(node_input):
        result = compliance.run(node_input)
        return NodeResult(output=result.output, transition=TransitionKind.REQUEST_REVISION)

    rogue = Node(
        name="compliance",
        build_input=compliance.build_input,
        run=run,
        apply=compliance.apply,
        output_model=compliance.output_model,
    )
    registry = NodeRegistry([real.get("analyst"), real.get("manager"), rogue])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    state = make_state("A0V0")

    with pytest.raises(FeedbackLoopDisabledError, match="feedback loop is disabled"):
        runner.run(state)

    assert state.status is RunStatus.FAILED
    errors = [e for e in runner._collector.events if e.event_type is EventType.PROTOCOL_ERROR]
    assert len(errors) == 1
    assert errors[0].payload["error_type"] == "FeedbackLoopDisabledError"
    assert state.revision_round == 0


def test_resolve_rejects_a_transition_with_no_edge():
    workflow = loop_workflow(max_revision_rounds=1, with_revision_edge=False)
    with pytest.raises(UnknownTransitionError, match="no 'request_revision' transition"):
        workflow.resolve("compliance", TransitionKind.REQUEST_REVISION)


def test_runner_rejects_a_workflow_node_missing_from_the_registry(
    make_state, workflow, conditions, make_registry
):
    """Workflow and registry must agree; a gap aborts the run rather than skipping it."""
    real = make_registry()
    registry = NodeRegistry([real.get("analyst"), real.get("manager")])  # compliance missing
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    state = make_state("A0V0")

    with pytest.raises(ProtocolError, match="no node registered under 'compliance'"):
        runner.run(state)

    assert state.status is RunStatus.FAILED
    errors = [e for e in runner._collector.events if e.event_type is EventType.PROTOCOL_ERROR]
    assert errors[0].node == "compliance"


def test_registry_rejects_a_duplicate_node_name(make_registry):
    real = make_registry()
    with pytest.raises(ProtocolError, match="duplicate node registration"):
        NodeRegistry([real.get("manager"), real.get("manager")])


def test_max_steps_guard_stops_a_cyclic_graph(make_state, conditions, make_registry):
    # The cyclic graph calls the manager repeatedly, so the fixture says so
    # explicitly rather than the fake silently reusing its first output.
    repeated = FakeAgentScript(outputs=(sample_case.manager_output(),), repeat_last=True)
    real = make_registry(manager=repeated)
    registry = NodeRegistry([real.get("analyst"), real.get("manager")])
    cyclic = WorkflowConfig(
        workflow_version="cyclic",
        start="analyst",
        nodes=("analyst", "manager"),
        edges={
            "analyst": {TransitionKind.DEFAULT: "manager"},
            "manager": {TransitionKind.DEFAULT: "analyst"},
        },
        max_revision_rounds=0,
        max_steps=5,
    )
    runner = WorkflowRunner(workflow=cyclic, conditions=conditions, registry=registry)
    state = make_state("A0V0")

    with pytest.raises(ProtocolError, match="exceeded max_steps"):
        runner.run(state)
    assert state.status is RunStatus.FAILED
    assert state.steps_executed == 5


def test_analyst_output_must_match_the_frozen_artifact(
    make_state, workflow, conditions, make_registry, repository
):
    real = make_registry()
    analyst = real.get("analyst")

    def build_input(state, invocation):
        return analyst.build_input(state, invocation)

    def run(node_input):
        # Load the *other* arm's artifact: a repository/state disagreement.
        other = (
            ErrorCondition.E1
            if node_input.error_condition is ErrorCondition.E0
            else ErrorCondition.E0
        )
        return NodeResult(output=repository.load(node_input.case_id, other))

    rogue = Node(
        name="analyst",
        build_input=build_input,
        run=run,
        apply=analyst.apply,
        requires_restricted_view=False,
        output_model=analyst.output_model,
    )
    registry = NodeRegistry([rogue, real.get("manager"), real.get("compliance")])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)

    with pytest.raises(ProtocolError, match="frozen to"):
        runner.run(make_state("A0V0", ErrorCondition.E0))


def test_node_returning_the_wrong_output_type_is_rejected(
    make_state, workflow, conditions, make_registry
):
    real = make_registry()
    manager = real.get("manager")
    rogue = Node(
        name="manager",
        build_input=manager.build_input,
        run=lambda node_input: NodeResult(output="not a manager output"),
        apply=manager.apply,
        output_model=manager.output_model,
    )
    registry = NodeRegistry([real.get("analyst"), rogue, real.get("compliance")])
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)

    with pytest.raises(ProtocolError, match="expected ManagerOutput"):
        runner.run(make_state("A0V0"))


# -- the future loop, proven at the interface level only -------------------


def test_loop_extension_point_works_when_enabled(make_state, conditions, make_registry):
    """The engine already supports the feedback loop; v1 just does not enable it.

    Nothing in the runner changes when the loop is switched on -- only the edge
    table and ``max_revision_rounds``.
    """
    # Both agents are called twice on this route, which the fixture declares.
    repeated = FakeAgentScript(
        outputs=(sample_case.manager_output(),), repeat_last=True
    )
    real = make_registry(
        manager=repeated,
        compliance=FakeAgentScript(
            outputs=(sample_case.compliance_output(),), repeat_last=True
        ),
    )
    compliance = real.get("compliance")
    invocations: list[int] = []

    def build_input(state, invocation):
        invocations.append(invocation)
        return compliance.build_input(state, invocation)

    def run(node_input):
        result = compliance.run(node_input)
        if invocations[-1] == 0:
            return NodeResult(
                output=result.output, transition=TransitionKind.REQUEST_REVISION
            )
        return result

    rogue = Node(
        name="compliance",
        build_input=build_input,
        run=run,
        apply=compliance.apply,
        output_model=compliance.output_model,
    )
    registry = NodeRegistry([real.get("analyst"), real.get("manager"), rogue])
    runner = WorkflowRunner(
        workflow=loop_workflow(max_revision_rounds=1, with_revision_edge=True),
        conditions=conditions,
        registry=registry,
    )
    outcome = runner.run(make_state("A0V0"))

    assert route_of(outcome) == [
        ("analyst", "manager"),
        ("manager", "compliance"),
        ("compliance", "manager"),
        ("manager", "compliance"),
        ("compliance", END),
    ]
    assert outcome.state.revision_round == 1
    assert invocations == [0, 1], "the second compliance call must be a fresh context"


def test_loop_respects_the_revision_budget(make_state, conditions, make_registry):
    """With max_revision_rounds=0 the same graph must refuse, not loop."""
    real = make_registry()
    compliance = real.get("compliance")
    rogue = Node(
        name="compliance",
        build_input=compliance.build_input,
        run=lambda node_input: NodeResult(
            output=compliance.run(node_input).output,
            transition=TransitionKind.REQUEST_REVISION,
        ),
        apply=compliance.apply,
        output_model=compliance.output_model,
    )
    registry = NodeRegistry([real.get("analyst"), real.get("manager"), rogue])
    runner = WorkflowRunner(
        workflow=loop_workflow(max_revision_rounds=0, with_revision_edge=False),
        conditions=conditions,
        registry=registry,
    )
    with pytest.raises(FeedbackLoopDisabledError):
        runner.run(make_state("A0V0"))


# -- end to end ------------------------------------------------------------


@pytest.mark.parametrize(("condition_id", "error_condition"), ALL_CELLS)
def test_fake_workflow_runs_end_to_end(make_state, runner, condition_id, error_condition):
    state = make_state(condition_id, error_condition)
    outcome = runner.run(state)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.steps_executed == 3
    assert state.manager_output is not None
    assert state.compliance_output is not None
    assert state.compliance_output.rule_id == state.policy.policy_id
    assert state.current_node == END
    assert state.revision_round == 0
    assert [e.event_type for e in outcome.events][0] is EventType.RUN_STARTED
    assert [e.event_type for e in outcome.events][-1] is EventType.RUN_COMPLETED


# -- what the fakes do and do not decide -----------------------------------


def test_fake_agents_do_not_infer_absent_from_a_missing_claim(make_state, runner):
    """The inference Gate 1 must not encode: no target claim -> absent.

    The E1 memo carries no claim in the target clause categories at all. The
    fixture declares a handoff that reports the clause ``present`` and
    escalates, and that is exactly what comes out.

    An earlier revision read the gap as ``absent`` and accepted here. That
    manufactured the very effect the pilot exists to measure, and made it look
    like a property of the workflow rather than of a fixture.
    """
    outcome = runner.run(make_state("A0V0", ErrorCondition.E1))
    state = outcome.state
    targets = [
        claim
        for claim in state.analyst_memo.claims
        if state.policy.is_target_category(claim.category)
    ]
    assert targets == [], "fixture assumption: the E1 memo has no target claim"
    assert state.manager_output.clause_status is ClauseStatus.PRESENT
    assert state.compliance_output.decision is Decision.ESCALATE


def test_the_same_script_yields_the_same_output_on_both_arms(make_state, runner):
    """Arm independence: under one declared scenario, E0 and E1 cannot differ.

    Any difference would mean production code had read the memo.
    """
    e0 = runner.run(make_state("A0V0", ErrorCondition.E0))
    e1 = runner.run(make_state("A0V0", ErrorCondition.E1, run_id="run-0002"))

    assert e0.state.manager_output.model_dump() == e1.state.manager_output.model_dump()
    assert (
        e0.state.compliance_output.model_dump() == e1.state.compliance_output.model_dump()
    )


def test_a_declared_omission_scenario_flows_through_unchanged(
    make_state, workflow, conditions, make_registry
):
    """Propagation is a fixture claim, and the fixture controls it end to end.

    Declaring "the Manager reads the gap as absent, Compliance accepts" yields
    ACCEPT on an E1 run whose gold is ESCALATE -- the story the experiment is
    about. It is produced here by two explicitly declared outputs, so the test
    states out loud what a real agent would have to do for the effect to appear.
    """
    registry = make_registry(
        manager=FakeAgentScript(
            outputs=(
                sample_case.manager_output(
                    clause_status=ClauseStatus.ABSENT,
                    decision=Decision.ACCEPT,
                    evidence_ids=(),
                    adopted_upstream_claim_ids=(),
                ),
            )
        ),
        compliance=FakeAgentScript(
            outputs=(
                sample_case.compliance_output(
                    clause_status=ClauseStatus.ABSENT,
                    decision=Decision.ACCEPT,
                    evidence_ids=(),
                    adopted_upstream_claim_ids=(),
                ),
            )
        ),
    )
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    state = make_state("A0V0", ErrorCondition.E1)
    runner.run(state)

    assert state.manager_output.clause_status is ClauseStatus.ABSENT
    assert state.compliance_output.decision is Decision.ACCEPT
    assert state.hidden.gold.gold_action is Decision.ESCALATE


def test_source_access_alone_changes_nothing(make_state, runner):
    """A0V0 and A1V0 differ only in a flag the fake never reads."""
    for error_condition in (ErrorCondition.E0, ErrorCondition.E1):
        a0v0 = runner.run(make_state("A0V0", error_condition))
        a1v0 = runner.run(make_state("A1V0", error_condition, run_id="run-0002"))
        assert (
            a0v0.state.compliance_output.model_dump()
            == a1v0.state.compliance_output.model_dump()
        )


def test_a_declared_verification_outcome_flows_through_unchanged(
    make_state, workflow, conditions, make_registry
):
    """A1V1's outcome is what the fixture declares, not what a stub decided.

    Gate 1 ships no verifier implementation, so nothing in ``src/`` fixes what
    an unverifiable source check does to the decision. The fixture declares it.
    """
    registry = make_registry(
        compliance=FakeAgentScript(
            outputs=(
                sample_case.compliance_output(
                    clause_status=ClauseStatus.UNKNOWN,
                    decision=Decision.ESCALATE,
                    verification_status=VerificationStatus.UNVERIFIABLE,
                    reason_summary="Declared: the source check could not be completed.",
                ),
            )
        )
    )
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    output = runner.run(make_state("A1V1", ErrorCondition.E0)).state.compliance_output

    assert output.verification_status is VerificationStatus.UNVERIFIABLE
    assert output.clause_status is ClauseStatus.UNKNOWN
    assert output.decision is Decision.ESCALATE


# -- the fixture layer itself ----------------------------------------------


def test_a_fake_with_no_scripted_output_for_an_invocation_fails_loudly(make_state):
    """No fallback default: an undeclared call is an error, not a stale replay."""
    manager = FakeManager(FakeAgentScript(outputs=(sample_case.manager_output(),)))
    view = build_manager_view(make_state("A0V0"), invocation=1)
    with pytest.raises(ProtocolError, match="no scripted output for invocation 1"):
        manager.run(view)


def test_a_fake_script_must_declare_at_least_one_output():
    with pytest.raises(ValidationError, match="at least one output"):
        FakeAgentScript(outputs=())


def test_a_fake_rejects_a_script_of_the_wrong_output_type(make_state, runner):
    state = make_state("A0V0")
    runner.run(state)
    compliance = FakeCompliance(FakeAgentScript(outputs=(sample_case.manager_output(),)))
    with pytest.raises(ProtocolError, match="expected ComplianceOutput"):
        compliance.run(build_compliance_view(state))


def test_repeat_last_is_opt_in(make_state):
    """Reusing one output across invocations is an explicit fixture choice."""
    manager = FakeManager(
        FakeAgentScript(outputs=(sample_case.manager_output(),), repeat_last=True)
    )
    assert manager.run(build_manager_view(make_state("A0V0"), invocation=7)).output is not None


def test_runner_does_not_mutate_experimental_conditions(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E1)
    before = {
        "error_condition": state.error_condition,
        "condition_id": state.condition_id,
        "source_access": state.source_access,
        "verification_required": state.verification_required,
        "hidden": state.hidden,
        "memo": state.analyst_memo,
        "case_id": state.case_id,
        "contract_text_hash": state.contract_text_hash,
    }
    runner.run(state)
    after = {
        "error_condition": state.error_condition,
        "condition_id": state.condition_id,
        "source_access": state.source_access,
        "verification_required": state.verification_required,
        "hidden": state.hidden,
        "memo": state.analyst_memo,
        "case_id": state.case_id,
        "contract_text_hash": state.contract_text_hash,
    }
    assert before == after


def test_runner_refuses_a_state_whose_flags_contradict_its_condition_id(
    make_state, runner
):
    """Tampering with the treatment must abort the run, and be recorded."""
    state = make_state("A1V1")
    tampered = state.model_copy(
        update={"source_access": False, "verification_required": True}
    )

    with pytest.raises(TreatmentIntegrityError, match="does not match condition"):
        runner.run(tampered)

    assert tampered.status is RunStatus.FAILED
    assert tampered.steps_executed == 0
    errors = [e for e in runner._collector.events if e.event_type is EventType.PROTOCOL_ERROR]
    assert len(errors) == 1
    assert not [
        e for e in runner._collector.events if e.event_type is EventType.NODE_STARTED
    ], "no node may run on an inconsistent state"


def test_runner_refuses_an_E1_state_without_an_omission_record(
    conditions, repository, policy
):
    state = ExperimentState.create(
        run_id="run-bad",
        case_id=sample_case.CASE_ID,
        repetition_id=0,
        condition_id="A0V0",
        conditions=conditions,
        error_condition=ErrorCondition.E1,
        contract_id=sample_case.CONTRACT_ID,
        contract_text_hash=sample_case.CONTRACT_TEXT_HASH,
        target_category=sample_case.TARGET_CATEGORY,
        memo=repository.load(sample_case.CASE_ID, ErrorCondition.E1),
        gold_status=ClauseStatus.PRESENT,
        policy=policy,
        omission=None,  # E1 with no provenance record
    )
    with pytest.raises(TreatmentIntegrityError, match="no omission record"):
        state.check_treatment_integrity(conditions)


def test_fake_workflow_is_deterministic(make_state, runner):
    first = runner.run(make_state("A1V1", ErrorCondition.E1))
    second = runner.run(make_state("A1V1", ErrorCondition.E1, run_id="run-0002"))
    assert first.state.compliance_output.model_dump() == second.state.compliance_output.model_dump()
    assert first.state.manager_output.model_dump() == second.state.manager_output.model_dump()
    # Timestamps come from the injected clock, so the logs are reproducible too.
    assert first.to_jsonl().replace("run-0001", "RUN") == second.to_jsonl().replace(
        "run-0002", "RUN"
    )


def test_manager_view_is_the_only_thing_the_manager_receives(make_state, runner):
    """A light integration check that the node ran on a view, not on state."""
    state = make_state("A0V0")
    runner.run(state)
    inputs = [
        event for event in runner._collector.events if event.event_type is EventType.AGENT_INPUT_CREATED
    ]
    manager_event = next(event for event in inputs if event.node == "manager")
    assert manager_event.payload["restricted"] is True
    assert "gold" not in str(manager_event.payload["input"])
    assert set(manager_event.payload["input"]) == set(ManagerInput.model_fields)
