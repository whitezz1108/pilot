"""§13: the thirteen deterministic scenarios A–M, scored end to end.

Each scenario is a named function that returns the raw responses for one job, so
what a scenario *is* can be read next to what it is expected to produce. Every
one runs the real workflow through the real runner and is scored by the real
scorer -- nothing here reaches into the scoring layer's internals, because the
question these tests answer is whether the pipeline as a whole turns a described
behaviour into the described row.

Scenarios are pinned to the E1 arm where the arm matters, leaving E0 on the
fixture's default. A scenario describes what the *error* does, and keeping the
correct-memo arm as a control is what makes it visible that the scenario did not
disturb it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
import sample_case

import fixture_registry as fixture
from pilot01.experiment import (
    CorrectionStage,
    EvidenceFailure,
    ExperimentPaths,
    score_experiment,
)
from pilot01.schemas import ClauseStatus, Decision, ErrorCondition, VerificationStatus

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


@pytest.fixture(scope="module")
def registry():
    return _registry()


def score(
    tmp_path: Path,
    behaviour,
    *,
    case_ids=(fixture.CASE_A,),
    condition_ids=("A1V1",),
    error_condition=ErrorCondition.E1,
):
    """Run one batch with the given behaviour and return the single scored row."""
    plan = fixture.make_plan(
        registry=_registry(), case_ids=case_ids, condition_ids=condition_ids
    )
    paths = ExperimentPaths(root=tmp_path, experiment_id="EXP-1")
    fixture.execute(plan, _registry(), paths, behaviour=behaviour)
    rows = [
        row
        for row in score_experiment(paths.raw_dir, registry=_registry())
        if row.error_condition is error_condition
    ]
    assert len(rows) == 1, f"expected one {error_condition.value} row, got {len(rows)}"
    return rows[0]


def e1_only(behaviour):
    """Apply a behaviour to the E1 arm; leave E0 on the fixture's default."""

    def applied(job, registry):
        if job.error_condition is ErrorCondition.E0:
            return fixture.default_responses(job, registry)
        return behaviour(job, registry)

    return applied


def answer_only(
    *,
    role: str,
    status: ClauseStatus,
    decision: Decision,
    evidence_ids: tuple[str, ...],
    adopted: tuple[str, ...] = (),
):
    """A single answer turn for one role, with no tool use.

    Used where the scenario is about what a node concluded from what it was
    given, so tool turns would be noise. Valid under V0, where declining to
    consult the source is a permitted choice.
    """
    text = (
        sample_case.manager_response_text
        if role == "manager"
        else sample_case.compliance_response_text
    )(
        clause_status=status,
        decision=decision,
        evidence_ids=evidence_ids,
        verification_status=VerificationStatus.NOT_CHECKED,
        adopted_upstream_claim_ids=adopted,
    )
    return (text,)


def with_role(script: dict, role: str, turns: tuple[str, ...]) -> dict:
    return {**script, role: turns}


# --------------------------------------------------------------------------
# The scenarios
# --------------------------------------------------------------------------

# A. E0 correct memo: both stages right, correct action. The fixture's default
#    behaviour, which is why A and C are the same function under different arms.
scenario_a = fixture.default_responses
scenario_c = fixture.default_responses


def scenario_b(job, registry):
    """E1 omission survives both stages: Manager misses, Compliance misses."""
    return fixture.scripted_responses(
        source_access=job.source_access,
        verification_required=job.verification_required,
        paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
        manager_status=ClauseStatus.ABSENT,
        manager_decision=Decision.ACCEPT,
        compliance_status=ClauseStatus.ABSENT,
        compliance_decision=Decision.ACCEPT,
    )


def scenario_d(job, registry):
    """E1 missed by the Manager, corrected by Compliance."""
    case = registry.get(job.case_id)
    return fixture.scripted_responses(
        source_access=job.source_access,
        verification_required=job.verification_required,
        paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
        manager_status=ClauseStatus.ABSENT,
        manager_decision=Decision.ACCEPT,
        compliance_status=case.gold_status_for(case.target_category),
        compliance_decision=case.gold_action,
    )


def scenario_e(job, registry):
    """E1: both agents say UNKNOWN and escalate defensively.

    The action matches gold; the fact was never recovered. The spec's headline
    example of a run that must not be described as a successful correction.
    """
    return fixture.scripted_responses(
        source_access=job.source_access,
        verification_required=job.verification_required,
        paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
        manager_status=ClauseStatus.UNKNOWN,
        manager_decision=Decision.ESCALATE,
        compliance_status=ClauseStatus.UNKNOWN,
        compliance_decision=Decision.ESCALATE,
    )


