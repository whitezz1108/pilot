"""E0/E1 memo pairing.

The omission arm must be a *strict* single-claim deletion of the correct arm:
no rewriting, no reordering, no marker, no model. These tests are the contract
between the two experimental arms.
"""

from __future__ import annotations

import pytest

import sample_case
from pilot01.schemas import (
    AnalystMemo,
    ClauseStatus,
    ErrorCondition,
    MemoClaim,
    build_omission_memo,
)
from pilot01.workflow.transitions import ProtocolError

MARKER_PATTERNS = ("omission", "omit", "deleted", "removed", "missing", "elided")


def all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys |= all_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            keys |= all_keys(nested)
    return keys


def test_E0_E1_differ_only_by_target_claim_deletion(e0_memo):
    result = build_omission_memo(
        e0_memo,
        sample_case.TARGET_CLAIM_ID,
        expected_category=sample_case.TARGET_CATEGORY,
    )
    e1_memo = result.memo

    assert len(e0_memo.claims) == len(e1_memo.claims) + 1
    assert sample_case.TARGET_CLAIM_ID in e0_memo.claim_ids()
    assert sample_case.TARGET_CLAIM_ID not in e1_memo.claim_ids()

    survivors = tuple(
        claim for claim in e0_memo.claims if claim.claim_id != sample_case.TARGET_CLAIM_ID
    )
    assert e1_memo.claims == survivors

    # Nothing outside the claim tuple differs.
    assert e1_memo.memo_id == e0_memo.memo_id
    assert e1_memo.case_id == e0_memo.case_id
    assert set(e0_memo.model_dump()) == set(e1_memo.model_dump())

    e0_dump, e1_dump = e0_memo.model_dump(), e1_memo.model_dump()
    assert e0_dump.pop("claims") != e1_dump.pop("claims")
    assert e0_dump == e1_dump


def test_omission_preserves_order_and_content_of_other_claims(e0_memo):
    e1_memo = build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID).memo

    expected_ids = [
        claim_id
        for claim_id in e0_memo.claim_ids()
        if claim_id != sample_case.TARGET_CLAIM_ID
    ]
    assert list(e1_memo.claim_ids()) == expected_ids

    for original, derived in zip(
        (c for c in e0_memo.claims if c.claim_id != sample_case.TARGET_CLAIM_ID),
        e1_memo.claims,
        strict=True,
    ):
        assert derived == original
        assert derived.model_dump() == original.model_dump()


def test_omission_memo_carries_no_marker(e0_memo):
    """The E1 memo must be indistinguishable from E0 apart from the missing claim."""
    e1_memo = build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID).memo

    assert set(all_keys(e1_memo.model_dump())) == set(all_keys(e0_memo.model_dump()))
    for key in all_keys(e1_memo.model_dump()):
        assert not any(pattern in key.lower() for pattern in MARKER_PATTERNS), key
    assert type(e1_memo) is type(e0_memo)


def test_omission_record_preserves_traceability(e0_memo, omission_record):
    assert omission_record.source_memo_id == e0_memo.memo_id
    assert omission_record.omitted_claim_id == sample_case.TARGET_CLAIM_ID
    assert omission_record.omitted_claim_category == sample_case.TARGET_CATEGORY
    assert omission_record.omitted_claim_status is ClauseStatus.PRESENT
    assert omission_record.e0_claim_count == len(e0_memo.claims)
    assert omission_record.e1_claim_count == len(e0_memo.claims) - 1


def test_omission_record_is_not_part_of_the_memo(e0_memo, omission_record):
    """Provenance lives beside the artifact, never inside it."""
    e1_memo = build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID).memo
    assert omission_record.omitted_claim_id not in e1_memo.claim_ids()
    assert "omission" not in set(all_keys(e1_memo.model_dump()))


def test_omission_requires_an_existing_target_claim(e0_memo):
    with pytest.raises(ValueError, match="not present in memo"):
        build_omission_memo(e0_memo, "CL-999")


def test_omission_rejects_an_ambiguous_claim_id():
    duplicated = AnalystMemo(
        memo_id="MEMO-DUP",
        case_id=sample_case.CASE_ID,
        claims=(
            MemoClaim(
                claim_id="CL-1",
                category=sample_case.TARGET_CATEGORY,
                status=ClauseStatus.PRESENT,
                summary="first",
            ),
            MemoClaim(
                claim_id="CL-1",
                category=sample_case.TARGET_CATEGORY,
                status=ClauseStatus.ABSENT,
                summary="second",
            ),
        ),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        build_omission_memo(duplicated, "CL-1")


def test_omission_rejects_the_wrong_category(e0_memo):
    """A mis-specified target must not silently delete the wrong clause."""
    with pytest.raises(ValueError, match="expected"):
        build_omission_memo(
            e0_memo, sample_case.TARGET_CLAIM_ID, expected_category="governing_law"
        )


def test_omission_is_deterministic(e0_memo):
    first = build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID)
    second = build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID)
    assert first.memo == second.memo
    assert first.provenance == second.provenance
    assert first.memo.model_dump_json() == second.memo.model_dump_json()


def test_omission_leaves_the_source_memo_untouched(e0_memo):
    before = e0_memo.model_dump_json()
    build_omission_memo(e0_memo, sample_case.TARGET_CLAIM_ID)
    assert e0_memo.model_dump_json() == before
    assert len(e0_memo.claims) == 4


def test_repository_selects_the_right_arm(repository, e0_memo, omission_record):
    e0 = repository.load(sample_case.CASE_ID, ErrorCondition.E0)
    e1 = repository.load(sample_case.CASE_ID, ErrorCondition.E1)

    assert e0 == e0_memo
    assert len(e1.claims) == len(e0.claims) - 1
    assert e1.claim(sample_case.TARGET_CLAIM_ID) is None
    assert e1.memo_id == e0.memo_id
    assert len(repository) == 2
    assert omission_record.e1_claim_count == len(e1.claims)


def test_repository_rejects_an_unknown_case(repository):
    with pytest.raises(ProtocolError, match="no frozen memo"):
        repository.load("CASE-404", ErrorCondition.E0)


def test_both_arms_carry_the_same_field_set(repository):
    e0 = repository.load(sample_case.CASE_ID, ErrorCondition.E0)
    e1 = repository.load(sample_case.CASE_ID, ErrorCondition.E1)
    assert set(e0.model_dump()) == set(e1.model_dump())
    for claim in e0.claims + e1.claims:
        assert set(claim.model_dump()) == set(e0.claims[0].model_dump())
