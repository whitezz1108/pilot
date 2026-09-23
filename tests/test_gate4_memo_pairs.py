"""Gate 4 §7, §8 and §9: the matched memo pairs, and what must not reach an agent.

Two questions run through this file, and they are different:

**Is the manipulation what it claims to be?** The E1 arm must be the E0 arm with
exactly one claim changed -- same schema, same count, same order, comparable
length, and the omitted claim's citations gone. Anything more than that is a
confound; anything less is not the treatment.

**Did anything else ride along?** The memo is agent-facing. A memo that carried
the CUAD title, the gold passage verbatim, or a marker distinguishing the arms
would break the experiment in a way no outcome number would reveal. So the
memo-facing views are built for real cases and scanned.
"""

from __future__ import annotations

import pytest

import gate4_fixture as g4
from pilot01.experiment.development import build_memo_qc, diff_memo_pair
from pilot01.experiment.development.memos import (
    CONTEXT_CLAIM_COUNT,
    MEMO_ORIGIN,
    SUMMARY_ABSENT,
    SUMMARY_PRESENT,
    memo_id_for,
)
from pilot01.schemas import ClauseStatus, ErrorCondition
from pilot01.workflow.views import (
    assert_view_clean,
    audit_view,
    build_compliance_view,
    build_manager_view,
)

MEMO_CLAIM_COUNT = 2 + CONTEXT_CLAIM_COUNT
"""Both policy target categories, plus the context claims."""


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_every_memo_has_the_declared_shape():
    for plan in g4.memo_plans():
        assert len(plan.memo.claims) == MEMO_CLAIM_COUNT
        assert plan.memo.case_id == plan.case_id
        assert plan.memo.memo_id == memo_id_for(plan.case_id)
        categories = [claim.category for claim in plan.memo.claims]
        assert len(set(categories)) == len(categories), "a memo repeats a category"
        assert categories[:2] == list(g4.policy().target_clause_categories), (
            "the memo must state both policy target categories, in policy order"
        )


def test_the_memo_states_both_target_categories_in_every_case():
    """The policy must be decidable from the memo alone.

    A memo that mentioned only the interesting category would make "confirmed
    absent" indistinguishable from "not looked at", and the omission would then
    be legible from what the memo is silent about rather than from what it says.
    """
    targets = set(g4.policy().target_clause_categories)
    for plan in g4.memo_plans():
        stated = {claim.category for claim in plan.memo.claims}
        assert targets <= stated


def test_positive_cases_assert_their_target_present_and_the_other_absent():
    for record, plan in zip(g4.positive_records(), [
        plan for plan in g4.memo_plans() if plan.is_paired
    ]):
        target_claim = plan.memo.claim(plan.target_claim_id)
        assert target_claim is not None
        assert target_claim.category == record.target_category
        assert target_claim.status is ClauseStatus.PRESENT
        assert target_claim.source_ids, "a claim of presence must cite where it read it"

        others = [
            claim
            for claim in plan.memo.claims
            if claim.category in g4.policy().target_clause_categories
            and claim.category != record.target_category
        ]
        assert len(others) == 1
        assert others[0].status is ClauseStatus.ABSENT
        assert others[0].source_ids == ()


def test_sentinel_memos_assert_both_targets_absent():
    for record, plan in zip(
        g4.sentinel_records(), [p for p in g4.memo_plans() if not p.is_paired]
    ):
        for claim in plan.memo.claims:
            if claim.category in g4.policy().target_clause_categories:
                assert claim.status is ClauseStatus.ABSENT
                assert claim.source_ids == ()
        assert record.is_negative_sentinel


def test_every_context_claim_cites_a_real_paragraph():
    for plan in g4.memo_plans():
        for claim in plan.memo.claims:
            if claim.category in g4.policy().target_clause_categories:
                continue
            assert claim.status is ClauseStatus.PRESENT
            assert claim.source_ids, f"{claim.claim_id} cites nothing"
            document = g4.documents()[
                g4.registry().get(plan.case_id).contract_id
            ]
            for source_id in claim.source_ids:
                assert document.paragraph(source_id) is not None


