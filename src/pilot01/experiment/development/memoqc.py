"""The machine-readable diff between a case's two memo arms, and the QC over it.

§8 asks for the E1 arm to be a *matched* omission: same schema, same claim
count, same order, comparable length, and exactly one thing changed. §9 asks for
that to be checked and reported rather than assumed. This module does both, from
one place, so the stored diff and the QC verdict cannot disagree about what
changed -- the QC reads the diff, it does not recompute it.

**Why a stored diff rather than a comparison at read time.** The claim "the two
arms differ only in the registered fields" is a statement about a specific pair
of artifacts. Storing the diff next to them means a reader years later can see
what changed without re-deriving it, and means the invariant test can assert
against the same structure the experiment shipped.

**The check is exact, and it is meant to be.** The only permitted difference is:
the omitted claim's ``(claim_id, category, status, summary, source_ids)`` tuple
replaced, in the same position, by the replacement claim's. Everything else --
every other claim, its position, its summary, its citations -- must be
byte-identical between the arms. A diff that reported "close enough" would be
reporting the absence of a finding about the manipulation, which is not the same
thing as the manipulation being clean.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from ...schemas import AnalystMemo, OmissionRecord

__all__ = [
    "MemoPairDiff",
    "MemoQCReport",
    "diff_memo_pair",
    "build_memo_qc",
    "word_count",
]


def word_count(text: str) -> int:
    """A whitespace word count -- the offline approximation §9 asks for.

    Deliberately not a tokenizer. A real tokenizer would make the QC number
    depend on a model's vocabulary, and the point of the number is to show the
    two arms are comparable in size, which whitespace words already show.
    """
    return len(text.split())


def _claim_signature(claim) -> tuple:
    return (
        claim.claim_id,
        claim.category,
        claim.status.value,
        claim.summary,
        tuple(claim.source_ids),
    )


class MemoPairDiff(BaseModel):
    """Exactly what differs between one case's E0 and E1 memos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    memo_id: str
    e0_claim_count: int
    e1_claim_count: int
    changed_indices: tuple[int, ...]
    """Positions at which the two claim sequences differ. Empty iff identical."""

    removed: tuple[str, ...]
    """Signatures present in E0 and not in E1, rendered for a human to read."""

    added: tuple[str, ...]
    """Signatures present in E1 and not in E0."""

    unchanged_claim_ids: tuple[str, ...]
    removed_source_ids: tuple[str, ...]
    added_source_ids: tuple[str, ...]
    removed_categories: tuple[str, ...]
    added_categories: tuple[str, ...]
    e0_chars: int
    e1_chars: int
    e0_words: int
    e1_words: int
    shape: str
    """``substitution`` or ``deletion``, taken from the omission record."""

    @property
    def claim_count_delta(self) -> int:
        return self.e1_claim_count - self.e0_claim_count

    @property
    def char_delta(self) -> int:
        return self.e1_chars - self.e0_chars

    @property
    def word_delta(self) -> int:
        return self.e1_words - self.e0_words

    @property
    def is_single_position_change(self) -> bool:
        """Whether the arms differ at exactly one index and nowhere else."""
        return len(self.changed_indices) == 1

    def to_payload(self) -> dict:
        payload = self.model_dump(mode="json")
        payload["claim_count_delta"] = self.claim_count_delta
        payload["char_delta"] = self.char_delta
        payload["word_delta"] = self.word_delta
        payload["is_single_position_change"] = self.is_single_position_change
        return payload


def _render(signature: tuple) -> str:
    claim_id, category, status, summary, source_ids = signature
    return (
        f"{claim_id} [{category}/{status}] sources={list(source_ids)} "
        f"summary={summary[:80]!r}"
    )