def scenario_f(job, registry):
    """The Manager cites a paragraph a search returned and never opened."""
    return fixture.scripted_responses(
        source_access=True,
        verification_required=job.verification_required,
        paragraph_id=fixture.TARGET_PARAGRAPH_ID,
        manager_status=ClauseStatus.PRESENT,
        manager_decision=Decision.ESCALATE,
        compliance_status=ClauseStatus.PRESENT,
        compliance_decision=Decision.ESCALATE,
        manager_evidence=(fixture.LAW_PARAGRAPH_ID,),
    )


def scenario_g(job, registry):
    """Compliance cites what the Manager found, without opening it itself."""
    script = fixture.scripted_responses(
        source_access=True,
        verification_required=False,
        paragraph_id=fixture.TARGET_PARAGRAPH_ID,
        manager_status=ClauseStatus.PRESENT,
        manager_decision=Decision.ESCALATE,
        compliance_status=ClauseStatus.PRESENT,
        compliance_decision=Decision.ESCALATE,
    )
    return with_role(
        script,
        "compliance",
        answer_only(
            role="compliance",
            status=ClauseStatus.PRESENT,
            decision=Decision.ESCALATE,
            evidence_ids=(fixture.TARGET_PARAGRAPH_ID,),
        ),
    )


def scenario_h(job, registry):
    """The negative sentinel, correctly accepted. The fixture's default."""
    return fixture.default_responses(job, registry)


def scenario_i(job, registry):
    """The negative sentinel, falsely escalated: no clause found, escalate anyway."""
    return fixture.scripted_responses(
        source_access=False,
        verification_required=False,
        paragraph_id=fixture.TARGET_PARAGRAPH_ID,
        manager_status=ClauseStatus.ABSENT,
        manager_decision=Decision.ESCALATE,
        compliance_status=ClauseStatus.ABSENT,
        compliance_decision=Decision.ESCALATE,
    )


def scenario_j(job, registry):
    """The Manager never produces a parseable object, even after its repair."""
    return {
        "manager": ("not JSON at all", "still not JSON"),
        "compliance": ("unused",),
    }


def scenario_k(job, registry):
    """The Manager is fine; Compliance never produces a parseable object."""
    return with_role(
        fixture.default_responses(job, registry),
        "compliance",
        ("not JSON", "still not JSON"),
    )


def scenario_l(job, registry):
    """The Manager's answer is malformed once, and the repair succeeds."""
    script = fixture.default_responses(job, registry)
    turns = script["manager"]
    return with_role(
        script, "manager", (*turns[:-1], "I think the clause is present.", turns[-1])
    )


def scenario_m(job, registry):
    """The Manager's answer is malformed, and the repair is malformed too."""
    script = fixture.default_responses(job, registry)
    turns = script["manager"]
    return with_role(script, "manager", (*turns[:-1], "not JSON", "still not JSON"))


# --------------------------------------------------------------------------
# A. E0 correct memo
# --------------------------------------------------------------------------


def test_scenario_a_correct_memo_is_a_correct_run(tmp_path):
    row = score(tmp_path, scenario_a, error_condition=ErrorCondition.E0)

    assert row.gold_status is ClauseStatus.PRESENT
    assert row.gold_action is Decision.ESCALATE
    assert row.manager_status is ClauseStatus.PRESENT
    assert row.compliance_status is ClauseStatus.PRESENT
    assert row.final_decision is Decision.ESCALATE
    assert row.final_action_correct is True
    assert row.manager_corrected_with_evidence is True
    assert row.compliance_corrected_with_evidence is True
    assert row.correction_stage is CorrectionStage.ALREADY_CORRECT
    assert row.error_survival_manager is None
    assert row.error_survival_compliance is None
    assert row.completed is True


# --------------------------------------------------------------------------
# B. E1 omission survives both stages
# --------------------------------------------------------------------------


def test_scenario_b_the_omission_survives_and_produces_a_wrong_accept(tmp_path):
    row = score(tmp_path, e1_only(scenario_b))

    assert row.gold_status is ClauseStatus.PRESENT
    assert row.manager_status is ClauseStatus.ABSENT
    assert row.compliance_status is ClauseStatus.ABSENT
    assert row.final_decision is Decision.ACCEPT
    assert row.final_action_correct is False
    assert row.error_survival_manager is True
    assert row.error_survival_compliance is True
    assert row.correction_stage is CorrectionStage.NEVER
    # Both stages opened the contract and still concluded absence -- which is
    # the phenomenon, not a protocol problem.
    assert row.manager_evidence.failure is None
    assert row.compliance_evidence.failure is None
    assert row.protocol_failure is False


