"""Validator treatment boundaries, using synthetic contracts and offline tools."""

import pytest

import sample_case
from pilot01.schemas import EvidenceProvenance, VerificationBasis, VerificationStatus
from pilot01.source import VALIDATOR_VERSION, VerificationFailure, VerificationOutcome
from pilot01.source.ledger import EvidenceLedger
from pilot01.source.verification import evaluate_verification
from test_source_access_conditions import (
    Case, TARGET_PARAGRAPH, GOVERNING_LAW_PARAGRAPH,
    search, open_span, verifying_answer, verifying_compliance_answer,
)


def mixed_provenance():
    return EvidenceProvenance(
        opened_paragraph_ids=(TARGET_PARAGRAPH,),
        inherited_source_ids=(GOVERNING_LAW_PARAGRAPH,),
        verification_basis=VerificationBasis.SELF_CHECKED,
    )


def audit(*, required, ledger, provenance=None, available=True):
    return evaluate_verification(
        output=sample_case.manager_output(evidence_provenance=provenance or mixed_provenance()),
        policy=sample_case.build_policy(),
        ledger=ledger,
        document=sample_case.build_contract_document(),
        verification_required=required,
        source_tools_available=available,
        node="manager",
        invocation=2,
    )


def complete_search_ledger():
    ledger = EvidenceLedger(node="manager")
    ledger.record_search("Change Of Control", [TARGET_PARAGRAPH])
    # Zero hits still establish that a target was searched, not that it is absent.
    ledger.record_search("assignment", [])
    ledger.record_upstream((GOVERNING_LAW_PARAGRAPH,))
    ledger.record_open(TARGET_PARAGRAPH)
    return ledger


def test_same_audit_but_different_gate_with_mixed_inherited_reference():
    ledger = complete_search_ledger()
    optional = audit(required=False, ledger=ledger)
    mandatory = audit(required=True, ledger=ledger)
    assert optional.audit_failures == mandatory.audit_failures == (
        VerificationFailure.EVIDENCE_NOT_OPENED,
    )
    assert optional.validation_pass is mandatory.validation_pass is False
    assert optional.satisfied and not mandatory.satisfied
    assert optional.failures == ()
    assert mandatory.failures == mandatory.audit_failures
    # Opening something is a trace fact; it is not the complete audit verdict.
    assert mandatory.status is VerificationStatus.VERIFIED
    assert mandatory.validator_version == VALIDATOR_VERSION
    assert mandatory.actual_opened_paragraph_ids == (TARGET_PARAGRAPH,)
    assert mandatory.target_search_audit[1].matched_queries == ("assignment",)
    inherited = mandatory.citation_audit[1]
    assert inherited.in_contract and inherited.observed and not inherited.self_opened
    assert inherited.reported_as == "inherited_source_ids"


def test_reopening_inherited_paragraph_in_this_invocation_completes_audit():
    ledger = complete_search_ledger()
    ledger.record_open(GOVERNING_LAW_PARAGRAPH)
    for required in (False, True):
        result = audit(required=required, ledger=ledger)
        assert result.validation_pass is True
        assert result.audit_failures == ()
        assert result.satisfied


def test_optional_missing_target_is_audited_even_when_not_enforced():
    ledger = EvidenceLedger(node="manager")
    ledger.record_search("change-of-control", [])
    ledger.record_open(TARGET_PARAGRAPH)
    result = audit(required=False, ledger=ledger)
    assert VerificationFailure.TARGET_NOT_SEARCHED in result.audit_failures
    assert "targets_not_searched=assignment" in result.diagnostics
    assert result.target_search_audit[0].matched_queries == ("change-of-control",)
    assert result.target_search_audit[1].matched_queries == ()
    assert result.failures == () and result.satisfied


def test_historical_and_no_access_records_are_not_complete_audit_passes():
    historical = VerificationOutcome(status=VerificationStatus.VERIFIED)
    assert historical.validation_pass is None and historical.validator_version is None
    result = audit(required=False, ledger=EvidenceLedger(node="manager"), available=False)
    assert result.validation_pass is None and result.audit_failures == ()