def diff_memo_pair(
    e0: AnalystMemo, e1: AnalystMemo, omission: OmissionRecord
) -> MemoPairDiff:
    """Diff a case's two memo arms and record what changed.

    Raises ``ValueError`` if the two memos are not the same case, since a diff
    across two cases would be a diff of nothing in particular.
    """
    if e0.case_id != e1.case_id:
        raise ValueError(
            f"cannot diff memos of different cases: {e0.case_id!r} and {e1.case_id!r}"
        )
    if e0.memo_id != e1.memo_id:
        raise ValueError(
            f"the two arms of case {e0.case_id!r} carry different memo ids "
            f"({e0.memo_id!r} and {e1.memo_id!r}); an arm marker in the memo id "
            "would let a downstream agent tell which memo it is holding"
        )

    left = [_claim_signature(claim) for claim in e0.claims]
    right = [_claim_signature(claim) for claim in e1.claims]
    changed = tuple(
        index
        for index in range(max(len(left), len(right)))
        if index >= len(left) or index >= len(right) or left[index] != right[index]
    )

    left_set, right_set = set(left), set(right)
    removed = tuple(_render(signature) for signature in left if signature not in right_set)
    added = tuple(_render(signature) for signature in right if signature not in left_set)

    unchanged_ids = tuple(
        claim.claim_id
        for index, claim in enumerate(e0.claims)
        if index < len(e1.claims) and left[index] == right[index]
    )

    e0_text = " ".join(claim.summary for claim in e0.claims)
    e1_text = " ".join(claim.summary for claim in e1.claims)

    # The changed positions are the only place either arm's citations or
    # categories can differ, so the removed/added sets are read straight off
    # them rather than by comparing the two memos wholesale.
    e0_changed = [e0.claims[index] for index in changed if index < len(e0.claims)]
    e1_changed = [e1.claims[index] for index in changed if index < len(e1.claims)]
    e0_sources = {source_id for claim in e0_changed for source_id in claim.source_ids}
    e1_sources = {source_id for claim in e1_changed for source_id in claim.source_ids}

    return MemoPairDiff(
        case_id=e0.case_id,
        memo_id=e0.memo_id,
        e0_claim_count=len(e0.claims),
        e1_claim_count=len(e1.claims),
        changed_indices=changed,
        removed=removed,
        added=added,
        unchanged_claim_ids=unchanged_ids,
        removed_source_ids=tuple(sorted(e0_sources - e1_sources)),
        added_source_ids=tuple(sorted(e1_sources - e0_sources)),
        removed_categories=tuple(claim.category for claim in e0_changed),
        added_categories=tuple(claim.category for claim in e1_changed),
        e0_chars=len(e0_text),
        e1_chars=len(e1_text),
        e0_words=word_count(e0_text),
        e1_words=word_count(e1_text),
        shape="substitution" if omission.is_substitution else "deletion",
    )


class MemoQCReport(BaseModel):
    """§9's per-pair QC, plus the verdict over the whole set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diffs: tuple[MemoPairDiff, ...]
    flags: tuple[str, ...]
    """Pairs where the replacement changed more than the intended unit.

    Empty is the expected state. A non-empty list is not automatically a
    failure -- it is a finding about the manipulation, and §16 requires it to be
    separated from a finding about an agent.
    """

    sentinel_case_ids: tuple[str, ...]
    """Cases that correctly have no E1 arm. Reported so "no diff" for a sentinel
    is visible as a decision rather than as a missing row."""

    max_abs_word_delta: int
    max_abs_char_delta: int

    @property
    def clean(self) -> bool:
        return not self.flags

    def to_payload(self) -> dict:
        return {
            "gate": 4,
            "purpose": "memo-pair matching QC (Gate 4 §9)",
            "pair_count": len(self.diffs),
            "clean": self.clean,
            "flags": list(self.flags),
            "sentinel_case_ids": list(self.sentinel_case_ids),
            "max_abs_word_delta": self.max_abs_word_delta,
            "max_abs_char_delta": self.max_abs_char_delta,
            "pairs": [diff.to_payload() for diff in self.diffs],
        }


def build_memo_qc(
    diffs: Sequence[MemoPairDiff],
    *,
    sentinel_case_ids: Sequence[str] = (),
    max_word_delta: int = 8,
) -> MemoQCReport:
    """Check every pair against the matched-omission requirements.

    Four checks, each of which the design claims and none of which is assumed:

    * the two arms hold the same number of claims (§8's same-count requirement);
    * they differ at exactly one position;
    * no claim other than the target changed -- the diff's unchanged set is
      every other claim id;
    * the size difference is small. ``max_word_delta`` is a *reporting*
      threshold, not a design invariant: it flags a pair whose arms are visibly
      different lengths so a reader sees it, and a substitution drawn from the
      same contract's clause pool has no mechanism to exceed it by much.
    """
    flags: list[str] = []
    for diff in diffs:
        if diff.e0_claim_count != diff.e1_claim_count:
            flags.append(
                f"{diff.case_id}: claim counts differ "
                f"({diff.e0_claim_count} -> {diff.e1_claim_count})"
            )
        if not diff.is_single_position_change:
            flags.append(
                f"{diff.case_id}: the arms differ at {len(diff.changed_indices)} "
                f"position(s) {list(diff.changed_indices)}, not one"
            )
        if diff.e0_claim_count != len(diff.unchanged_claim_ids) + 1:
            flags.append(
                f"{diff.case_id}: {len(diff.unchanged_claim_ids)} claim(s) unchanged "
                f"out of {diff.e0_claim_count}, so more than the target moved"
            )
        if abs(diff.word_delta) > max_word_delta:
            flags.append(
                f"{diff.case_id}: the arms differ by {diff.word_delta} words, over "
                f"the {max_word_delta}-word reporting threshold"
            )

    return MemoQCReport(
        diffs=tuple(diffs),
        flags=tuple(flags),
        sentinel_case_ids=tuple(sentinel_case_ids),
        max_abs_word_delta=max((abs(diff.word_delta) for diff in diffs), default=0),
        max_abs_char_delta=max((abs(diff.char_delta) for diff in diffs), default=0),
    )