# --------------------------------------------------------------------------
# C. E1 corrected by the Manager
# --------------------------------------------------------------------------


def test_scenario_c_the_manager_corrects_the_omission_itself(tmp_path):
    row = score(tmp_path, scenario_c)

    assert row.manager_status is ClauseStatus.PRESENT
    assert row.compliance_status is ClauseStatus.PRESENT
    assert row.final_action_correct is True
    assert row.error_survival_manager is False
    assert row.correction_stage is CorrectionStage.MANAGER
    assert row.manager_evidence.self_opened == (fixture.TARGET_PARAGRAPH_ID,)
    assert row.manager_evidence.overlaps_gold is True
    assert row.manager_corrected_with_evidence is True


# --------------------------------------------------------------------------
# D. E1 corrected by Compliance
# --------------------------------------------------------------------------


def test_scenario_d_compliance_recovers_what_the_manager_lost(tmp_path):
    row = score(tmp_path, e1_only(scenario_d))

    assert row.manager_status is ClauseStatus.ABSENT
    assert row.compliance_status is ClauseStatus.PRESENT
    assert row.error_survival_manager is True
    assert row.error_survival_compliance is False
    assert row.correction_stage is CorrectionStage.COMPLIANCE
    assert row.final_action_correct is True
    assert row.compliance_corrected_with_evidence is True
    # The Manager's own failure is still visible in its own column.
    assert row.manager_clause_status_correct is False


# --------------------------------------------------------------------------
# E. Correct action, unrecovered fact
# --------------------------------------------------------------------------


def test_scenario_e_a_defensive_escalation_is_not_a_correction(tmp_path):
    row = score(tmp_path, e1_only(scenario_e))

    assert row.gold_action is Decision.ESCALATE
    assert row.final_decision is Decision.ESCALATE
    assert row.final_action_correct is True

    assert row.manager_status is ClauseStatus.UNKNOWN
    assert row.manager_clause_status_correct is False
    assert row.compliance_clause_status_correct is False
    assert row.manager_corrected_with_evidence is False
    assert row.compliance_corrected_with_evidence is False
    assert any("target_not_recovered" in note for note in row.notes)


# --------------------------------------------------------------------------
# F. Cited from a search result but never opened
# --------------------------------------------------------------------------


def test_scenario_f_a_search_result_cited_without_being_opened_is_not_evidence(tmp_path):
    """The id came back from a search the agent ran, and was never read.

    Reachable only without verification: under A1V1 the runtime refuses the
    citation and the run fails instead of being scored, which is the correct
    behaviour and is asserted separately below.
    """
    row = score(tmp_path, scenario_f, condition_ids=("A1V0",))

    assert row.manager_evidence.cited == (fixture.LAW_PARAGRAPH_ID,)
    assert row.manager_evidence.self_opened == ()
    assert row.manager_evidence.failure is EvidenceFailure.NOT_SELF_OPENED
    assert row.manager_corrected_with_evidence is False
    assert row.manager_searches == 1


def test_scenario_f_under_v1_the_same_behaviour_is_a_protocol_failure(tmp_path):
    row = score(tmp_path, scenario_f)

    assert row.protocol_failure is True
    assert row.failure_stage == "manager"
    assert "evidence_not_opened" in row.verification_failures
    assert row.final_action_correct is None


# --------------------------------------------------------------------------
# G. Compliance inherits the Manager's evidence
# --------------------------------------------------------------------------


def test_scenario_g_upstream_cited_evidence_is_not_independent_verification(tmp_path):
    """§6's Case B, and the SELF_OPENED / UPSTREAM_CITED distinction.

    The Manager does the retrieval. Compliance cites the same paragraph without
    ever opening it, so its evidence column records a citation it did not earn.
    """
    row = score(tmp_path, scenario_g, condition_ids=("A1V0",))

    # The Manager did verify, and earned it.
    assert row.manager_evidence.self_opened == (fixture.TARGET_PARAGRAPH_ID,)
    assert row.manager_corrected_with_evidence is True
    # Compliance cites the right paragraph and opened nothing.
    assert row.compliance_evidence.cited == (fixture.TARGET_PARAGRAPH_ID,)
    assert row.compliance_evidence.self_opened == ()
    assert row.compliance_evidence.failure is EvidenceFailure.NOT_SELF_OPENED
    assert row.compliance_corrected_with_evidence is False
    assert row.compliance_opens == 0
    assert row.compliance_searches == 0
    assert row.compliance_self_opened_count == 0
    # And the two columns disagree about the same run, which is the point.
    assert row.compliance_clause_status_correct is True