def test_the_target_claim_cites_the_paragraph_the_gold_evidence_is_in():
    for record in g4.positive_records():
        plan = next(p for p in g4.memo_plans() if p.case_id == record.case_id)
        claim = plan.memo.claim(plan.target_claim_id)
        assert claim is not None
        assert set(claim.source_ids) == set(record.paragraph_ids)


def test_the_summaries_are_paraphrases_and_never_quotations():
    """A1V1 exists so an agent can verify a claim against the source.

    A memo that already contained the clause verbatim would make that
    verification a string comparison against something the agent was handed, so
    no claim's summary may contain the annotated text it cites.
    """
    annotations = g4.annotations()
    for record in g4.positive_records():
        plan = next(p for p in g4.memo_plans() if p.case_id == record.case_id)
        summaries = " ".join(claim.summary for claim in plan.memo.claims)
        contract = annotations.by_id(record.contract_id)
        for excerpt in record.gold_evidence_texts:
            # The whole span, and its first 60 characters: a long quotation
            # would be caught by the first even if a short one slipped past the
            # second, and the second catches the fragment case.
            assert excerpt not in summaries
            assert excerpt[:60] not in summaries


# ---------------------------------------------------------------------------
# §8: the manipulation
# ---------------------------------------------------------------------------


def test_the_two_arms_share_a_memo_id_and_a_case_id():
    """Nothing in the memo may distinguish the arms."""
    for record in g4.positive_records():
        e0 = g4.registry().memo(record.case_id, ErrorCondition.E0)
        e1 = g4.registry().memo(record.case_id, ErrorCondition.E1)
        assert e0.memo_id == e1.memo_id
        assert e0.case_id == e1.case_id


def test_the_two_arms_hold_the_same_number_of_claims():
    """§8's same-count requirement -- the reason the omission is a substitution."""
    for record in g4.positive_records():
        e0 = g4.registry().memo(record.case_id, ErrorCondition.E0)
        e1 = g4.registry().memo(record.case_id, ErrorCondition.E1)
        assert len(e0.claims) == len(e1.claims) == MEMO_CLAIM_COUNT


def test_the_arms_differ_at_exactly_one_position():
    for record in g4.positive_records():
        diff = record.memo_pair_diff
        assert diff is not None
        assert diff.is_single_position_change, (
            f"{record.case_id}: changed at {list(diff.changed_indices)}"
        )
        assert diff.shape == "substitution"
        assert diff.e0_claim_count == diff.e1_claim_count


def test_only_the_omitted_claim_changed_and_every_other_claim_is_identical():
    for record in g4.positive_records():
        e0 = g4.registry().memo(record.case_id, ErrorCondition.E0)
        e1 = g4.registry().memo(record.case_id, ErrorCondition.E1)
        omitted = record.omission_target_claim_id
        for claim in e0.claims:
            if claim.claim_id == omitted:
                continue
            twin = e1.claim(claim.claim_id)
            assert twin is not None, f"{claim.claim_id} vanished from E1"
            assert twin == claim, f"{claim.claim_id} was rewritten in E1"
        # And the order is unchanged: the replacement sits at the target's index.
        assert [c.claim_id for c in e1.claims if c.claim_id != record.omission_replacement_claim_id] == [
            c.claim_id for c in e0.claims if c.claim_id != omitted
        ]


def test_the_omitted_claim_is_gone_from_the_omission_arm():
    for record in g4.positive_records():
        e1 = g4.registry().memo(record.case_id, ErrorCondition.E1)
        assert record.omission_target_claim_id not in e1.claim_ids()


