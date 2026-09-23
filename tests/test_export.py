"""The persistence boundary: the public record cannot carry gold.

A run legitimately holds hidden labels, so the risk is not that they exist but
that they escape. These tests pin both halves of the boundary:

* the execution record is gold-free, at field level, at serialized-key level and
  at value level;
* the hidden labels remain reachable through the explicitly separate scorer-only
  path, and through no other.

They also pin the *reason* the boundary exists: ``RunOutcome`` itself does carry
gold, so serializing it is not an export.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import sample_case
from conftest import GOLD_OFFSET_SENTINELS
from pilot01.schemas import ClauseStatus, ErrorCondition, HiddenGold
from pilot01.workflow.export import (
    GOLD_KEY_PATTERNS,
    ExecutionRecord,
    ExportLeakError,
    ScoringRecord,
    build_execution_record,
    build_scoring_record,
)
from pilot01.workflow.views import (
    ComplianceInput,
    ManagerInput,
    audit_view,
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


def string_values(value):
    """Every string value anywhere in a serialized structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from string_values(nested)


def test_exported_record_is_free_of_hidden_values(make_state, runner):
    """Value-level scan of the public record, across the design matrix."""
    for condition_id, error_condition in ALL_CELLS:
        outcome = runner.run(make_state(condition_id, error_condition))
        values = set(string_values(build_execution_record(outcome).model_dump(mode="json")))
        assert "gold_clause_status" not in values
        for sentinel in GOLD_OFFSET_SENTINELS:
            assert str(sentinel) not in values


# -- the boundary is needed ------------------------------------------------


def test_run_outcome_itself_is_not_an_export_format(make_state, runner):
    """Why the boundary exists: the raw outcome does carry hidden gold.

    This is the accident the export module is built to prevent -- a plain
    ``model_dump()`` of a run writes the scoring labels out.
    """
    outcome = runner.run(make_state("A1V1", ErrorCondition.E1))
    raw = json.dumps(outcome.model_dump(mode="json"))

    assert "gold_target_clause_status" in raw
    assert str(GOLD_OFFSET_SENTINELS[0]) in raw

    # ... and the supported path does not.
    exported = json.dumps(outcome.to_execution_record().model_dump(mode="json"))
    assert "gold" not in exported
    assert str(GOLD_OFFSET_SENTINELS[0]) not in exported


# -- the execution record --------------------------------------------------


def test_safe_execution_export_contains_no_gold(make_state, runner):
    outcome = runner.run(make_state("A1V1", ErrorCondition.E1))
    record = build_execution_record(outcome)
    payload = record.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True)

    assert "gold" not in text
    assert "hidden" not in text
    assert "omission" not in text
    for sentinel in GOLD_OFFSET_SENTINELS:
        assert str(sentinel) not in text
    assert record.to_jsonl() == outcome.to_jsonl()
    assert "gold" not in record.to_jsonl()


def test_execution_record_has_no_field_that_could_hold_gold():
    for name in ExecutionRecord.model_fields:
        assert not any(pattern in name.lower() for pattern in GOLD_KEY_PATTERNS), name


def test_execution_record_carries_the_researcher_metadata(make_state, runner):
    """The record is not stripped bare: it is the analyzable public artifact."""
    state = make_state("A1V1", ErrorCondition.E1, run_id="run-0007")
    outcome = runner.run(state)
    record = build_execution_record(outcome)

    assert record.run_id == "run-0007"
    assert record.case_id == state.case_id
    assert record.repetition_id == state.repetition_id
    assert record.condition_id == "A1V1"
    assert record.error_condition is ErrorCondition.E1
    assert record.source_access is True and record.verification_required is True
    assert record.contract_id == state.contract_id
    assert record.contract_text_hash == state.contract_text_hash
    assert record.target_category == state.target_category
    assert record.policy_id == state.policy.policy_id
    assert record.policy_target_clause_categories == state.policy.target_clause_categories
    assert record.manager_output == state.manager_output
    assert record.compliance_output == state.compliance_output
    assert record.steps_executed == outcome.steps_executed
    assert record.final_node == outcome.final_node
    assert record.event_ids() == tuple(e.event_id for e in outcome.events)
    assert record.events == outcome.events


@pytest.mark.parametrize(("condition_id", "error_condition"), ALL_CELLS)
def test_no_cell_can_export_gold(make_state, runner, condition_id, error_condition):
    outcome = runner.run(make_state(condition_id, error_condition))
    text = json.dumps(build_execution_record(outcome).model_dump(mode="json"))
    for sentinel in GOLD_OFFSET_SENTINELS:
        assert str(sentinel) not in text
    assert "gold" not in text


# -- the guard rails -------------------------------------------------------