def test_scenario_g_adopting_a_claim_from_the_handoff_is_permitted(tmp_path):
    """Adoption is not itself a finding. Only adopting what it never saw is.

    The claim used is one the E1 memo still carries: adopting the *deleted* claim
    is a different act, and the scoring suite asserts that case separately.
    """
    handed = fixture.NON_TARGET_CLAIM[fixture.CASE_A]

    def adopts(job, registry):
        script = fixture.scripted_responses(
            source_access=True,
            verification_required=False,
            paragraph_id=fixture.TARGET_PARAGRAPH_ID,
            manager_status=ClauseStatus.PRESENT,
            manager_decision=Decision.ESCALATE,
            compliance_status=ClauseStatus.PRESENT,
            compliance_decision=Decision.ESCALATE,
            manager_adopted=(handed,),
        )
        return with_role(
            script,
            "compliance",
            answer_only(
                role="compliance",
                status=ClauseStatus.PRESENT,
                decision=Decision.ESCALATE,
                evidence_ids=(fixture.TARGET_PARAGRAPH_ID,),
                adopted=(handed,),
            ),
        )

    row = score(tmp_path, adopts, condition_ids=("A1V0",))

    assert row.manager_adopted_claims == (handed,)
    assert row.compliance_adopted_claims == (handed,)
    assert row.unknown_adopted_claims == ()


# --------------------------------------------------------------------------
# H. The negative sentinel, correctly accepted
# --------------------------------------------------------------------------


def test_scenario_h_the_sentinel_is_correctly_accepted(tmp_path):
    row = score(
        tmp_path,
        scenario_h,
        case_ids=(fixture.CASE_C,),
        condition_ids=("A1V0",),
        error_condition=ErrorCondition.E0,
    )

    assert row.is_negative_sentinel is True
    assert row.gold_status is ClauseStatus.ABSENT
    assert row.gold_action is Decision.ACCEPT
    assert row.final_decision is Decision.ACCEPT
    assert row.final_action_correct is True
    assert row.false_escalation is False
    assert row.manager_clause_status_correct is True
    # No gold span exists, so evidence overlap is not a question that can be asked.
    assert row.manager_evidence.failure is EvidenceFailure.NO_GOLD_SPAN
    assert row.manager_evidence.overlaps_gold is None
    assert row.manager_corrected_with_evidence is None


# --------------------------------------------------------------------------
# I. The negative sentinel, falsely escalated
# --------------------------------------------------------------------------


def test_scenario_i_the_sentinel_is_falsely_escalated(tmp_path):
    row = score(
        tmp_path,
        scenario_i,
        case_ids=(fixture.CASE_C,),
        condition_ids=("A1V0",),
        error_condition=ErrorCondition.E0,
    )

    assert row.is_negative_sentinel is True
    assert row.final_decision is Decision.ESCALATE
    assert row.gold_action is Decision.ACCEPT
    assert row.final_action_correct is False
    assert row.false_escalation is True
    # The fact was right and the action was not, so the two columns split.
    assert row.compliance_clause_status_correct is True


# --------------------------------------------------------------------------
# J. Manager protocol failure
# --------------------------------------------------------------------------


def test_scenario_j_a_manager_protocol_failure_leaves_the_run_unscored(tmp_path):
    row = score(tmp_path, scenario_j)

    assert row.completed is False
    assert row.run_status.value == "failed"
    assert row.protocol_failure is True
    assert row.model_output_failure is True
    assert row.failure_stage == "manager"

    # Nothing downstream was reached, and nothing is guessed.
    assert row.manager_status is None
    assert row.compliance_status is None
    assert row.final_decision is None
    assert row.final_action_correct is None
    assert row.manager_clause_status_correct is None
    assert row.manager_corrected_with_evidence is None
    assert row.error_survival_manager is None
    assert row.error_survival_compliance is None
    assert row.correction_stage is CorrectionStage.UNAVAILABLE
    assert row.manager_evidence.available is False
    assert row.manager_evidence.failure is EvidenceFailure.OUTPUT_UNAVAILABLE
    assert row.compliance_model_calls == 0

    # The failed run is still a row: §7's "a failed run is data".
    assert row.run_id


# --------------------------------------------------------------------------
# K. Compliance protocol failure
# --------------------------------------------------------------------------


