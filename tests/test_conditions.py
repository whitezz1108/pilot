"""Governance conditions, workflow configuration, and treatment integrity.

These tests protect the *design*: exactly three main cells, no A0V1, a strictly
sequential v1 workflow with the feedback loop disabled, and governance flags
that cannot drift from the condition id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import pilot01
import sample_case
from pilot01.config import (
    V1_EXCLUDED_CONDITION_IDS,
    V1_MAIN_CONDITION_IDS,
    ConditionConfig,
    ConditionsConfig,
    WorkflowConfig,
    config_dir,
    load_conditions_v1,
    load_workflow_v1,
)
from pilot01.schemas import (
    ClauseStatus,
    Decision,
    ErrorCondition,
    ExperimentalPolicy,
    HiddenGold,
    expected_decision,
    policy_01,
)
from pilot01.workflow.state import ExperimentState, TreatmentIntegrityError
from pilot01.workflow.transitions import (
    END,
    FeedbackLoopDisabledError,
    TransitionKind,
    WorkflowConfigError,
)


# -- the three main governance cells ---------------------------------------


def test_A0V0_has_no_source_access(conditions):
    condition = conditions.get("A0V0")
    assert condition.source_access is False
    assert condition.verification_required is False


def test_A1V0_has_source_access_without_required_verification(conditions):
    condition = conditions.get("A1V0")
    assert condition.source_access is True
    assert condition.verification_required is False


def test_A1V1_has_source_access_and_required_verification(conditions):
    condition = conditions.get("A1V1")
    assert condition.source_access is True
    assert condition.verification_required is True


def test_no_A0V1_main_condition(conditions):
    assert "A0V1" not in conditions.main_conditions
    assert "A0V1" not in conditions.conditions
    assert "A0V1" in conditions.excluded_conditions
    assert V1_EXCLUDED_CONDITION_IDS == ("A0V1",)


def test_exactly_three_main_conditions(conditions):
    assert conditions.main_conditions == V1_MAIN_CONDITION_IDS
    assert len(conditions.conditions) == 3


def test_governance_cell_is_a_three_cell_design(conditions):
    """The three cells must not collapse into duplicates."""
    cells = {
        (c.source_access, c.verification_required) for c in conditions.conditions.values()
    }
    assert cells == {(False, False), (True, False), (True, True)}


def test_degenerate_A0V1_cell_is_rejected_by_the_schema():
    with pytest.raises(ValidationError, match="A0V1"):
        ConditionConfig(source_access=False, verification_required=True)


def test_config_cannot_promote_A0V1_to_a_main_condition(conditions):
    """Editing the YAML to run A0V1 must fail loudly, not silently run."""
    with pytest.raises(ValidationError, match="A0V1"):
        ConditionsConfig(
            conditions_version="1",
            conditions={
                "A0V0": conditions.get("A0V0"),
                "A1V0": conditions.get("A1V0"),
                "A1V1": conditions.get("A1V1"),
                "A0V1": {
                    "source_access": False,
                    "verification_required": True,
                    "description": "degenerate",
                },
            },
            main_conditions=("A0V0", "A1V0", "A1V1", "A0V1"),
        )


def test_main_conditions_must_be_defined(conditions):
    # Errors raised inside pydantic validators surface as ValidationError; the
    # message is what carries the semantics.
    with pytest.raises(ValidationError, match="undefined conditions"):
        ConditionsConfig(
            conditions_version="1",
            conditions={"A0V0": conditions.get("A0V0")},
            main_conditions=("A0V0", "A9V9"),
        )


def test_v1_main_condition_set_is_pinned(conditions, tmp_path):
    """The v1 file cannot gain a fourth main cell by editing YAML alone."""
    text = (config_dir() / "conditions_v1.yaml").read_text(encoding="utf-8")
    tampered = text.replace(
        "main_conditions:\n  - A0V0\n  - A1V0\n  - A1V1",
        "main_conditions:\n  - A0V0\n  - A1V0",
    )
    assert tampered != text, "fixture assumption: main_conditions block was found"
    path = tmp_path / "conditions_v1.yaml"
    path.write_text(tampered, encoding="utf-8")
    with pytest.raises(WorkflowConfigError, match="must define exactly"):
        load_conditions_v1(path)


# -- workflow configuration ------------------------------------------------


def test_workflow_v1_is_strictly_sequential(workflow):
    assert workflow.start == "analyst"
    assert workflow.nodes == ("analyst", "manager", "compliance")
    assert workflow.resolve("analyst", TransitionKind.DEFAULT) == "manager"
    assert workflow.resolve("manager", TransitionKind.DEFAULT) == "compliance"
    assert workflow.resolve("compliance", TransitionKind.DEFAULT) == END


def test_workflow_v1_disables_the_feedback_loop(workflow):
    assert workflow.max_revision_rounds == 0
    for node, table in workflow.edges.items():
        assert TransitionKind.REQUEST_REVISION not in table, node


def test_workflow_rejects_a_revision_edge_while_the_loop_is_disabled():
    with pytest.raises(ValidationError, match="request_revision"):
        WorkflowConfig(
            workflow_version="2",
            start="analyst",
            nodes=("analyst", "manager", "compliance"),
            edges={
                "analyst": {TransitionKind.DEFAULT: "manager"},
                "manager": {TransitionKind.DEFAULT: "compliance"},
                "compliance": {
                    TransitionKind.DEFAULT: END,
                    TransitionKind.REQUEST_REVISION: "manager",
                },
            },
            max_revision_rounds=0,
            max_steps=64,
        )


def test_workflow_accepts_the_revision_edge_once_the_loop_is_enabled():
    """The future loop is a config change only -- the engine already supports it."""
    workflow = WorkflowConfig(
        workflow_version="2",
        start="analyst",
        nodes=("analyst", "manager", "compliance"),
        edges={
            "analyst": {TransitionKind.DEFAULT: "manager"},
            "manager": {TransitionKind.DEFAULT: "compliance"},
            "compliance": {
                TransitionKind.DEFAULT: END,
                TransitionKind.REQUEST_REVISION: "manager",
            },
        },
        max_revision_rounds=2,
        max_steps=64,
    )
    assert workflow.resolve("compliance", TransitionKind.REQUEST_REVISION) == "manager"


def test_workflow_rejects_an_edge_to_an_unknown_node():
    with pytest.raises(ValidationError, match="unknown target"):
        WorkflowConfig(
            workflow_version="1",
            start="analyst",
            nodes=("analyst", "manager"),
            edges={
                "analyst": {TransitionKind.DEFAULT: "nowhere"},
                "manager": {TransitionKind.DEFAULT: END},
            },
            max_revision_rounds=0,
            max_steps=8,
        )


def test_workflow_rejects_a_missing_default_edge():
    with pytest.raises(ValidationError, match="no 'default' edge"):
        WorkflowConfig(
            workflow_version="1",
            start="analyst",
            nodes=("analyst", "manager"),
            edges={
                "analyst": {TransitionKind.DEFAULT: "manager"},
                "manager": {},
            },
            max_revision_rounds=0,
            max_steps=8,
        )


def test_unknown_transition_kind_is_rejected(workflow):
    with pytest.raises(FeedbackLoopDisabledError):
        workflow.resolve("compliance", TransitionKind.REQUEST_REVISION)


def test_workflow_v1_file_is_the_loaded_file(workflow):
    assert workflow.workflow_version == load_workflow_v1().workflow_version == "1"


# -- treatment integrity ---------------------------------------------------


@pytest.mark.parametrize(
    ("condition_id", "source_access", "verification_required"),
    [("A0V0", False, False), ("A1V0", True, False), ("A1V1", True, True)],
)
def test_governance_flags_are_derived_from_the_condition_id(
    make_state, condition_id, source_access, verification_required
):
    state = make_state(condition_id)
    assert state.source_access is source_access
    assert state.verification_required is verification_required


def test_state_factory_refuses_a_non_main_condition(make_state):
    with pytest.raises(TreatmentIntegrityError, match="not a permitted main"):
        make_state("A0V1")


def test_treatment_fields_are_frozen(make_state):
    """The orchestration layer cannot silently mutate a condition mid-run."""
    state = make_state("A0V0")
    for field, value in (
        ("error_condition", ErrorCondition.E1),
        ("condition_id", "A1V1"),
        ("source_access", True),
        ("verification_required", True),
        ("case_id", "CASE-999"),
        ("target_category", "assignment"),
    ):
        with pytest.raises(ValidationError):
            setattr(state, field, value)
    assert state.condition_id == "A0V0"


def test_gold_must_be_consistent_with_the_policy(make_state):
    """A mislabelled gold action must not be able to enter a run."""
    state = make_state("A0V0")
    mislabelled = {
        **state.model_dump(),
        "hidden": {
            "gold": HiddenGold(
                gold_target_clause_status={
                    sample_case.TARGET_CATEGORY: ClauseStatus.PRESENT,
                    sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.ABSENT,
                },
                gold_action=Decision.ACCEPT,  # POLICY-01 says ESCALATE
            )
        },
    }
    # TreatmentIntegrityError is a ProtocolError (a RuntimeError), so pydantic
    # propagates it unwrapped and the domain error type survives validation.
    with pytest.raises(TreatmentIntegrityError, match="gold label inconsistency"):
        ExperimentState(**mislabelled)


def test_gold_action_follows_policy_01(make_state):
    """Gold is drawn from ``present``/``absent`` only, and maps as the rule says.

    There is deliberately no ``unknown`` case here any more. ``unknown`` used to
    be a gold label -- the correct reading of an omission memo was "the status
    is unresolved", and revision 1 escalated that -- but a gold label is a
    property of the *contract*, which is built knowing which clauses it holds.
    ``unknown`` is a property of an *agent's evidence*, so it is not a gold
    value and the schema refuses it. What the policy does with an agent that
    reports it is asserted in
    :func:`test_the_policy_maps_an_unresolved_assessment_to_review`.
    """
    present = make_state("A0V0", gold_status=ClauseStatus.PRESENT)
    absent = make_state("A0V0", gold_status=ClauseStatus.ABSENT)

    assert present.hidden.gold.gold_action.value == "ESCALATE"
    assert absent.hidden.gold.gold_action.value == "ACCEPT"


def test_gold_refuses_an_unknown_label(make_state):
    """A case cannot be labelled unknown: the contract's clauses are known."""
    with pytest.raises(ValidationError, match="never unknown"):
        make_state("A0V0", gold_status=ClauseStatus.UNKNOWN)


