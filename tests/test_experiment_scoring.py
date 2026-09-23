"""Deriving outcomes from raw artifacts: the metrics, and what ``None`` means.

Gate 3 §7 and §8. Two rules run through every test here.

**Incorrect is not unavailable.** A metric that cannot be computed is ``None``
with a note explaining why, never ``False``. The tests below assert the notes as
well as the values, because a ``None`` nobody can interpret is not an
improvement on a wrong answer.

**No invented numbers.** Usage totals are all-or-nothing, and a cost is reported
only when the operator supplied prices and the provider supplied counts.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

import pytest
import sample_case

import fixture_registry as fixture
from pilot01.experiment import (
    CorrectionStage,
    EvidenceFailure,
    ExperimentPaths,
    ExperimentScores,
    PricingTable,
    ScoringError,
    ScriptedClientFactory,
    score_experiment,
    score_run,
    spans_overlap,
)
from pilot01.experiment import read_raw_run
from pilot01.model import ScriptedModelClient, TokenUsage
from pilot01.schemas import ClauseStatus, Decision, ErrorCondition


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry():
    return _registry()


def run_and_score(
    tmp_path: Path,
    *,
    case_ids=(fixture.CASE_A,),
    condition_ids=("A1V1",),
    behaviour=None,
    factory=None,
    pricing=None,
):
    plan = fixture.make_plan(
        registry=_registry(), case_ids=case_ids, condition_ids=condition_ids
    )
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    fixture.execute(plan, _registry(), paths, behaviour=behaviour, factory=factory)
    return score_experiment(paths.raw_dir, registry=_registry(), pricing=pricing)


def row_for(scores: ExperimentScores, *, case_id, error_condition, condition_id):
    rows = scores.of_cell(
        case_id=case_id,
        error_condition=ErrorCondition(error_condition),
        condition_id=condition_id,
    )
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


def propagate(job, registry):
    """E1: the Manager reads the gap as absence, and Compliance agrees.

    The phenomenon the pilot exists to observe, declared rather than inferred.
    """
    if job.error_condition is ErrorCondition.E0:
        return fixture.default_responses(job, registry)
    return fixture.scripted_responses(
        source_access=job.source_access,
        verification_required=job.verification_required,
        paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
        manager_status=ClauseStatus.ABSENT,
        manager_decision=Decision.ACCEPT,
        compliance_status=ClauseStatus.ABSENT,
        compliance_decision=Decision.ACCEPT,
    )


def compliance_recovers(job, registry):
    """E1: the Manager loses the clause, Compliance puts it back."""
    if job.error_condition is ErrorCondition.E0:
        return fixture.default_responses(job, registry)
    case = registry.get(job.case_id)
    return fixture.scripted_responses(
        source_access=job.source_access,
        verification_required=job.verification_required,
        paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
        manager_status=ClauseStatus.ABSENT,
        manager_decision=Decision.ACCEPT,
        compliance_status=case.gold_clause_status,
        compliance_decision=case.gold_action,
    )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_a_row_carries_the_run_identity_from_its_own_artifact(tmp_path):
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.run_id.startswith("EXP-1__CASE-A__E0__A1V1")
    assert row.experiment_id == "EXP-1"
    assert row.contract_id == fixture.CONTRACT_ID
    assert row.source_access is True
    assert row.verification_required is True
    assert row.repeat_index == 0
    assert row.plan_fingerprint.startswith("sha256:")
    assert row.cell == (fixture.CASE_A, "E0", "A1V1", 0)


def test_the_row_carries_the_cases_labels(tmp_path):
    """Gold reaches the row from the registry, never from the raw tree."""
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.gold_clause_status is ClauseStatus.PRESENT
    assert row.gold_action is Decision.ESCALATE
    assert row.is_negative_sentinel is False


def test_scoring_a_run_against_the_wrong_case_is_refused(tmp_path):
    plan = fixture.make_plan(
        registry=_registry(), case_ids=(fixture.CASE_A,), condition_ids=("A1V1",)
    )
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    fixture.execute(plan, _registry(), paths)
    raw = read_raw_run(paths.run_dir(plan.jobs[0].run_id))

    with pytest.raises(ScoringError, match="scored against case"):
        score_run(
            raw,
            case=_registry().get(fixture.CASE_B),
            document=_registry().document(fixture.CASE_B),
        )


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------


def test_a_correct_run_scores_correct(tmp_path):
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.final_decision is Decision.ESCALATE
    assert row.correct is True
    assert row.produced_answer is True


def test_an_omission_that_propagates_is_a_wrong_answer(tmp_path):
    scores = run_and_score(tmp_path, behaviour=propagate)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E1", condition_id="A1V1")

    assert row.final_decision is Decision.ACCEPT
    assert row.correct is False
    assert row.error_survival_manager is True
    assert row.error_survival_compliance is True
    assert row.correction_stage is CorrectionStage.NEVER


def test_a_run_with_no_answer_scores_none_rather_than_false(tmp_path):
    """§7: a protocol failure must not enter the error rate as a wrong answer."""
    scores = run_and_score(
        tmp_path,
        behaviour=lambda job, registry: {
            "manager": ("not JSON", "still not JSON"),
            "compliance": ("unused",),
        },
    )
    row = scores.rows[0]

    assert row.correct is None
    assert row.produced_answer is False
    assert row.protocol_failure is True
    assert row.correction_stage is CorrectionStage.UNAVAILABLE
    assert any("no final decision" in note for note in row.notes)
    assert any("no usable output" in note for note in row.notes)


def test_a_sentinel_that_accepts_scores_correct(tmp_path):
    scores = run_and_score(tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",))
    row = scores.rows[0]

    assert row.gold_action is Decision.ACCEPT
    assert row.is_negative_sentinel is True
    assert row.final_decision is Decision.ACCEPT
    assert row.correct is True


# --------------------------------------------------------------------------
# Error survival and correction stage
# --------------------------------------------------------------------------


def test_error_survival_is_not_applicable_on_the_correct_memo_arm(tmp_path):
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.error_survival_manager is None
    assert row.error_survival_compliance is None
    assert row.correction_stage is CorrectionStage.ALREADY_CORRECT
    assert any("the arm carried no error" in note for note in row.notes)


def test_the_stage_names_where_the_error_stopped(tmp_path):
    scores = run_and_score(tmp_path, behaviour=compliance_recovers)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E1", condition_id="A1V1")

    assert row.manager_clause_status is ClauseStatus.ABSENT
    assert row.compliance_clause_status is ClauseStatus.PRESENT
    assert row.error_survival_manager is True
    assert row.error_survival_compliance is False
    assert row.correction_stage is CorrectionStage.COMPLIANCE
    assert row.correct is True


def test_a_manager_that_never_lost_the_clause_is_the_manager_stage(tmp_path):
    """The default scenario: the Manager's answer is already right."""
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E1", condition_id="A1V1")

    assert row.error_survival_manager is False
    assert row.error_survival_compliance is False
    assert row.correction_stage is CorrectionStage.MANAGER