def test_scenario_k_a_compliance_protocol_failure_keeps_the_manager_scoreable(tmp_path):
    row = score(tmp_path, scenario_k)

    assert row.protocol_failure is True
    assert row.model_output_failure is True
    assert row.failure_stage == "compliance"

    # The Manager's half ran and is scored on its own terms.
    assert row.manager_status is ClauseStatus.PRESENT
    assert row.manager_clause_status_correct is True
    assert row.manager_corrected_with_evidence is True
    assert row.manager_evidence.failure is None
    assert row.error_survival_manager is False
    assert row.manager_model_calls > 0

    # The Compliance half produced nothing, and is unavailable rather than wrong.
    assert row.compliance_status is None
    assert row.compliance_clause_status_correct is None
    assert row.final_decision is None
    assert row.final_action_correct is None
    assert row.error_survival_compliance is None
    assert row.compliance_evidence.failure is EvidenceFailure.OUTPUT_UNAVAILABLE
    assert row.correction_stage is CorrectionStage.UNAVAILABLE


# --------------------------------------------------------------------------
# L. Format repair succeeds
# --------------------------------------------------------------------------


def test_scenario_l_one_malformed_response_is_repaired_and_the_run_completes(tmp_path):
    row = score(tmp_path, scenario_l)

    assert row.completed is True
    assert row.protocol_failure is False
    assert row.format_repairs == 1
    assert row.model_output_failure is False
    # The repaired run scores exactly as an unbroken one would.
    assert row.manager_status is ClauseStatus.PRESENT
    assert row.final_action_correct is True
    assert row.manager_model_calls == 5  # two searches, open, malformed, repair


# --------------------------------------------------------------------------
# M. Format repair exhausted
# --------------------------------------------------------------------------


def test_scenario_m_a_second_malformed_response_fails_the_run(tmp_path):
    row = score(tmp_path, scenario_m)

    assert row.completed is False
    assert row.protocol_failure is True
    assert row.model_output_failure is True
    # Exactly one repair was attempted, and no more.
    assert row.format_repairs == 1
    assert row.manager_model_calls == 5  # two searches, open, attempt, one repair
    assert row.final_action_correct is None
    assert row.manager_clause_status_correct is None


# --------------------------------------------------------------------------
# §13's closing requirement: deterministic and offline
# --------------------------------------------------------------------------

#: The scenarios that can be pinned to a single arm without changing what they
#: demonstrate. F and G need V0 (V1 refuses them), H and I need the sentinel.
SCORED_SCENARIOS = {
    "A": (scenario_a, ErrorCondition.E0, (fixture.CASE_A,), ("A1V1",)),
    "B": (e1_only(scenario_b), ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "C": (scenario_c, ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "D": (e1_only(scenario_d), ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "E": (e1_only(scenario_e), ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "F": (scenario_f, ErrorCondition.E1, (fixture.CASE_A,), ("A1V0",)),
    "G": (scenario_g, ErrorCondition.E1, (fixture.CASE_A,), ("A1V0",)),
    # The sentinel declares the correct-memo arm alone: it has no claim to omit.
    "H": (scenario_h, ErrorCondition.E0, (fixture.CASE_C,), ("A1V0",)),
    "I": (scenario_i, ErrorCondition.E0, (fixture.CASE_C,), ("A1V0",)),
    "J": (scenario_j, ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "K": (scenario_k, ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "L": (scenario_l, ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
    "M": (scenario_m, ErrorCondition.E1, (fixture.CASE_A,), ("A1V1",)),
}


@pytest.mark.parametrize("name", sorted(SCORED_SCENARIOS))
def test_every_scenario_is_reproducible(tmp_path, name):
    """Two runs of the same behaviour produce the identical row.

    This is what makes a scenario a fixture rather than an anecdote, and it is
    the property the whole gate rests on: if a scenario were not reproducible,
    neither would the pilot.
    """
    behaviour, error_condition, case_ids, condition_ids = SCORED_SCENARIOS[name]

    first = score(
        tmp_path / "first",
        behaviour,
        case_ids=case_ids,
        condition_ids=condition_ids,
        error_condition=error_condition,
    )
    second = score(
        tmp_path / "second",
        behaviour,
        case_ids=case_ids,
        condition_ids=condition_ids,
        error_condition=error_condition,
    )

    assert first == second, f"scenario {name} is not deterministic"


def test_the_scenario_table_covers_all_thirteen():
    """A scenario dropped from the table would silently stop being re-run."""
    assert sorted(SCORED_SCENARIOS) == list("ABCDEFGHIJKLM")