def test_the_policy_maps_an_unresolved_assessment_to_review():
    """An agent's ``unknown`` is REVIEW, and the gold action is never REVIEW.

    The two halves of the revision-2 rule, asserted together because they are
    the same design decision seen from either side: unresolved is reported as
    unresolved, and a case is never *built* unresolved.
    """
    policy = sample_case.build_policy()
    statuses = {
        sample_case.TARGET_CATEGORY: ClauseStatus.UNKNOWN,
        sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.ABSENT,
    }
    assert expected_decision(statuses, policy) is Decision.REVIEW

    # Presence still wins over an unresolved sibling: the trigger is
    # disjunctive, so one positive clause decides the case.
    assert (
        expected_decision(
            {
                sample_case.TARGET_CATEGORY: ClauseStatus.UNKNOWN,
                sample_case.OTHER_CLAUSE_CATEGORY: ClauseStatus.PRESENT,
            },
            policy,
        )
        is Decision.ESCALATE
    )


def test_E0_and_E1_share_contract_metadata_and_gold(make_state):
    """The treatment is the memo alone: the contract and its gold are fixed."""
    e0 = make_state("A1V0", ErrorCondition.E0)
    e1 = make_state("A1V0", ErrorCondition.E1)

    assert e0.contract_id == e1.contract_id
    assert e0.contract_text_hash == e1.contract_text_hash
    assert e0.hidden.gold == e1.hidden.gold
    assert e0.target_category == e1.target_category
    assert e0.analyst_memo.memo_id == e1.analyst_memo.memo_id
    assert len(e0.analyst_memo.claims) == len(e1.analyst_memo.claims) + 1