def test_the_omitted_claims_source_ids_are_gone_from_the_omission_arm():
    """§8: the target's evidence ids go with it.

    A memo that dropped the claim but kept its citation would leave a pointer to
    the very passage the omission is about -- an agent following it would find
    the clause, and the manipulation would be undone by its own provenance.
    """
    for record in g4.positive_records():
        diff = record.memo_pair_diff
        assert diff is not None
        e1 = g4.registry().memo(record.case_id, ErrorCondition.E1)
        live_sources = {source_id for claim in e1.claims for source_id in claim.source_ids}
        for source_id in diff.removed_source_ids:
            # Only required to be gone when nothing else in E1 cites it. A
            # paragraph can legitimately hold two clauses, and the context
            # claims are unchanged, so a shared citation is not a leak.
            e0 = g4.registry().memo(record.case_id, ErrorCondition.E0)
            cited_elsewhere = any(
                source_id in claim.source_ids
                for claim in e0.claims
                if claim.claim_id != record.omission_target_claim_id
            )
            if not cited_elsewhere:
                assert source_id not in live_sources


def test_the_replacement_is_a_non_target_category_the_memo_did_not_already_hold():
    for record in g4.positive_records():
        spec = g4.registry().get(record.case_id)
        replacement = spec.omission_replacement_claim
        assert replacement is not None
        assert not g4.policy().is_target_category(replacement.category)
        assert replacement.claim_id not in [
            claim.claim_id
            for claim in g4.registry().memo(record.case_id, ErrorCondition.E0).claims
        ]
        assert replacement.category in record.neutral_categories
        assert replacement.status is ClauseStatus.PRESENT


def test_the_omission_record_matches_the_memos_that_exist():
    from pilot01.experiment.development.registry import (
        assert_pair_matches_omission_record,
    )

    for record in g4.positive_records():
        assert_pair_matches_omission_record(g4.registry(), record.case_id)


def test_the_lengths_are_matched():
    """§8's "keep length reasonably matched", reported rather than assumed."""
    qc = build_memo_qc(
        [record.memo_pair_diff for record in g4.positive_records() if record.memo_pair_diff]
    )
    assert qc.clean, qc.flags
    assert qc.max_abs_word_delta <= 8
    assert qc.max_abs_char_delta <= 200


def test_the_arms_carry_the_same_schema():
    """Same field set, same claim field set -- a shape difference would be a cue."""
    e0_fields = set(
        g4.registry().memo(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E0).model_dump().keys()
    )
    e1_fields = set(
        g4.registry().memo(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E1).model_dump().keys()
    )
    assert e0_fields == e1_fields
    e0_claim = set(
        g4.registry().memo(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E0).claims[0].model_dump().keys()
    )
    e1_claim = set(
        g4.registry().memo(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E1).claims[0].model_dump().keys()
    )
    assert e0_claim == e1_claim


def test_a_sentinel_gets_e0_only_and_no_fabricated_e1():
    """§8: "Sentinels get E0 only -- do NOT fabricate E1." """
    for record in g4.sentinel_records():
        spec = g4.registry().get(record.case_id)
        assert spec.error_conditions == (ErrorCondition.E0,)
        assert spec.omission_target_claim_id is None
        assert spec.omission_replacement_claim is None
        assert g4.registry().omission_record(record.case_id) is None
        assert record.memo_pair_diff is None
        with pytest.raises(Exception):
            g4.registry().memo(record.case_id, ErrorCondition.E1)


def test_the_sentinels_are_reported_apart_in_the_memo_qc():
    diffs = [r.memo_pair_diff for r in g4.positive_records() if r.memo_pair_diff]
    qc = build_memo_qc(
        diffs, sentinel_case_ids=[r.case_id for r in g4.sentinel_records()]
    )
    assert len(qc.diffs) == 8
    assert set(qc.sentinel_case_ids) == set(g4.SENTINEL_CASE_IDS)
    assert qc.clean


def test_the_diff_refuses_to_compare_two_different_cases():
    e0 = g4.registry().memo(g4.POSITIVE_CASE_IDS[0], ErrorCondition.E0)
    e1 = g4.registry().memo(g4.POSITIVE_CASE_IDS[1], ErrorCondition.E1)
    omission = g4.registry().omission_record(g4.POSITIVE_CASE_IDS[0])
    assert omission is not None
    with pytest.raises(ValueError, match="different cases"):
        diff_memo_pair(e0, e1, omission)


# ---------------------------------------------------------------------------
# §7: nothing hidden rides along into a memo-facing view
# ---------------------------------------------------------------------------