def test_execution_record_refuses_a_gold_field():
    """``extra="forbid"`` closes the obvious route in."""
    with pytest.raises(ValidationError):
        ExecutionRecord(
            run_id="r",
            case_id="c",
            repetition_id=0,
            experiment_version="1",
            workflow_version="1",
            policy_version="1",
            condition_id="A0V0",
            error_condition=ErrorCondition.E0,
            source_access=False,
            verification_required=False,
            contract_id="c",
            contract_text_hash="h",
            target_category="t",
            policy_id="POLICY-01",
            policy_target_clause_categories=("x",),
            status="completed",
            final_node="end",
            steps_executed=3,
            revision_round=0,
            gold_action="ESCALATE",
        )


def test_execution_record_refuses_gold_nested_in_a_permitted_field():
    """The validator walks the serialization, so nesting is not an escape hatch."""
    with pytest.raises(ExportLeakError, match="hidden scoring data"):
        ExecutionRecord(
            run_id="r",
            case_id="c",
            repetition_id=0,
            experiment_version="1",
            workflow_version="1",
            policy_version="1",
            condition_id="A0V0",
            error_condition=ErrorCondition.E0,
            source_access=False,
            verification_required=False,
            contract_id="c",
            contract_text_hash="h",
            target_category="t",
            policy_id="POLICY-01",
            policy_target_clause_categories=("x",),
            status="completed",
            final_node="end",
            steps_executed=3,
            revision_round=0,
            events=(
                {
                    "event_id": 0,
                    "run_id": "r",
                    "event_type": "run_started",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "payload": {"nested": {"gold_clause_status": "present"}},
                },
            ),
        )


def test_export_leak_error_is_a_protocol_error():
    """A leak is a structural violation, so it aborts a run rather than warning."""
    from pilot01.workflow.transitions import ProtocolError

    assert issubclass(ExportLeakError, ProtocolError)


# -- the hidden half stays reachable, on its own path ----------------------


def test_hidden_scoring_data_remains_available_to_the_scorer(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E1)
    outcome = runner.run(state)
    scoring = build_scoring_record(outcome)

    assert scoring.gold == state.hidden.gold
    assert scoring.gold.gold_evidence_offsets == GOLD_OFFSET_SENTINELS
    assert (
        scoring.gold.gold_target_clause_status[sample_case.TARGET_CATEGORY]
        is ClauseStatus.PRESENT
    )
    assert scoring.omission == state.hidden.omission
    assert scoring.run_id == outcome.run_id
    assert scoring.condition_id == "A1V1"
    assert scoring.error_condition is ErrorCondition.E1


def test_the_two_records_are_separate_types():
    """The scorer-only path is not reachable from the public one."""
    assert not issubclass(ScoringRecord, ExecutionRecord)
    assert not issubclass(ExecutionRecord, ScoringRecord)
    assert set(ExecutionRecord.model_fields) & set(ScoringRecord.model_fields) == {
        "run_id",
        "case_id",
        "repetition_id",
        "condition_id",
        "error_condition",
    }
    assert "gold" in ScoringRecord.model_fields
    assert "omission" in ScoringRecord.model_fields


def test_scoring_record_still_rejects_unmodelled_fields():
    with pytest.raises(ValidationError):
        ScoringRecord(
            run_id="r",
            case_id="c",
            repetition_id=0,
            condition_id="A0V0",
            error_condition=ErrorCondition.E0,
            gold=HiddenGold(
                gold_clause_status=ClauseStatus.PRESENT, gold_action="ESCALATE"
            ),
            extra="nope",
        )


# -- export does not disturb the agent-facing boundary --------------------


def test_export_leaves_the_restricted_views_unchanged_and_clean(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E1)
    manager_before = build_manager_view(state)
    outcome = runner.run(state)
    build_execution_record(outcome)
    build_scoring_record(outcome)

    manager_after = build_manager_view(state)
    compliance_after = build_compliance_view(state)

    assert manager_after == manager_before
    assert audit_view(manager_after) == ()
    assert audit_view(compliance_after, extra_forbidden=("memo", "analyst")) == ()
    assert set(ManagerInput.model_fields) == {
        "case_id",
        "policy",
        "analyst_memo",
        "source_access",
        "verification_required",
        "invocation",
    }
    assert "analyst_memo" not in ComplianceInput.model_fields
    assert "analyst_memo" not in json.dumps(compliance_after.model_dump(mode="json"))


def test_export_does_not_mutate_the_state(make_state, runner):
    state = make_state("A1V1", ErrorCondition.E1)
    outcome = runner.run(state)
    before = state.model_dump()
    build_execution_record(outcome)
    build_scoring_record(outcome)
    assert state.model_dump() == before