def test_sample_case_targets_a_single_claim(make_state, omission_record):
    """The omission experiment needs exactly one target claim to delete."""
    e0 = make_state("A0V0", ErrorCondition.E0)
    targets = [
        claim
        for claim in e0.analyst_memo.claims
        if e0.policy.is_target_category(claim.category)
    ]
    assert [claim.claim_id for claim in targets] == [sample_case.TARGET_CLAIM_ID]
    assert omission_record.omitted_claim_id == sample_case.TARGET_CLAIM_ID


# -- the policy's target categories are an input, not a default ------------


def test_policy_requires_explicit_target_categories():
    """The categories POLICY-01 treats as critical must be passed in, always."""
    with pytest.raises(TypeError):
        policy_01()  # type: ignore[call-arg]


def test_policy_rejects_empty_target_categories():
    with pytest.raises(ValidationError, match="no target clause categories"):
        ExperimentalPolicy(
            policy_id="POLICY-01",
            policy_version="1",
            target_clause_categories=(),
            decision_if_target_present=Decision.ESCALATE,
            decision_if_target_absent=Decision.ACCEPT,
            decision_if_target_unknown=Decision.REVIEW,
            description="",
        )


def test_policy_rejects_duplicate_target_categories():
    with pytest.raises(ValidationError, match="duplicate target clause categories"):
        policy_01(target_clause_categories=("change_of_control", "change_of_control"))


def test_state_has_no_default_policy(make_state):
    """A run cannot silently fall back to a built-in policy."""
    assert ExperimentState.model_fields["policy"].is_required()
    with pytest.raises(ValidationError, match="policy"):
        ExperimentState(
            **{k: v for k, v in make_state("A0V0").model_dump().items() if k != "policy"}
        )


def test_the_case_fixture_supplies_the_target_categories(policy):
    """The categories in play come from the case, and are named there."""
    assert policy.target_clause_categories == sample_case.TARGET_CLAUSE_CATEGORIES
    assert sample_case.TARGET_CATEGORY in policy.target_clause_categories


def test_no_clause_category_placeholder_ships_in_src():
    """Clause categories must not become code defaults.

    The placeholder categories live in the test fixture only. If one ever
    appears under ``src/`` it has become an implicit experimental default, which
    is exactly what the dataset audit is supposed to decide instead.
    """
    placeholders = ("change_of_control", "assignment")
    offenders: list[str] = []
    for path in sorted(Path(pilot01.__file__).resolve().parent.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.name}: {name}" for name in placeholders if name in text
        )
    assert offenders == [], f"clause category placeholder(s) in src/: {offenders}"