def test_replay_excludes_other_nodes_invocations_and_failed_tool_attempts():
    from pilot01.source import ToolCallRecord, replay_ledger

    records = []
    for node, invocation, tool, ok, query, ids in (
        ("compliance", 2, "open_source_span", True, "", (GOVERNING_LAW_PARAGRAPH,)),
        ("manager", 1, "open_source_span", True, "", (GOVERNING_LAW_PARAGRAPH,)),
        ("manager", 2, "open_source_span", False, "", (GOVERNING_LAW_PARAGRAPH,)),
        ("manager", 2, "search_contract", False, "assignment", ()),
        ("manager", 2, "search_contract", True, "change of control", (TARGET_PARAGRAPH,)),
        ("manager", 2, "open_source_span", True, "", (TARGET_PARAGRAPH,)),
    ):
        records.append(ToolCallRecord(
            sequence=len(records), call_index=len(records), node=node, invocation=invocation,
            tool=tool, available=True, ok=ok, arguments={"query": query}, result_ids=ids,
        ))
    ledger = replay_ledger(records, node="manager", invocation=2)
    result = audit(required=True, ledger=ledger)
    assert ledger.opened_ids == (TARGET_PARAGRAPH,)
    assert result.target_search_audit[1].matched_queries == ()
    assert VerificationFailure.EVIDENCE_NOT_OPENED in result.failures
    assert VerificationFailure.TARGET_NOT_SEARCHED in result.failures


@pytest.mark.parametrize("condition", ["A1V0", "A1V1"])
@pytest.mark.parametrize("node", ["manager", "compliance"])
def test_runtime_records_audit_and_gates_before_applying_candidate(condition, node):
    normal_manager = (search(), search("assignment"), open_span(), verifying_answer())
    normal_compliance = (
        search(), search("assignment"), open_span(), verifying_compliance_answer()
    )
    answer = verifying_answer if node == "manager" else verifying_compliance_answer
    mixed = (
        search(), search("assignment"), open_span(),
        answer(evidence_provenance=mixed_provenance()),
    )
    case = Case(
        condition_id=condition,
        manager_responses=mixed if node == "manager" else normal_manager,
        compliance_responses=mixed if node == "compliance" else normal_compliance,
    )
    state = case.run()
    result = case.verdicts(node)[0]
    assert result.validation_pass is False
    assert VerificationFailure.EVIDENCE_NOT_OPENED in result.audit_failures
    if condition == "A1V1":
        assert case.error is not None
        assert getattr(state, f"{node}_output") is None
        if node == "manager":
            assert case.verdicts("compliance") == ()
    else:
        assert case.error is None
        assert getattr(state, f"{node}_output") is not None


@pytest.mark.parametrize("required", [False, True])
def test_unregistered_reason_reference_is_audited_and_must_be_opened(required):
    ledger = complete_search_ledger()
    output = sample_case.manager_output(
        evidence_ids=(TARGET_PARAGRAPH,), verification_status=VerificationStatus.VERIFIED,
        reason_summary=f"The clause assessment uses {GOVERNING_LAW_PARAGRAPH}.",
    )
    result = evaluate_verification(
        output=output, policy=sample_case.build_policy(), ledger=ledger,
        document=sample_case.build_contract_document(), verification_required=required,
        source_tools_available=True,
    )
    assert result.reason_paragraph_ids == (GOVERNING_LAW_PARAGRAPH,)
    assert result.unregistered_reason_paragraph_ids == (GOVERNING_LAW_PARAGRAPH,)
    assert VerificationFailure.NARRATIVE_REFERENCE_NOT_REGISTERED in result.audit_failures
    assert VerificationFailure.EVIDENCE_NOT_OPENED in result.audit_failures
    assert result.satisfied is (not required)
    assert result.citation_audit[-1].reported_as == "reason_summary"


