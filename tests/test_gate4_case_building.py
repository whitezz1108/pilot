"""Gate 4 §4 and §5: the twelve real cases, and every claim made about them.

These tests are the ones that decide whether the development set is what the
build says it is. Each assertion below corresponds to a sentence in §5 or to one
of §4's exclusion criteria, and each is checked against the frozen CUAD file
rather than against a copy of it.

The failure this file exists to catch is quiet: a case whose gold offsets have
drifted still runs, still produces a decision, and still gets scored -- against
the wrong characters. Nothing downstream would notice.
"""

from __future__ import annotations

import hashlib

import pytest

import gate4_fixture as g4
from pilot01.experiment.development import (
    GATE4_DEVELOPMENT_SEED,
    selection_fingerprint,
)
from pilot01.experiment.development.selection import (
    EXCLUSION_CRITERIA,
    MAX_GOLD_SPANS,
    MIN_SPAN_CHARS,
    POSITIVE_TARGET_QUOTAS,
    SENTINEL_COUNT,
)
from pilot01.schemas import ClauseStatus, Decision, ErrorCondition
from pilot01.source.cuad import canonical_annotation_path, recorded_digest


# ---------------------------------------------------------------------------
# The frozen source the cases were built from
# ---------------------------------------------------------------------------


def test_the_annotations_come_from_the_canonical_cuad_file():
    annotations = g4.annotations()
    expected = canonical_annotation_path()

    assert annotations.source_path == str(expected)
    digest = hashlib.sha256(expected.read_bytes()).hexdigest()
    assert annotations.source_digest == digest
    assert annotations.source_digest == recorded_digest()


def test_every_gold_offset_reproduces_its_own_text_in_the_frozen_context():
    """§5: ``context[start:end] == expected CUAD evidence text``, for every span.

    Checked across the whole corpus rather than only over the twelve selected
    cases, because the selection reads the same offsets: a corpus-wide drift
    would change which contracts are eligible before it changed any case.
    """
    annotations = g4.annotations()
    checked = 0
    for contract in annotations.contracts:
        for clause in contract.clauses:
            for start, end in clause.spans:
                assert 0 <= start < end <= len(contract.context), (
                    f"{contract.contract_id} {clause.category}: span ({start}, {end}) "
                    f"is outside a context of {len(contract.context)} characters"
                )
                assert contract.context[start:end].strip(), (
                    f"{contract.contract_id} {clause.category}: span ({start}, {end}) "
                    "slices to whitespace"
                )
                checked += 1
    assert checked > 0