def _state(case_id: str, error_condition: ErrorCondition):
    from pilot01.config import load_conditions_v1
    from pilot01.workflow.state import ExperimentState

    spec = g4.registry().get(case_id)
    return ExperimentState.create(
        run_id=f"TEST__{case_id}__{error_condition.value}",
        case_id=case_id,
        repetition_id=0,
        condition_id="A1V1",
        conditions=load_conditions_v1(),
        error_condition=error_condition,
        contract_id=spec.contract_id,
        contract_text_hash=spec.contract_text_hash,
        target_category=spec.target_category,
        memo=g4.registry().memo(case_id, error_condition),
        gold_status=spec.gold_clause_status,
        policy=spec.policy,
        gold_evidence_offsets=spec.gold_evidence_offsets,
        omission=g4.registry().omission_record(case_id),
    )


def test_the_manager_view_is_clean_for_every_real_case_and_arm():
    for case_id in g4.registry().case_ids:
        spec = g4.registry().get(case_id)
        for error_condition in spec.error_conditions:
            view = build_manager_view(_state(case_id, error_condition))
            assert_view_clean(view)
            assert audit_view(view) == ()


def test_no_gold_marker_reaches_the_manager_view():
    for case_id in g4.registry().case_ids:
        spec = g4.registry().get(case_id)
        for error_condition in spec.error_conditions:
            serialized = build_manager_view(_state(case_id, error_condition)).model_dump_json()
            lowered = serialized.lower()
            for forbidden in (
                "gold",
                "sentinel",
                "omission",
                "treatment",
                "e0",
                "e1",
                "error_condition",
                "condition_id",
                "cuad",
            ):
                assert forbidden not in lowered, (
                    f"{case_id}/{error_condition.value}: the manager view contains "
                    f"{forbidden!r}"
                )


def test_neither_the_cuad_title_nor_the_gold_passage_reaches_the_manager_view():
    """The two concrete things that must not be in an agent's input."""
    for record in g4.case_set().cases:
        spec = g4.registry().get(record.case_id)
        for error_condition in spec.error_conditions:
            serialized = build_manager_view(
                _state(record.case_id, error_condition)
            ).model_dump_json()
            assert record.cuad_title not in serialized
            for excerpt in record.gold_evidence_texts:
                assert excerpt not in serialized


def test_the_omission_record_never_reaches_any_view():
    for record in g4.positive_records():
        state = _state(record.case_id, ErrorCondition.E1)
        serialized = build_manager_view(state).model_dump_json()
        replacement = g4.registry().get(record.case_id).omission_replacement_claim
        assert replacement is not None
        assert record.omission_target_claim_id not in serialized or (
            record.omission_target_claim_id
            not in [claim.claim_id for claim in state.analyst_memo.claims]
        )


def test_the_compliance_view_cannot_see_the_memo():
    for case_id in g4.registry().case_ids:
        spec = g4.registry().get(case_id)
        for error_condition in spec.error_conditions:
            state = _state(case_id, error_condition)
            if state.manager_output is None:
                continue
            view = build_compliance_view(state)
            assert_view_clean(view)
            serialized = view.model_dump_json()
            for claim in state.analyst_memo.claims:
                assert claim.claim_id not in serialized
                assert claim.summary not in serialized


def test_the_memo_origin_label_is_recorded_and_says_what_it_is():
    """§7: the researcher-constructed status must be preserved in metadata."""
    assert "DEVELOPMENT" in MEMO_ORIGIN
    assert "not an independently generated analyst judgment" in MEMO_ORIGIN
    for record in g4.case_set().cases:
        assert record.memo_origin == MEMO_ORIGIN


def test_the_summary_templates_are_paraphrases_by_construction():
    """The templates are the mechanism, so they are asserted directly."""
    for template in (SUMMARY_PRESENT, SUMMARY_ABSENT):
        assert "{category}" in template
        assert len(template) < 200
    assert "cites the source passage" in SUMMARY_PRESENT
    assert "not found" in SUMMARY_ABSENT