def test_opened_but_unregistered_reason_reference_still_fails_consistency():
    ledger = complete_search_ledger()
    ledger.record_open(GOVERNING_LAW_PARAGRAPH)
    output = sample_case.manager_output(
        evidence_ids=(TARGET_PARAGRAPH,), verification_status=VerificationStatus.VERIFIED,
        reason_summary=f"Uses {GOVERNING_LAW_PARAGRAPH} and its local shorthand "
                       f"{GOVERNING_LAW_PARAGRAPH.split(':')[1]}.",
    )
    result = evaluate_verification(
        output=output, policy=sample_case.build_policy(), ledger=ledger,
        document=sample_case.build_contract_document(), verification_required=True,
        source_tools_available=True,
    )
    assert result.reason_paragraph_ids == (GOVERNING_LAW_PARAGRAPH,)
    assert result.failures == (VerificationFailure.NARRATIVE_REFERENCE_NOT_REGISTERED,)


def test_registered_opened_reason_references_pass_with_uncertainty_only_unopened_mention():
    ledger = complete_search_ledger()
    short = GOVERNING_LAW_PARAGRAPH.split(':')[1]
    output = sample_case.manager_output(
        evidence_ids=(TARGET_PARAGRAPH,), verification_status=VerificationStatus.VERIFIED,
        reason_summary=f"Assessment uses {TARGET_PARAGRAPH}.",
        uncertainties=(f"I did not open {short}; it is not evidence for my assessment.",),
    )
    result = evaluate_verification(
        output=output, policy=sample_case.build_policy(), ledger=ledger,
        document=sample_case.build_contract_document(), verification_required=True,
        source_tools_available=True,
    )
    assert result.validation_pass is True
    assert result.uncertainty_paragraph_ids == (GOVERNING_LAW_PARAGRAPH,)
    assert result.unregistered_reason_paragraph_ids == ()


def test_foreign_reason_id_is_rejected_and_shorthand_does_not_resolve_it_locally():
    ledger = complete_search_ledger()
    foreign = "0123456789ab:p0001"
    output = sample_case.manager_output(
        evidence_ids=(TARGET_PARAGRAPH,), verification_status=VerificationStatus.VERIFIED,
        reason_summary=f"Assessment uses [{foreign}].",
    )
    result = evaluate_verification(
        output=output, policy=sample_case.build_policy(), ledger=ledger,
        document=sample_case.build_contract_document(), verification_required=True,
        source_tools_available=True,
    )
    assert result.reason_paragraph_ids == (foreign,)
    assert VerificationFailure.EVIDENCE_NOT_IN_CONTRACT in result.failures
    assert VerificationFailure.EVIDENCE_NOT_OBSERVED in result.failures


@pytest.mark.parametrize("condition", ["A1V0", "A1V1"])
@pytest.mark.parametrize("node", ["manager", "compliance"])
def test_runtime_gates_narrative_reference_before_applying_output(condition, node):
    answer = verifying_answer if node == "manager" else verifying_compliance_answer
    narrative = (
        search(), search("assignment"), open_span(),
        answer(reason_summary=f"Assessment also uses {GOVERNING_LAW_PARAGRAPH}."),
    )
    case = Case(
        condition_id=condition,
        manager_responses=narrative if node == "manager" else (
            search(), search("assignment"), open_span(), verifying_answer()
        ),
        compliance_responses=narrative if node == "compliance" else (
            search(), search("assignment"), open_span(), verifying_compliance_answer()
        ),
    )
    state = case.run()
    assert VerificationFailure.NARRATIVE_REFERENCE_NOT_REGISTERED in case.verdicts(node)[0].audit_failures
    assert (getattr(state, f"{node}_output") is None) is (condition == "A1V1")
    # Candidate remains in the model log even when the workflow rejects it.
    assert any(record.parsed_output and GOVERNING_LAW_PARAGRAPH in
               record.parsed_output.get("reason_summary", "")
               for record in case.call_log.records if record.role == node)