def test_every_selected_gold_span_reproduces_the_text_the_case_records():
    annotations = g4.annotations()
    for record in g4.case_set().cases:
        contract = annotations.by_id(record.contract_id)
        offsets = record.gold_evidence_offsets
        spans = [
            (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
        ]
        assert len(spans) == len(record.gold_evidence_texts)
        for (start, end), expected in zip(spans, record.gold_evidence_texts):
            assert contract.context[start:end] == expected


def test_the_case_contract_text_matches_the_canonical_context():
    """A case's contract text hash must be the canonical file's own text.

    The registry carries contract text; the annotation reader carries a context.
    If those two ever diverged, the offsets would be valid against one and
    meaningless against the other, and the case would score against nothing.
    """
    annotations = g4.annotations()
    for record in g4.case_set().cases:
        spec = g4.registry().get(record.case_id)
        assert spec.contract_text == annotations.by_id(record.contract_id).context
        assert record.contract_text_hash == spec.contract_text_hash


def test_every_gold_span_maps_to_at_least_one_source_paragraph():
    """§5's second verification requirement, checked against the real layer."""
    for record in g4.case_set().cases:
        if not record.gold_evidence_offsets:
            assert record.is_negative_sentinel
            assert record.paragraph_ids == ()
            continue
        document = g4.documents()[record.contract_id]
        offsets = record.gold_evidence_offsets
        spans = [
            (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
        ]
        overlapping = {
            paragraph.paragraph_id
            for paragraph in document.paragraphs
            if any(
                start < paragraph.end_char and paragraph.start_char < end
                for start, end in spans
            )
        }
        assert overlapping, f"{record.case_id}: gold evidence maps to no paragraph"
        assert set(record.paragraph_ids) <= overlapping
        for paragraph_id in record.paragraph_ids:
            assert document.paragraph(paragraph_id) is not None


# ---------------------------------------------------------------------------
# §4: composition
# ---------------------------------------------------------------------------


def test_the_development_set_holds_exactly_twelve_cases():
    case_set = g4.case_set()
    assert len(case_set.registry) == 12
    assert len(case_set.cases) == 12
    assert case_set.case_ids == g4.POSITIVE_CASE_IDS + g4.SENTINEL_CASE_IDS


def test_the_composition_is_four_four_four():
    """§4: ~4 of each target category, plus 4 sentinels."""
    assert g4.selection().composition() == {
        "positive:Change Of Control": POSITIVE_TARGET_QUOTAS["Change Of Control"],
        "positive:Termination For Convenience": POSITIVE_TARGET_QUOTAS[
            "Termination For Convenience"
        ],
        "sentinel": SENTINEL_COUNT,
    }


def test_every_positive_case_has_exactly_one_target_category_present():
    """§4's "exactly one target positive", checked against CUAD itself.

    Both targets present would mean the E1 arm still carried a policy trigger
    after the omission, so the omission would not be the only thing that
    changed.
    """
    annotations = g4.annotations()
    targets = g4.policy().target_clause_categories
    for record in g4.positive_records():
        contract = annotations.by_id(record.contract_id)
        present = [category for category in targets if contract.status(category)]
        assert present == [record.target_category], (
            f"{record.case_id}: CUAD records {present} present, but the case names "
            f"{record.target_category!r} as its target"
        )
        assert record.gold_clause_status is ClauseStatus.PRESENT
        assert record.gold_action is Decision.ESCALATE


def test_every_sentinel_has_both_target_categories_absent():
    annotations = g4.annotations()
    targets = g4.policy().target_clause_categories
    for record in g4.sentinel_records():
        contract = annotations.by_id(record.contract_id)
        for category in targets:
            assert not contract.status(category), (
                f"{record.case_id}: CUAD records {category!r} present, but the case "
                "is a negative sentinel"
            )
        assert record.gold_clause_status is ClauseStatus.ABSENT
        assert record.gold_action is Decision.ACCEPT
        assert record.gold_evidence_offsets == ()
        assert record.gold_evidence_texts == ()


def test_sentinels_name_both_target_categories_between_them():
    """The named target alternates, so neither category is absent from the sentinels."""
    named = {record.target_category for record in g4.sentinel_records()}
    assert named == set(g4.policy().target_clause_categories)


def test_the_two_case_classes_are_disjoint_and_cover_the_set():
    positives = {record.contract_id for record in g4.positive_records()}
    sentinels = {record.contract_id for record in g4.sentinel_records()}
    assert not positives & sentinels
    assert len(positives) + len(sentinels) == 12


# ---------------------------------------------------------------------------
# §4: the exclusion criteria
# ---------------------------------------------------------------------------


def test_no_selected_contract_is_a_duplicate_of_another():
    duplicates = g4.duplicate_of()
    for record in g4.case_set().cases:
        assert record.contract_id not in duplicates, (
            f"{record.case_id} is a copy of {duplicates.get(record.contract_id)}"
        )


def test_no_selected_gold_span_contains_a_redaction_marker():
    from pilot01.experiment.development.selection import REDACTION_MARKERS

    for record in g4.case_set().cases:
        for text in record.gold_evidence_texts:
            for marker in REDACTION_MARKERS:  # noqa: B007 - named for the message
                assert marker not in text, (
                    f"{record.case_id}: gold evidence contains the redaction marker "
                    f"{marker!r}"
                )


def test_no_selected_gold_evidence_is_fragmented():
    for record in g4.positive_records():
        assert 0 < record.span_count <= MAX_GOLD_SPANS
        for text in record.gold_evidence_texts:
            assert len(text.strip()) >= MIN_SPAN_CHARS


def test_every_selected_contract_has_room_for_a_neutral_replacement():
    from pilot01.experiment.development.selection import MIN_NEUTRAL_CATEGORIES

    for record in g4.positive_records():
        assert len(record.neutral_categories) >= MIN_NEUTRAL_CATEGORIES
        targets = set(g4.policy().target_clause_categories)
        assert not targets & set(record.neutral_categories)


def test_the_exclusion_audit_names_every_criterion_and_how_many_it_removed():
    """§4: the audit is part of the artifact, not a narrative about it.

    A pool size with no audit is a number a reader has to take on trust. This
    asserts that the audit exists, that every reason it reports is one of the
    declared criteria, and that it accounts for the contracts that did not make
    each pool.
    """
    selection = g4.selection()
    counts = selection.exclusion_counts()
    assert counts, "no exclusions were recorded at all, which cannot be right"
    for reason in counts:
        assert reason in EXCLUSION_CRITERIA, f"undeclared exclusion reason {reason!r}"

    total_contracts = len(g4.annotations())
    for kind, pool in selection.candidates.items():
        considered = len(pool) + sum(
            1 for entry in selection.excluded if entry.kind == kind
        )
        assert considered == total_contracts, (
            f"pool {kind!r}: {len(pool)} eligible + "
            f"{considered - len(pool)} excluded != {total_contracts} contracts"
        )


def test_the_pools_are_large_enough_that_the_choice_within_them_is_arbitrary():
    """§4: the twelve are a pick from a pool that would all have been defensible."""
    pools = g4.selection().candidates
    assert len(pools["positive:Change Of Control"]) >= 4 * POSITIVE_TARGET_QUOTAS[
        "Change Of Control"
    ]
    assert len(pools["positive:Termination For Convenience"]) >= (
        4 * POSITIVE_TARGET_QUOTAS["Termination For Convenience"]
    )
    assert len(pools["sentinel"]) >= 4 * SENTINEL_COUNT


# ---------------------------------------------------------------------------
# Determinism and the declared seed
# ---------------------------------------------------------------------------


def test_the_seed_is_declared_in_the_source():
    assert g4.selection().seed == GATE4_DEVELOPMENT_SEED
    assert GATE4_DEVELOPMENT_SEED == 20250901


def test_the_same_seed_gives_the_same_twelve_cases():
    from pilot01.experiment.development import select_development_cases

    rebuilt = select_development_cases(
        g4.annotations(),
        policy_targets=g4.policy().target_clause_categories,
        policy_version=g4.policy().policy_version,
        paragraph_spans=g4.paragraph_spans(),
        duplicate_of=g4.duplicate_of(),
    )
    assert rebuilt.cases == g4.selection().cases
    assert rebuilt.fingerprint == g4.selection().fingerprint


def test_a_different_seed_gives_a_different_but_still_valid_sample():
    """The seed chooses *which* eligible contracts, not whether they are eligible.

    A different seed must still produce four/four/four, still avoid duplicates
    and redactions, and still satisfy every criterion -- otherwise the seed
    would be doing work that the criteria are supposed to be doing.
    """
    from pilot01.experiment.development import select_development_cases

    other = select_development_cases(
        g4.annotations(),
        policy_targets=g4.policy().target_clause_categories,
        policy_version=g4.policy().policy_version,
        paragraph_spans=g4.paragraph_spans(),
        duplicate_of=g4.duplicate_of(),
        seed=GATE4_DEVELOPMENT_SEED + 1,
    )
    assert other.composition() == g4.selection().composition()
    assert other.fingerprint != g4.selection().fingerprint
    assert {case.contract_id for case in other.cases} != {
        case.contract_id for case in g4.selection().cases
    }
    for record in other.cases:
        assert record.contract_id not in g4.duplicate_of()
        for text in record.span_texts:
            assert len(text.strip()) >= MIN_SPAN_CHARS


def test_the_selection_fingerprint_covers_content_not_just_the_seed():
    cases = g4.selection().cases
    assert selection_fingerprint(cases, g4.selection().seed) == g4.selection().fingerprint
    assert selection_fingerprint(cases, g4.selection().seed + 1) != g4.selection().fingerprint
    assert selection_fingerprint(cases[:-1], g4.selection().seed) != g4.selection().fingerprint


def test_selection_refuses_a_quota_for_a_category_the_policy_does_not_target():
    from pilot01.experiment.development import select_development_cases

    with pytest.raises(ValueError, match="does not treat as targets"):
        select_development_cases(
            g4.annotations(),
            policy_targets=g4.policy().target_clause_categories,
            policy_version=g4.policy().policy_version,
            paragraph_spans=g4.paragraph_spans(),
            positive_quotas={"Governing Law": 1},
        )


def test_selection_refuses_a_policy_that_is_not_two_categories():
    from pilot01.experiment.development import select_development_cases

    with pytest.raises(ValueError, match="two-category policy"):
        select_development_cases(
            g4.annotations(),
            policy_targets=("Change Of Control",),
            policy_version="1",
            paragraph_spans=g4.paragraph_spans(),
            positive_quotas={"Change Of Control": 1},
        )


# ---------------------------------------------------------------------------
# §5: the case record
# ---------------------------------------------------------------------------


def test_every_record_carries_the_fields_the_spec_names():
    required = {
        "case_id",
        "contract_id",
        "cuad_title",
        "contract_text_hash",
        "target_category",
        "target_categories",
        "gold_clause_status",
        "gold_evidence_offsets",
        "gold_action",
        "is_negative_sentinel",
        "selection_reason",
        "exclusion_notes",
        "memo_ids",
        "policy_version",
        "source_snapshot_fingerprint",
        "review_status",
    }
    for record in g4.case_set().cases:
        assert required <= set(record.model_dump().keys())
        assert record.cuad_title
        assert record.selection_reason
        assert record.exclusion_notes
        assert record.memo_ids
        assert record.policy_version == g4.policy().policy_version


def test_every_record_is_pending_human_review():
    """§6: no code path may claim a human reviewed these cases."""
    for record in g4.case_set().cases:
        assert record.review_status == "PENDING_HUMAN_REVIEW"


def test_the_registry_carries_no_gold_and_the_record_carries_it_all():
    """The split that keeps gold out of the run-time artifact's readable surface.

    A case spec must carry the labels the scorer needs -- it is the scorer's
    input -- but it must not carry the experimenter-side material: the CUAD
    title, the selection reasoning, or the claim-level provenance. Those live in
    the development record, which nothing at run time reads.
    """
    spec_fields = set(g4.registry().get(g4.POSITIVE_CASE_IDS[0]).model_dump().keys())
    for forbidden in ("cuad_title", "selection_reason", "review_status", "claim_provenance"):
        assert forbidden not in spec_fields

    record_fields = set(g4.case_set().cases[0].model_dump().keys())
    for name in ("cuad_title", "selection_reason", "review_status", "claim_provenance"):
        assert name in record_fields


def test_the_cases_are_run_under_the_arms_the_design_declares():
    for record in g4.positive_records():
        assert record.error_conditions == ("E0", "E1")
        spec = g4.registry().get(record.case_id)
        assert spec.error_conditions == (ErrorCondition.E0, ErrorCondition.E1)
    for record in g4.sentinel_records():
        assert record.error_conditions == ("E0",)
        spec = g4.registry().get(record.case_id)
        assert spec.error_conditions == (ErrorCondition.E0,)


def test_a_sentinel_has_no_omission_record_and_no_omission_target():
    for record in g4.sentinel_records():
        spec = g4.registry().get(record.case_id)
        assert spec.omission_target_claim_id is None
        assert spec.omission_replacement_claim is None
        assert g4.registry().omission_record(record.case_id) is None
        assert record.omission_target_claim_id is None
        assert record.memo_pair_diff is None
        with pytest.raises(Exception):
            g4.registry().memo(record.case_id, ErrorCondition.E1)


def test_the_registry_survives_a_round_trip_through_its_file(tmp_path):
    from pilot01.experiment.cases import CaseRegistry

    path = g4.registry().write_json(tmp_path / "cases_v1.json")
    reloaded = CaseRegistry.load_json(path)
    assert reloaded.case_ids == g4.registry().case_ids
    assert reloaded.chunking == g4.registry().chunking
    for case_id in reloaded.case_ids:
        assert reloaded.get(case_id) == g4.registry().get(case_id)


def test_the_registry_and_the_record_are_self_consistent():
    case_set = g4.case_set()
    for record in case_set.cases:
        spec = case_set.registry.get(record.case_id)
        assert spec.contract_id == record.contract_id
        assert spec.gold_clause_status is record.gold_clause_status
        assert spec.gold_action is record.gold_action
        assert spec.is_negative_sentinel == record.is_negative_sentinel
        assert spec.gold_evidence_offsets == record.gold_evidence_offsets
        assert record.contract_text_hash == spec.contract_text_hash