def test_error_survival_is_not_applicable_when_the_omitted_claim_did_not_assert_presence():
    """The sentinel's gold status is ABSENT, so "recovering" it is a different act."""
    from pilot01.experiment.scoring import _error_survival

    notes: list[str] = []
    result = _error_survival(
        node="manager",
        error_condition=ErrorCondition.E1,
        gold_status=ClauseStatus.ABSENT,
        status=ClauseStatus.PRESENT,
        notes=notes,
    )

    assert result is None
    assert notes and "did not assert presence" in notes[0]


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_opening_the_gold_paragraph_overlaps_it(tmp_path):
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.manager_evidence.failure is None
    assert row.manager_evidence.overlaps_gold is True
    assert row.manager_evidence.self_opened == (fixture.TARGET_PARAGRAPH_ID,)
    assert row.manager_evidence.opened_count == 1
    assert row.manager_opens == 1
    assert row.manager_searches == 1


def test_citing_upstream_without_opening_anything_is_not_self_opened(tmp_path):
    """A0V0: the node has no tools, so it can only cite what it was handed."""
    scores = run_and_score(tmp_path, condition_ids=("A0V0",))
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A0V0")

    assert row.manager_evidence.available is True
    assert row.manager_evidence.cited == ("S-3.2",)
    assert row.manager_evidence.self_opened == ()
    assert row.manager_evidence.overlaps_gold is False
    assert row.manager_evidence.failure is EvidenceFailure.NOT_SELF_OPENED
    assert row.manager_opens == 0


def test_a_sentinel_has_no_gold_span_to_overlap(tmp_path):
    """Not a failure to find the clause: there is no clause, so the question cannot be asked."""
    scores = run_and_score(tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",))
    row = scores.rows[0]

    assert row.manager_evidence.gold_spans == ()
    assert row.manager_evidence.overlaps_gold is None
    assert row.manager_evidence.failure is EvidenceFailure.NO_GOLD_SPAN


def test_opening_a_real_but_wrong_paragraph_of_a_gold_bearing_case(tmp_path):
    def open_the_governing_law(job, registry):
        return fixture.scripted_responses(
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=fixture.LAW_PARAGRAPH_ID,
            manager_status=ClauseStatus.PRESENT,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.PRESENT,
            compliance_decision=Decision.ESCALATE,
            query="governing law",
        )

    scores = run_and_score(
        tmp_path, condition_ids=("A1V1",), behaviour=open_the_governing_law
    )
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.manager_evidence.self_opened == (fixture.LAW_PARAGRAPH_ID,)
    assert row.manager_evidence.overlaps_gold is False
    assert row.manager_evidence.failure is EvidenceFailure.NO_GOLD_OVERLAP


def cite_the_other_contract(job, registry):
    """The Manager cites its real evidence *and* an id from the other contract.

    Both, rather than the foreign id alone: a citation nothing was opened for is
    reported as ``not_self_opened``, which would mask the more specific finding.
    This is also the shape the failure takes in practice -- an agent that reads
    the right clause and then pads its citation list.
    """
    return fixture.scripted_responses(
        source_access=True,
        verification_required=job.verification_required,
        paragraph_id=fixture.TARGET_PARAGRAPH_ID,
        manager_evidence=(fixture.TARGET_PARAGRAPH_ID, fixture.SECOND_LAW_PARAGRAPH_ID),
        compliance_evidence=(fixture.TARGET_PARAGRAPH_ID,),
        manager_status=ClauseStatus.PRESENT,
        manager_decision=Decision.ESCALATE,
        compliance_status=ClauseStatus.PRESENT,
        compliance_decision=Decision.ESCALATE,
    )


def test_citing_a_foreign_paragraph_id_is_flagged(tmp_path):
    """Under V0 nothing checks the citation, so the metric is what catches it.

    Only reachable without verification: under A1V1 the runtime refuses the
    citation first and the run never completes, which is the next test. This is
    the case the evidence metric exists for -- the run looks fine, and the
    citation is to a clause of a contract the run was never given.
    """
    scores = run_and_score(
        tmp_path, condition_ids=("A1V0",), behaviour=cite_the_other_contract
    )
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V0")

    assert row.manager_evidence.self_opened == (fixture.TARGET_PARAGRAPH_ID,)
    assert row.manager_evidence.foreign == (fixture.SECOND_LAW_PARAGRAPH_ID,)
    assert row.manager_evidence.failure is EvidenceFailure.FOREIGN_EVIDENCE
    assert row.manager_evidence.overlaps_gold is True


def test_under_v1_the_runtime_refuses_a_foreign_citation_before_scoring(tmp_path):
    """The same behaviour under A1V1 is a protocol failure, not a scored answer.

    Worth pinning: the two layers must not disagree about what happened, and the
    scored row must not quietly present a refused run as a wrong answer.
    """
    scores = run_and_score(tmp_path, condition_ids=("A1V1",), behaviour=cite_the_other_contract)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.run_status.value == "failed"
    assert row.protocol_failure is True
    assert row.failure_stage == "manager"
    assert "evidence_not_in_contract" in row.verification_failures
    assert row.correct is None
    assert row.manager_evidence.available is False


def test_citing_no_evidence_at_all_is_reported_as_such(tmp_path):
    def cite_nothing(job, registry):
        return fixture.scripted_responses(
            source_access=True,
            verification_required=False,
            paragraph_id=fixture.TARGET_PARAGRAPH_ID,
            manager_evidence=(),
            compliance_evidence=(),
            manager_status=ClauseStatus.PRESENT,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.PRESENT,
            compliance_decision=Decision.ESCALATE,
        )

    scores = run_and_score(tmp_path, condition_ids=("A1V0",), behaviour=cite_nothing)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V0")

    assert row.manager_evidence.cited == ()
    assert row.manager_evidence.overlaps_gold is False
    assert row.manager_evidence.failure is EvidenceFailure.NO_EVIDENCE


def test_an_unavailable_output_has_no_evidence_assessment(tmp_path):
    scores = run_and_score(
        tmp_path,
        behaviour=lambda job, registry: {
            "manager": ("not JSON", "still not JSON"),
            "compliance": ("unused",),
        },
    )
    row = scores.rows[0]

    assert row.manager_evidence.available is False
    assert row.manager_evidence.overlaps_gold is None
    assert row.manager_evidence.failure is EvidenceFailure.OUTPUT_UNAVAILABLE


def test_spans_overlap_is_half_open():
    assert spans_overlap((0, 10), (5, 15))
    assert spans_overlap((5, 15), (0, 10))
    assert not spans_overlap((0, 10), (10, 20))
    assert not spans_overlap((10, 20), (0, 10))
    assert not spans_overlap((0, 10), (20, 30))


# --------------------------------------------------------------------------
# Fact, action and evidence are three different questions (§6)
# --------------------------------------------------------------------------


def test_a_defensive_escalation_is_a_correct_action_with_no_recovery(tmp_path):
    """§6's Case A: the action matches gold while the fact was never recovered.

    This is the run the spec forbids describing as a successful correction. Both
    agents say UNKNOWN and escalate, landing on the gold action because the
    policy says to escalate when the clause is not found -- not because either
    read the contract. Note they *do* cite the paragraph they opened: V1 asks
    whether the evidence was consulted, not whether the conclusion followed from
    it, so the run is fully compliant and still never recovered the fact.
    """

    def escalate_without_recovering(job, registry):
        return fixture.scripted_responses(
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=fixture.TARGET_PARAGRAPH_ID,
            manager_status=ClauseStatus.UNKNOWN,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.UNKNOWN,
            compliance_decision=Decision.ESCALATE,
        )

    scores = run_and_score(tmp_path, behaviour=escalate_without_recovering)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.gold_clause_status is ClauseStatus.PRESENT
    assert row.gold_action is Decision.ESCALATE
    assert row.final_decision is Decision.ESCALATE
    assert row.final_action_correct is True

    assert row.compliance_clause_status is ClauseStatus.UNKNOWN
    assert row.compliance_clause_status_correct is False
    assert row.manager_clause_status_correct is False
    # The evidence itself was impeccable -- opened, self-cited, overlapping gold.
    assert row.manager_evidence.failure is None
    assert row.manager_evidence.overlaps_gold is True
    # And the correction still did not happen, because the fact was not recovered.
    assert row.manager_corrected_with_evidence is False
    assert row.compliance_corrected_with_evidence is False
    assert any("target_not_recovered" in note for note in row.notes)


def test_recovering_the_clause_with_opened_evidence_is_corrected_with_evidence(tmp_path):
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.manager_clause_status_correct is True
    assert row.compliance_clause_status_correct is True
    assert row.manager_corrected_with_evidence is True
    assert row.compliance_corrected_with_evidence is True
    assert row.manager_self_opened_count == 1
    assert row.compliance_self_opened_count == 1


def test_the_right_clause_with_unopened_evidence_is_not_corrected_with_evidence(tmp_path):
    """§6's Case B: the status is right, the verification is inherited.

    The clause is recovered, so the fact column is True; nothing was opened, so
    the evidence column is False. Two columns, and they disagree -- which is the
    distinction the schema exists to preserve.
    """
    scores = run_and_score(tmp_path, condition_ids=("A0V0",))
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A0V0")

    assert row.compliance_clause_status_correct is True
    assert row.compliance_evidence.failure is EvidenceFailure.NOT_SELF_OPENED
    assert row.compliance_corrected_with_evidence is False
    assert row.manager_corrected_with_evidence is False
    assert row.manager_self_opened_count == 0
    assert any("not_self_opened" in note for note in row.notes)


def test_corrected_with_evidence_is_not_applicable_on_a_sentinel(tmp_path):
    scores = run_and_score(tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",))
    row = scores.rows[0]

    assert row.manager_corrected_with_evidence is None
    assert row.compliance_corrected_with_evidence is None
    assert any("no recovery to evidence" in note for note in row.notes)


def test_the_fact_column_is_still_defined_on_a_sentinel(tmp_path):
    """A sentinel's gold status is ABSENT, so saying ABSENT is a right answer."""
    scores = run_and_score(tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",))
    row = scores.rows[0]

    assert row.gold_clause_status is ClauseStatus.ABSENT
    assert row.manager_clause_status_correct is True
    assert row.compliance_clause_status_correct is True


def test_a_sentinel_answered_present_is_a_false_positive_on_the_fact_column(tmp_path):
    def hallucinate_the_clause(job, registry):
        return fixture.scripted_responses(
            source_access=False,
            verification_required=False,
            paragraph_id=fixture.TARGET_PARAGRAPH_ID,
            manager_status=ClauseStatus.PRESENT,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.PRESENT,
            compliance_decision=Decision.ESCALATE,
        )

    scores = run_and_score(
        tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",), behaviour=hallucinate_the_clause
    )
    row = scores.rows[0]

    assert row.gold_clause_status is ClauseStatus.ABSENT
    assert row.manager_clause_status_correct is False
    assert row.compliance_clause_status_correct is False


def test_false_escalation_is_reported_only_for_sentinels(tmp_path):
    """A positive case is None, not False: nothing was at risk of a false flag."""
    scores = run_and_score(tmp_path, condition_ids=("A1V1",))
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.is_negative_sentinel is False
    assert row.false_escalation is None
    assert any("not applicable" in note for note in row.notes if "false_escalation" in note)


def test_a_sentinel_that_accepts_has_not_falsely_escalated(tmp_path):
    scores = run_and_score(tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",))
    row = scores.rows[0]

    assert row.is_negative_sentinel is True
    assert row.final_decision is Decision.ACCEPT
    assert row.false_escalation is False


def test_a_sentinel_falsely_escalated_is_flagged(tmp_path):
    """The sentinel's whole purpose: catching a confident answer to no clause."""

    def escalate_the_absent_clause(job, registry):
        return fixture.scripted_responses(
            source_access=False,
            verification_required=False,
            paragraph_id=fixture.TARGET_PARAGRAPH_ID,
            manager_status=ClauseStatus.ABSENT,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.ABSENT,
            compliance_decision=Decision.ESCALATE,
        )

    scores = run_and_score(
        tmp_path, case_ids=(fixture.CASE_C,), condition_ids=("A1V0",), behaviour=escalate_the_absent_clause
    )
    row = scores.rows[0]

    assert row.gold_action is Decision.ACCEPT
    assert row.final_decision is Decision.ESCALATE
    assert row.final_action_correct is False
    assert row.false_escalation is True
    # The fact is right and the action is wrong: the agent saw no clause and
    # escalated anyway. Exactly the split §6 wants visible.
    assert row.compliance_clause_status_correct is True


def test_a_run_that_died_has_no_false_escalation_verdict(tmp_path):
    scores = run_and_score(
        tmp_path,
        case_ids=(fixture.CASE_C,),
        condition_ids=("A1V0",),
        behaviour=lambda job, registry: {"manager": ("not JSON", "still not JSON"), "compliance": ("x",)},
    )
    row = scores.rows[0]

    assert row.false_escalation is None
    assert row.final_action_correct is None
    assert any("false_escalation: the run produced no final decision" in note for note in row.notes)


# --------------------------------------------------------------------------
# Claim adoption
# --------------------------------------------------------------------------


def test_adopting_a_memo_claim_on_the_correct_memo_arm_is_not_a_finding(tmp_path):
    """The E0 memo contains the claim, so adopting it is reading the input."""
    scores = run_and_score(tmp_path)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")

    assert row.manager_adopted_claims == (fixture.TARGET_CLAIM[fixture.CASE_A],)
    assert row.unknown_adopted_claims == ()


def test_adopting_the_omitted_claim_on_the_e1_arm_is_a_finding(tmp_path):
    """On E1 the memo never carried it, so nothing in the input could produce it."""
    scores = run_and_score(tmp_path, behaviour=propagate)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E1", condition_id="A1V1")

    assert row.manager_adopted_claims == ()
    assert row.unknown_adopted_claims == ()


def test_a_node_that_adopts_what_it_was_never_given_is_flagged(tmp_path):
    """The Manager adopts a claim the E1 memo does not contain."""

    def adopt_the_deleted_claim(job, registry):
        from pilot01.schemas import ErrorCondition

        if job.error_condition is ErrorCondition.E0:
            return fixture.default_responses(job, registry)
        return fixture.scripted_responses(
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
            manager_status=ClauseStatus.ABSENT,
            manager_decision=Decision.ACCEPT,
            compliance_status=ClauseStatus.ABSENT,
            compliance_decision=Decision.ACCEPT,
            manager_adopted=(fixture.TARGET_CLAIM[fixture.CASE_A],),
            compliance_adopted=(fixture.TARGET_CLAIM[fixture.CASE_A],),
        )

    scores = run_and_score(tmp_path, behaviour=adopt_the_deleted_claim)
    row = row_for(scores, case_id=fixture.CASE_A, error_condition="E1", condition_id="A1V1")

    assert row.unknown_adopted_claims == (fixture.TARGET_CLAIM[fixture.CASE_A],)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def test_verification_is_reported_per_node_and_per_condition(tmp_path):
    """``required``, ``checked`` and ``satisfied`` are three different questions.

    Required is what the condition demanded; checked is what the node actually
    did; satisfied is whether the demand was met. Under A1V0 the node is not
    required to consult the source and does so anyway -- which is a permitted
    choice, not a shortfall, so it is satisfied either way. Only A0V0, where
    there is no tool to call, leaves the source unconsulted.
    """
    scores = run_and_score(tmp_path, condition_ids=("A1V1", "A1V0", "A0V0"))
    required = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V1")
    optional = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A1V0")
    toolless = row_for(scores, case_id=fixture.CASE_A, error_condition="E0", condition_id="A0V0")

    assert required.manager_verification_required is True
    assert required.manager_verification_checked is True
    assert required.manager_verification_satisfied is True
    assert required.verification_failures == ()

    assert optional.manager_verification_required is False
    assert optional.manager_verification_checked is True
    assert optional.manager_verification_satisfied is True
    assert optional.verification_failures == ()

    assert toolless.manager_verification_required is False
    assert toolless.manager_verification_checked is False
    assert toolless.manager_verification_satisfied is True
    assert toolless.verification_failures == ()


def test_a_failed_verification_is_recorded_on_the_row(tmp_path):
    def answer_without_tools(job, registry):
        text = sample_case.manager_response_text(
            clause_status=ClauseStatus.PRESENT,
            decision=Decision.ESCALATE,
            evidence_ids=(fixture.TARGET_PARAGRAPH_ID,),
        )
        return {"manager": (text,), "compliance": (text,)}

    scores = run_and_score(tmp_path, behaviour=answer_without_tools)
    row = scores.rows[0]

    assert row.verification_failure is True
    assert "no_tool_use" in row.verification_failures
    assert row.manager_verification_satisfied is False


# --------------------------------------------------------------------------
# Usage and cost
# --------------------------------------------------------------------------


class UsageFactory:
    """A scripted factory that reports the same token counts on every call."""

    def __init__(self, script, usage):
        self._inner = ScriptedClientFactory(script)
        self._usage = usage

    def __call__(self, *, role, job):
        inner = self._inner(role=role, job=job)
        return ScriptedModelClient(
            inner.outputs, repeat_last=True, usage=self._usage
        )


def test_usage_totals_sum_when_every_call_reports(tmp_path):
    plan = fixture.make_plan(
        registry=_registry(), case_ids=(fixture.CASE_A,), condition_ids=("A1V1",)
    )
    factory = UsageFactory(
        fixture.script_for_plan(plan, _registry()),
        TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )
    scores = run_and_score(tmp_path, factory=factory)
    row = scores.rows[0]

    assert row.model_calls == 6
    assert row.prompt_tokens == 600
    assert row.completion_tokens == 120
    assert row.total_tokens == 720
    assert not any("reported no value" in note for note in row.notes)


def test_usage_totals_are_all_or_nothing(tmp_path):
    """A partial total looks like a measurement and is not one."""
    from pilot01.experiment.scoring import _sum_or_none

    notes: list[str] = []
    assert _sum_or_none([1, None, 3], "prompt_tokens", notes) is None
    assert notes and "1 of 3" in notes[0]

    notes = []
    assert _sum_or_none([], "prompt_tokens", notes) is None
    assert notes == []

    notes = []
    assert _sum_or_none([1, 2, 3], "prompt_tokens", notes) == 6
    assert notes == []


def test_a_scripted_run_without_usage_reports_none_and_says_so(tmp_path):
    scores = run_and_score(tmp_path)
    row = scores.rows[0]

    assert row.prompt_tokens is None
    assert row.total_tokens is None
    assert any("reported no value" in note for note in row.notes)


def test_cost_is_none_without_a_pricing_table(tmp_path):
    scores = run_and_score(tmp_path)
    row = scores.rows[0]

    assert row.estimated_cost_usd is None
    assert any("no pricing table was supplied" in note for note in row.notes)


def test_cost_is_computed_when_prices_and_tokens_are_both_supplied(tmp_path):
    plan = fixture.make_plan(
        registry=_registry(), case_ids=(fixture.CASE_A,), condition_ids=("A1V1",)
    )
    factory = UsageFactory(
        fixture.script_for_plan(plan, _registry()),
        TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    # Priced by the id the run actually used, which comes from the models
    # config rather than from the client -- the point of a versioned table.
    # Read from that config rather than written out here: the id in
    # config/models_v1.yaml is a development setting that changes when the
    # pilot's model does, and a literal here would make this test pass while
    # silently pricing nothing.
    model_id = fixture.models().for_role("manager").model_id
    pricing = PricingTable(
        pricing_version="test-1",
        input_per_million={model_id: 3.0},
        output_per_million={model_id: 15.0},
    )
    scores = run_and_score(tmp_path, factory=factory, pricing=pricing)
    row = scores.rows[0]

    assert row.estimated_cost_usd == pytest.approx(6 * (3.0 + 15.0), rel=1e-6)
    assert not any("cost" in note for note in row.notes)


def test_pricing_refuses_to_guess():
    table = PricingTable(pricing_version="test-1")

    assert table.estimate(model_id="m", prompt_tokens=10, completion_tokens=5) is None
    assert table.estimate(model_id="m", prompt_tokens=None, completion_tokens=5) is None
    assert table.estimate(model_id="m", prompt_tokens=10, completion_tokens=None) is None

    priced = PricingTable(
        pricing_version="test-1",
        input_per_million={"m": 1.0},
        output_per_million={"m": 1.0},
    )
    assert priced.estimate(model_id="other", prompt_tokens=10, completion_tokens=5) is None
    assert priced.estimate(model_id="m", prompt_tokens=10, completion_tokens=5) == pytest.approx(
        15 / 1_000_000
    )


def test_a_pricing_table_loads_from_a_versioned_file(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "pricing_version": "2024-06",
                "currency": "USD",
                "input_per_million": {"m": 1.0},
                "output_per_million": {"m": 2.0},
            }
        ),
        encoding="utf-8",
    )

    table = PricingTable.load_json(path)

    assert table.pricing_version == "2024-06"
    assert table.currency == "USD"


def test_a_missing_pricing_table_is_reported(tmp_path):
    with pytest.raises(ScoringError, match="no pricing table"):
        PricingTable.load_json(tmp_path / "absent.json")


# --------------------------------------------------------------------------
# The experiment-level result
# --------------------------------------------------------------------------


def test_scoring_is_a_function_of_the_raw_tree_alone(tmp_path):
    """§10: processed results are rebuilt from raw artifacts, not from the plan."""
    plan = fixture.make_plan(registry=_registry(), condition_ids=("A1V1",))
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    fixture.execute(plan, _registry(), paths)

    first = score_experiment(paths.raw_dir, registry=_registry())
    second = score_experiment(paths.raw_dir, registry=_registry())

    assert first == second
    assert len(first) == len(plan.jobs)
    assert first.experiment_id == "EXP-1"
    assert first.plan_fingerprints == (plan.fingerprint(),)


def test_rows_are_sorted_by_run_id(tmp_path):
    plan = fixture.make_plan(registry=_registry(), condition_ids=("A1V1",))
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    fixture.execute(plan, _registry(), paths)

    scores = score_experiment(paths.raw_dir, registry=_registry())

    assert [row.run_id for row in scores.rows] == sorted(row.run_id for row in scores.rows)


def test_scoring_a_tree_that_has_not_been_run_yields_no_rows(tmp_path):
    scores = score_experiment(tmp_path / "absent", registry=_registry())

    assert len(scores) == 0
    assert scores.experiment_id == ""


def test_scores_round_trip_through_jsonl(tmp_path):
    scores = run_and_score(tmp_path)
    path = scores.write_jsonl(tmp_path / "run_scores.jsonl")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(scores)
    from pilot01.experiment import RunScore

    restored = [RunScore.model_validate_json(line) for line in lines]
    assert tuple(restored) == scores.rows


def test_the_csv_has_one_row_per_run_and_columns_from_the_model(tmp_path):
    scores = run_and_score(tmp_path)
    path = scores.write_csv(tmp_path / "run_scores.csv")

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == len(scores)
    assert [row["run_id"] for row in rows] == [row.run_id for row in scores.rows]
    # Flattened from the model rather than hand-listed, so a new field appears.
    assert "manager_evidence_overlaps_gold" in rows[0]
    assert rows[0]["manager_evidence_overlaps_gold"] in {"True", "False", ""}
    # §6's named columns, and the nested assessments flattened alongside them.
    for column in (
        "final_action_correct",
        "manager_clause_status_correct",
        "compliance_clause_status_correct",
        "manager_corrected_with_evidence",
        "compliance_corrected_with_evidence",
        "false_escalation",
    ):
        assert column in rows[0], f"{column} is missing from the CSV"
    # `correct` is the short name for final_action_correct, and a property is
    # not a column -- two columns for one number is one too many.
    assert "correct" not in rows[0]


def test_by_run_id_and_of_cell_select_the_right_rows(tmp_path):
    scores = run_and_score(tmp_path)
    first = scores.rows[0]

    assert scores.by_run_id(first.run_id) == first
    assert len(scores.of_cell(
        case_id=fixture.CASE_A,
        error_condition=first.error_condition,
        condition_id=first.condition_id,
    )) == 1

    with pytest.raises(ScoringError):
        scores.by_run_id("EXP-1__NOPE")
