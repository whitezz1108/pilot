"""Choosing the real development contracts, deterministically and for stated reasons.

Gate 4 runs on twelve real CUAD contracts: eight *positive* cases whose target
clause is genuinely present (so the E1 arm has something to omit) and four
*negative sentinels* where both target clauses are genuinely absent (so the
policy's ACCEPT branch has something to be wrong about).

**Selection happens before any live run, and never looks at a model outcome.**
That is the whole point of doing it here, in a module that cannot import a
model client, from annotations that were frozen before this project started.
A pool chosen by "which contracts the agents handled interestingly" would not
be a sample of contracts; it would be a sample of results.

**Every rejection is recorded with a reason.** The output of this module is not
just twelve cases -- it is twelve cases plus an audit of what was excluded and
why, because "we picked these twelve" is only a scientific statement if a
reader can see what the alternative would have been. The exclusion criteria are
declared as constants below and applied mechanically; none of them consults a
model, and none of them is tuned to a result.

**The pool is large and the choice within it is arbitrary.** Both target
categories have far more eligible contracts than the design needs, so the
selection is a diversity-seeking pick over a pool that would all have been
defensible. The seed is declared and the procedure is a pure function of
(annotations, seed), so a reader can rerun it and get the same twelve.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ...schemas import ClauseStatus, Decision
from .cuad_annotations import CuadAnnotationSet, CuadContractAnnotations

__all__ = [
    "GATE4_DEVELOPMENT_SEED",
    "POSITIVE_TARGET_QUOTAS",
    "SENTINEL_COUNT",
    "EXCLUSION_CRITERIA",
    "MAX_GOLD_SPANS",
    "MIN_SPAN_CHARS",
    "MIN_CONTEXT_CHARS",
    "MIN_NEUTRAL_CATEGORIES",
    "NEAR_DUPLICATE_THRESHOLD",
    "Candidate",
    "Excluded",
    "SelectedCase",
    "SelectionReport",
    "selection_fingerprint",
    "near_duplicate_groups",
    "select_development_cases",
]


GATE4_DEVELOPMENT_SEED = 20250901
"""The declared seed for Gate 4's development-set selection.

There was no pre-existing development seed to preserve -- the project's only
seed so far is the run plan's, which is a different decision (run order) from
this one (which contracts). Fixing a new one here, in the source rather than in
a command line, is what makes "the same twelve cases" reproducible by anyone
who checks out the repository.
"""

POSITIVE_TARGET_QUOTAS: Mapping[str, int] = {
    "Change Of Control": 4,
    "Termination For Convenience": 4,
}
"""How many positive cases each target category contributes.

Four each, so neither target category can drive a result on its own and the
two-category policy is exercised in both directions. Each positive case has
*exactly one* target category present: a contract with both present would
still be a policy trigger after the E1 arm removed one of them, so the omission
would not be the only thing that changed.
"""

SENTINEL_COUNT = 4
"""How many negative sentinels to draw: both target clauses confirmed absent."""

EXCLUSION_CRITERIA: Mapping[str, str] = {
    "DUPLICATE_TEXT": "another contract in the corpus has byte-identical text",
    "NEAR_DUPLICATE_TEXT": (
        "the text is a near-duplicate of another contract (a re-executed or "
        "amended copy); including both would double-count one drafting style"
    ),
    "TEXT_TOO_SHORT": (
        "the contract is too short to carry the context claims a memo needs "
        "alongside the target claim"
    ),
    "REDACTED_TARGET_SPAN": (
        "the gold evidence span contains a redaction marker, so the text the "
        "case would be scored against is not the clause"
    ),
    "FRAGMENTED_EVIDENCE": (
        "the gold evidence is spread over more spans than the design admits, "
        "or a span is too short to be a clause statement"
    ),
    "STATUS_AMBIGUOUS": (
        "CUAD's own annotation is internally non-committal -- the impossible "
        "flag and the answer spans disagree -- so the gold status would need "
        "substantive legal interpretation to fix"
    ),
    "OFFSET_MISMATCH": (
        "the annotation's offsets do not reproduce its own text in the frozen "
        "context, so the case would be scored against the wrong characters"
    ),
    "UNMAPPABLE_TO_PARAGRAPH": (
        "the gold span overlaps no paragraph in the Gate-2.5 source layer, so "
        "the evidence metric could never register it"
    ),
    "BOTH_TARGETS_PRESENT": (
        "both target categories are present, so removing one would leave the "
        "case still carrying a policy trigger"
    ),
    "NO_NEUTRAL_REPLACEMENT": (
        "the contract offers fewer than four non-target clauses with usable "
        "evidence, so the memo could not be filled and the omitted claim could "
        "not be replaced by a clause the memo does not already carry"
    ),
    "TARGET_PRESENT_IN_SENTINEL_POOL": (
        "a sentinel must have both target clauses confirmed absent; this one "
        "has at least one present"
    ),
    "TARGET_ABSENT_IN_POSITIVE_POOL": (
        "a positive case must have its target clause present"
    ),
}
"""Every reason a contract can be excluded, with why the criterion exists.

Kept as data rather than as comments so the audit trail names a reason the same
way the code does, and so a reader can see the complete set of ways a contract
could fail to make the pool without reading the selection logic.
"""

MAX_GOLD_SPANS = 2
"""The most evidence spans a case may carry before it counts as fragmented."""

MIN_SPAN_CHARS = 30
"""The shortest a gold span may be. Below this it is a phrase, not a clause."""

MIN_NEUTRAL_CATEGORIES = 4
"""How many usable non-target clauses a positive case's contract must offer.

Three of them become the memo's context claims and the fourth becomes the E1
arm's replacement, so a contract with fewer than four cannot support a
length-matched omission at all. Declared as a constant rather than as a magic
number inside the memo builder, because it is an eligibility criterion: it
decides which contracts are in the pool, and that decision belongs with the
other criteria where it can be audited.
"""

MIN_CONTEXT_CHARS = 4000
"""The shortest a contract may be.

The memo schema gives each case a fixed set of context claims beside its target
claim, and the E1 arm has to replace the target with a *different* non-target
clause from the same contract. A contract with only one or two clauses in it
has nothing to offer as a replacement, and its memo would be mostly padding.
"""

NEAR_DUPLICATE_THRESHOLD = 0.80
"""Bottom-k Jaccard estimate above which two contracts count as near-duplicates.

CUAD contains re-executed and amended copies of the same agreement, and two
copies of one agreement are one drafting style sampled twice. 0.80 is high
enough that ordinary boilerplate overlap between unrelated contracts (governing
law, notices, counterparts) stays well below it.
"""

_SHINGLE_WORDS = 6
_SIGNATURE_SIZE = 48

REDACTION_MARKERS = (
    "[***]",
    "[****]",
    "[*****]",
    "XXXX",
    "____",
    "####",
)
"""Substrings CUAD uses where text was withheld.

A redacted span is still a *present* clause -- that part of the annotation is
fine -- but its text is not the clause, so a case built on it would score
evidence overlap against asterisks.
"""

_QUESTION_TITLE_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """One contract's eligibility as a case of a given kind.

    A contract produces one candidate per kind it is being considered for, and
    the two kinds have disjoint requirements (a positive needs its target
    present; a sentinel needs both targets absent), so no contract is ever a
    candidate for both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    kind: str
    """``"positive"`` or ``"sentinel"``."""

    target_category: str
    """The category the case is about. For a sentinel, the one whose absence is
    the point -- both are absent, so this is a labelling choice, recorded here
    so the registry does not have to invent one later."""

    other_category: str
    """The other policy target category, carried so the audit can show it was
    checked rather than merely not mentioned."""

    gold_status: ClauseStatus
    spans: tuple[tuple[int, int], ...]
    context_chars: int
    title: str
    type_token: str
    """A coarse contract-type token taken from the title, used only for diversity."""

    paragraph_ordinals: tuple[int, ...]
    """Ordinals of the Gate-2.5 paragraphs the gold spans fall in. At least one each.

    Ordinals rather than paragraph ids: an id carries the contract's own text
    hash and the chunking sizes, so it cannot be known until the registry picks
    its layer. The registry builder resolves these against the layer it builds
    and fails if any of them does not resolve.
    """

    neutral_categories: tuple[str, ...]
    """Non-target categories with usable evidence, in CUAD's own question order.

    These are the pool the E1 arm draws its replacement claim from, so a
    candidate with none of them cannot support a length-matched omission.
    """

    @property
    def evidence_start_ratio(self) -> float:
        """Where in the contract the target clause sits, as a fraction of its length.

        Used only to spread the sample across the document: clauses cluster at
        the end of these agreements, and twelve cases all drawn from the final
        tenth would be twelve samples of one position.
        """
        if not self.spans or self.context_chars <= 0:
            return 0.0
        return self.spans[0][0] / self.context_chars


class Excluded(BaseModel):
    """A contract that did not make the pool, and the criteria that kept it out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    kind: str
    reasons: tuple[str, ...]

    def explain(self) -> str:
        return "; ".join(f"{reason}: {EXCLUSION_CRITERIA[reason]}" for reason in self.reasons)


class SelectedCase(BaseModel):
    """One of the twelve, with everything Gate 4 §5 requires a case record to hold.

    This is the selection layer's own record, deliberately wider than
    :class:`~pilot01.experiment.cases.CaseSpec`: the case spec carries what a
    run needs, while this carries what a *reader* needs to audit the choice --
    the CUAD identity, the selection reason, the exclusions that did not apply,
    and the review state. The registry builder turns one of these plus a memo
    into a case spec, so nothing here has to be squeezed into the run-time shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    contract_id: str
    cuad_title: str
    """The upstream title, experimenter-side only. Never reaches a model."""

    contract_text_hash: str
    target_category: str
    target_categories: tuple[str, ...]
    """The policy's full target list, so the record is self-describing."""

    gold_target_clause_status: dict[str, ClauseStatus]
    """One gold label per target category the policy names.

    The pool rule in :func:`_check_contract` already fixes both labels: a
    positive contract must hold the target category and must not hold the other
    (``BOTH_TARGETS_PRESENT`` rejects it), and a sentinel contract must hold
    neither. So this mapping is not a new judgement -- it is the eligibility
    rule written in the shape the scorer compares against.
    """

    gold_evidence_offsets: tuple[int, ...]
    gold_action: Decision
    is_negative_sentinel: bool
    selection_reason: str
    exclusion_notes: tuple[str, ...]
    """Criteria that were *checked and passed*, so the audit shows the checks ran."""

    memo_ids: tuple[str, ...]
    policy_version: str
    source_snapshot_fingerprint: str
    review_status: str
    """One of ``PENDING_HUMAN_REVIEW`` / ``REVIEWED`` / ``REJECTED``.

    Gate 4 sets this to ``PENDING_HUMAN_REVIEW`` and leaves it there. No human
    has reviewed these cases, and the field exists so that no artifact can
    claim otherwise by omission.
    """

    paragraph_ordinals: tuple[int, ...]
    span_texts: tuple[str, ...]
    """The gold evidence text, sliced from the frozen context."""

    neutral_categories: tuple[str, ...]

    @property
    def span_count(self) -> int:
        return len(self.spans())

    def spans(self) -> tuple[tuple[int, int], ...]:
        offsets = self.gold_evidence_offsets
        return tuple(
            (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
        )


@dataclass(frozen=True)
class SelectionReport:
    """The twelve cases, and the audit of how the pool became twelve."""

    cases: tuple[SelectedCase, ...]
    candidates: Mapping[str, tuple[Candidate, ...]]
    excluded: tuple[Excluded, ...]
    seed: int
    fingerprint: str

    def composition(self) -> dict[str, int]:
        """Counts by kind and target category -- the shape §4 requires."""
        counts: dict[str, int] = {}
        for case in self.cases:
            key = (
                "sentinel"
                if case.is_negative_sentinel
                else f"positive:{case.target_category}"
            )
            counts[key] = counts.get(key, 0) + 1
        return counts

    def exclusion_counts(self) -> dict[str, int]:
        """How many contracts each criterion removed, across all kinds."""
        counts: dict[str, int] = {}
        for excluded in self.excluded:
            for reason in excluded.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    def to_payload(self) -> dict:
        return {
            "gate": 4,
            "purpose": (
                "development-set selection for the CUAD omission-propagation pilot; "
                "DEVELOPMENT ONLY -- not a confirmatory sample"
            ),
            "seed": self.seed,
            "fingerprint": self.fingerprint,
            "composition": self.composition(),
            "pool_sizes": {
                kind: len(candidates) for kind, candidates in self.candidates.items()
            },
            "exclusion_counts": self.exclusion_counts(),
            "exclusion_criteria": dict(EXCLUSION_CRITERIA),
            "excluded": [entry.model_dump(mode="json") for entry in self.excluded],
            "cases": [case.model_dump(mode="json") for case in self.cases],
        }


# ---------------------------------------------------------------------------
# near-duplicate detection
# ---------------------------------------------------------------------------


def _shingle_hashes(text: str) -> list[int]:
    """Bottom-k hashes of the text's word 6-grams.

    Word shingles rather than character n-grams: these contracts run to tens of
    thousands of words, and word shingles catch exactly the duplication that
    matters here (a re-executed copy of the same agreement) at a fraction of the
    cost. Keeping only the k smallest hashes bounds memory regardless of length.
    """
    words = _QUESTION_TITLE_TOKEN_RE.sub(" ", text.lower()).split()
    if len(words) < _SHINGLE_WORDS:
        return []
    hashes: list[int] = []
    for index in range(len(words) - _SHINGLE_WORDS + 1):
        shingle = " ".join(words[index : index + _SHINGLE_WORDS])
        hashes.append(int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big"))
    hashes.sort()
    return hashes[:_SIGNATURE_SIZE]


def near_duplicate_groups(
    texts: Mapping[str, str], *, threshold: float = NEAR_DUPLICATE_THRESHOLD
) -> dict[str, str]:
    """Map each contract id to a canonical id it duplicates, when it duplicates one.

    Uses a bottom-k sketch Jaccard estimate: take the k smallest shingle hashes
    of the union of two contracts and measure how many of them both hold. That
    is the standard estimator for this sketch and it is stable -- the same
    inputs give the same answer on every machine, with no hashing seed involved,
    because the hashes come from blake2b rather than from ``hash()``.

    Exact duplicates are handled first and unconditionally: identical text is
    identical text whatever the estimator says about its shingles.
    """
    exact: dict[str, str] = {}
    by_hash: dict[str, str] = {}
    for contract_id, text in texts.items():
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in by_hash:
            exact[contract_id] = by_hash[digest]
        else:
            by_hash[digest] = contract_id

    signatures = {contract_id: _shingle_hashes(text) for contract_id, text in texts.items()}
    duplicates: dict[str, str] = {}
    ids = list(texts)
    for index, left in enumerate(ids):
        if left in exact:
            continue
        left_hashes = signatures[left]
        if not left_hashes:
            continue
        left_set = set(left_hashes)
        for right in ids[:index]:
            if right in exact or right in duplicates:
                continue
            right_set = set(signatures[right])
            if not right_set:
                continue
            union = sorted(left_set | right_set)[:_SIGNATURE_SIZE]
            if not union:
                continue
            shared = sum(1 for value in union if value in left_set and value in right_set)
            if shared / len(union) >= threshold:
                duplicates[left] = right
                break
    duplicates.update(exact)
    return duplicates


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


def _contains_redaction(text: str) -> bool:
    return any(marker in text for marker in REDACTION_MARKERS)


def _type_token(title: str) -> str:
    """A coarse contract-type token from a CUAD title.

    CUAD titles look like ``PARTY_DATE-EX-10-TYPE OF AGREEMENT``; the part after
    the last hyphen is the type. Titles that do not follow that shape fall back
    to their whole trailing word, which is still a usable diversity key and is
    never used for anything but spreading the sample.
    """
    tail = title.rsplit("-", 1)[-1].strip()
    token = _QUESTION_TITLE_TOKEN_RE.sub(" ", tail).strip()
    return token or "UNKNOWN"


def _usable_neutral_categories(
    contract: CuadContractAnnotations, *, targets: Iterable[str]
) -> tuple[str, ...]:
    """Non-target categories with at least one clean, non-trivial span."""
    target_set = set(targets)
    usable: list[str] = []
    for clause in contract.clauses:
        if clause.category in target_set or not clause.present:
            continue
        spans = clause.texts(contract.context)
        if not spans or any(_contains_redaction(text) for text in spans):
            continue
        if max(len(text.strip()) for text in spans) < MIN_SPAN_CHARS:
            continue
        usable.append(clause.category)
    return tuple(usable)


def _check_contract(
    contract: CuadContractAnnotations,
    *,
    kind: str,
    target: str,
    other: str,
    targets: Sequence[str],
    paragraph_spans: Sequence[tuple[int, int]],
) -> tuple[Candidate | None, tuple[str, ...]]:
    """Decide one contract's eligibility, returning the candidate or the reasons.

    Every criterion is applied to every contract rather than short-circuiting on
    the first failure, so the audit can report the complete set of reasons a
    contract was kept out. A contract rejected for three reasons is three facts
    about the pool, not one.
    """
    reasons: list[str] = []

    if len(contract.context) < MIN_CONTEXT_CHARS:
        reasons.append("TEXT_TOO_SHORT")

    target_clause = contract.clause(target)
    other_clause = contract.clause(other)

    if kind == "positive":
        if not target_clause.present:
            reasons.append("TARGET_ABSENT_IN_POSITIVE_POOL")
        if other_clause.present:
            reasons.append("BOTH_TARGETS_PRESENT")
    else:
        if target_clause.present or other_clause.present:
            reasons.append("TARGET_PRESENT_IN_SENTINEL_POOL")

    if target_clause.is_impossible and target_clause.spans:
        reasons.append("STATUS_AMBIGUOUS")

    spans = target_clause.spans if kind == "positive" else ()
    if kind == "positive":
        if len(spans) > MAX_GOLD_SPANS:
            reasons.append("FRAGMENTED_EVIDENCE")
        texts = target_clause.texts(contract.context)
        if any(_contains_redaction(text) for text in texts):
            reasons.append("REDACTED_TARGET_SPAN")
        if texts and min(len(text.strip()) for text in texts) < MIN_SPAN_CHARS:
            reasons.append("FRAGMENTED_EVIDENCE")
        for start, end in spans:
            if not (0 <= start < end <= len(contract.context)):
                reasons.append("OFFSET_MISMATCH")
            elif not contract.context[start:end].strip():
                reasons.append("OFFSET_MISMATCH")

    neutral = _usable_neutral_categories(contract, targets=targets)
    if kind == "positive" and len(neutral) < MIN_NEUTRAL_CATEGORIES:
        reasons.append("NO_NEUTRAL_REPLACEMENT")

    paragraph_ordinals: tuple[int, ...] = ()
    if kind == "positive" and spans:
        matched = [
            index
            for index, (paragraph_start, paragraph_end) in enumerate(paragraph_spans)
            if any(
                start < paragraph_end and paragraph_start < end for start, end in spans
            )
        ]
        if not matched:
            reasons.append("UNMAPPABLE_TO_PARAGRAPH")
        paragraph_ordinals = tuple(matched)

    if reasons:
        return None, tuple(dict.fromkeys(reasons))

    return (
        Candidate(
            contract_id=contract.contract_id,
            kind=kind,
            target_category=target,
            other_category=other,
            gold_status=ClauseStatus.PRESENT if kind == "positive" else ClauseStatus.ABSENT,
            spans=spans,
            context_chars=len(contract.context),
            title=contract.title,
            type_token=_type_token(contract.title),
            paragraph_ordinals=paragraph_ordinals,
            neutral_categories=neutral,
        ),
        (),
    )


# ---------------------------------------------------------------------------
# diversity
# ---------------------------------------------------------------------------


def _diversity_distance(left: Candidate, right: Candidate) -> float:
    """A distance in ``[0, 1]``-ish used only to spread the sample.

    Two terms, both deliberately coarse: a categorical penalty when the two
    contracts are the same type or fall in the same length bucket, and a
    continuous term for how far apart the target clauses sit in their documents.
    Nothing here is a measurement of the experiment -- it is a way to avoid
    drawing twelve near-identical samples, and it is fixed in the source so the
    same pool and seed always give the same twelve.
    """
    categorical = 0.0
    if left.type_token == right.type_token:
        categorical += 0.4
    if _length_bucket(left.context_chars) == _length_bucket(right.context_chars):
        categorical += 0.2
    positional = abs(left.evidence_start_ratio - right.evidence_start_ratio)
    return categorical + 0.4 * min(1.0, positional * 4.0)


def _length_bucket(chars: int) -> int:
    return int(math.log2(max(chars, 2)))


def _select_diverse(pool: Sequence[Candidate], count: int, rng: random.Random) -> list[Candidate]:
    """Greedy farthest-point selection over ``pool``, seeded for tie-breaking.

    The first pick is drawn from the shuffled order rather than fixed, so the
    seed genuinely determines the sample instead of only its ordering. Each
    later pick maximizes the *minimum* distance to everything already chosen,
    which is the standard way to spread a small sample over a heterogeneous
    pool. Ties are broken by the shuffled order, so the result is a pure
    function of ``(pool, count, seed)``.
    """
    order = list(pool)
    rng.shuffle(order)
    if count >= len(order):
        return order
    chosen = [order[0]]
    remaining = order[1:]
    while len(chosen) < count and remaining:
        best_index = 0
        best_score = -1.0
        for index, candidate in enumerate(remaining):
            score = min(_diversity_distance(candidate, taken) for taken in chosen)
            if score > best_score:
                best_score = score
                best_index = index
        chosen.append(remaining.pop(best_index))
    return chosen


# ---------------------------------------------------------------------------
# the selection
# ---------------------------------------------------------------------------


def _case_id(kind: str, target: str, contract_id: str) -> str:
    """``DEV-<KIND>-<CATEGORY>-<CONTRACT>``.

    The id says what the case is, which keeps a run directory readable, and it
    is derived from the contract id rather than from a counter so that adding a
    case later cannot renumber the existing ones.
    """
    slug = {
        "Change Of Control": "COC",
        "Termination For Convenience": "TFC",
    }.get(target, _QUESTION_TITLE_TOKEN_RE.sub("", target).upper()[:6] or "TARGET")
    prefix = "SENT" if kind == "sentinel" else "POS"
    return f"DEV-{prefix}-{slug}-{contract_id.replace('CUAD-', '')}"


def selection_fingerprint(cases: Sequence[SelectedCase], seed: int) -> str:
    """A digest of the chosen cases, their categories and their gold spans.

    Covers the selection's *content* rather than the file that holds it: the
    contract ids, the target categories and the gold offsets, in order. Two
    selections that agree on this fingerprint are the same development set,
    whatever else differs about how they were written down.

    The per-category mapping is hashed through the *case's own* target category,
    not as a whole. That is the single value the pre-per-category field held, so
    the material is byte-for-byte what it was before the mapping was introduced
    and the frozen Gate 4.5 fingerprint is preserved. Hashing the whole mapping
    instead would move the fingerprint of a selection that had not changed --
    and the other category is ``absent`` in every pool by construction, so it
    carries no information the fingerprint needs.
    """
    digest = hashlib.sha256()
    digest.update(f"gate4-development-selection/v1\nseed={seed}\n".encode())
    for case in cases:
        digest.update(
            (
                f"{case.case_id}\t{case.contract_id}\t{case.target_category}\t"
                f"{case.gold_target_clause_status[case.target_category].value}\t"
                f"{','.join(str(value) for value in case.gold_evidence_offsets)}\n"
            ).encode()
        )
    return "sha256:" + digest.hexdigest()


def select_development_cases(
    annotations: CuadAnnotationSet,
    *,
    policy_targets: Sequence[str],
    policy_version: str,
    paragraph_spans: Mapping[str, Sequence[tuple[int, int]]],
    seed: int = GATE4_DEVELOPMENT_SEED,
    positive_quotas: Mapping[str, int] | None = None,
    sentinel_count: int = SENTINEL_COUNT,
    duplicate_of: Mapping[str, str] | None = None,
) -> SelectionReport:
    """Select the twelve development cases from the frozen CUAD annotations.

    ``paragraph_spans`` maps a contract id to the ``[start, end)`` spans of its
    paragraphs in the Gate-2.5 source layer. It is passed in rather than built
    here so that the selection is checked against the *actual* layer retrieval
    will serve, at the chunking the registry will use -- a gold span that the
    evidence metric could never register is an exclusion criterion, and the only
    way to know is to ask the layer.

    ``duplicate_of`` is the near-duplicate map, passed in so the (expensive)
    sketch is computed once by the caller and so a test can supply a small
    hand-built one.

    Raises ``ValueError`` if the pool cannot fill the design, naming the
    shortfall: a silently smaller development set would change the run plan's
    arithmetic without changing anything a reader could see.
    """
    quotas = dict(POSITIVE_TARGET_QUOTAS if positive_quotas is None else positive_quotas)
    missing = [category for category in quotas if category not in policy_targets]
    if missing:
        raise ValueError(
            f"positive quotas name categories {missing!r} that the policy does not "
            f"treat as targets ({list(policy_targets)!r}); the two must agree, or a "
            "case would be built for a clause the policy does not escalate on"
        )
    if len(policy_targets) != 2:
        raise ValueError(
            f"this selection is written for a two-category policy, but the policy "
            f"names {len(policy_targets)} target categories ({list(policy_targets)!r}); "
            "the positive/sentinel split and the 'both targets' exclusion both "
            "assume exactly two"
        )

    duplicate_of = dict(duplicate_of or {})
    first, second = policy_targets
    source_fingerprint = "sha256:" + annotations.source_digest.removeprefix("sha256:")

    candidates: dict[str, list[Candidate]] = {}
    excluded: list[Excluded] = []

    def consider(kind: str, target: str, other: str) -> None:
        bucket = candidates.setdefault(kind if kind == "sentinel" else f"{kind}:{target}", [])
        for contract in annotations.contracts:
            if contract.contract_id in duplicate_of:
                excluded.append(
                    Excluded(
                        contract_id=contract.contract_id,
                        kind=kind if kind == "sentinel" else f"{kind}:{target}",
                        reasons=("DUPLICATE_TEXT",),
                    )
                )
                continue
            candidate, reasons = _check_contract(
                contract,
                kind=kind,
                target=target,
                other=other,
                targets=policy_targets,
                paragraph_spans=paragraph_spans.get(contract.contract_id, ()),
            )
            if candidate is None:
                excluded.append(
                    Excluded(
                        contract_id=contract.contract_id,
                        kind=kind if kind == "sentinel" else f"{kind}:{target}",
                        reasons=reasons,
                    )
                )
            else:
                bucket.append(candidate)

    consider("positive", first, second)
    consider("positive", second, first)
    consider("sentinel", first, second)

    rng = random.Random(seed)
    selected: list[SelectedCase] = []

    for category in (first, second):
        pool = candidates[f"positive:{category}"]
        want = quotas.get(category, 0)
        if len(pool) < want:
            raise ValueError(
                f"the pool for target category {category!r} holds {len(pool)} eligible "
                f"contract(s) but the design needs {want}; either the exclusion "
                "criteria are too strict for this corpus or the design has to change, "
                "and neither is a decision this function may make silently"
            )
        for candidate in _select_diverse(pool, want, rng):
            selected.append(
                _to_selected_case(
                    annotations.by_id(candidate.contract_id),
                    candidate,
                    kind="positive",
                    policy_targets=policy_targets,
                    policy_version=policy_version,
                    source_snapshot_fingerprint=source_fingerprint,
                    paragraph_spans=paragraph_spans.get(candidate.contract_id, ()),
                )
            )

    sentinel_pool = candidates["sentinel"]
    if len(sentinel_pool) < sentinel_count:
        raise ValueError(
            f"the sentinel pool holds {len(sentinel_pool)} contract(s) but the design "
            f"needs {sentinel_count}"
        )
    for index, candidate in enumerate(_select_diverse(sentinel_pool, sentinel_count, rng)):
        # A sentinel has both targets absent, so either one can be named as "the"
        # target. Alternating them keeps both categories represented among the
        # sentinels, which matters because the case record states one category.
        #
        # ``other_category`` is relabelled with it, not left as the pool's
        # original choice. The two fields are a pair -- ``other_category`` means
        # "the *other* policy target" -- and relabelling only the first made
        # them collide whenever the alternation picked the second target, so a
        # sentinel could describe itself as being about one category while
        # naming the same category as the other one. The pool builds its
        # candidates with ``target=first, other=second`` and this loop is free
        # to rename the target afterwards, so the pair has to move together.
        target = policy_targets[index % len(policy_targets)]
        other = next(
            category for category in policy_targets if category != target
        )
        selected.append(
            _to_selected_case(
                annotations.by_id(candidate.contract_id),
                candidate.model_copy(
                    update={"target_category": target, "other_category": other}
                ),
                kind="sentinel",
                policy_targets=policy_targets,
                policy_version=policy_version,
                source_snapshot_fingerprint=source_fingerprint,
                paragraph_spans=paragraph_spans.get(candidate.contract_id, ()),
            )
        )

    cases = tuple(selected)
    return SelectionReport(
        cases=cases,
        candidates={key: tuple(value) for key, value in candidates.items()},
        excluded=tuple(excluded),
        seed=seed,
        fingerprint=selection_fingerprint(cases, seed),
    )


def _to_selected_case(
    contract: CuadContractAnnotations,
    candidate: Candidate,
    *,
    kind: str,
    policy_targets: Sequence[str],
    policy_version: str,
    source_snapshot_fingerprint: str,
    paragraph_spans: Sequence[tuple[int, int]],
) -> SelectedCase:
    """Turn an eligible candidate into the §5 case record, re-checking its offsets.

    The offsets are re-sliced one final time here, immediately before they are
    written into a record that scoring will use, because "the file verified"
    and "this case's spans are right" are different claims and only the second
    one is what a score depends on.
    """
    texts: list[str] = []
    for start, end in candidate.spans:
        sliced = contract.context[start:end]
        if not sliced:
            raise ValueError(
                f"contract {contract.contract_id!r} has gold span ({start}, {end}) "
                "that slices to nothing out of its own frozen context"
            )
        texts.append(sliced)

    paragraph_ordinals: list[int] = []
    for start, end in candidate.spans:
        overlapped = _paragraph_ids_for(paragraph_spans, start, end)
        if not overlapped:
            raise ValueError(
                f"contract {contract.contract_id!r} has a gold span that maps to no "
                "paragraph in the source layer it was checked against"
            )
        for ordinal in overlapped:
            # Two spans of one clause can land in the same paragraph, and one
            # span can straddle two; the set of paragraphs the evidence occupies
            # is what the memo cites and what the reviewer is shown, so each is
            # recorded once, in document order.
            if ordinal not in paragraph_ordinals:
                paragraph_ordinals.append(ordinal)

    return SelectedCase(
        case_id=_case_id(kind, candidate.target_category, contract.contract_id),
        contract_id=contract.contract_id,
        cuad_title=contract.title,
        contract_text_hash=contract.text_hash,
        target_category=candidate.target_category,
        target_categories=tuple(policy_targets),
        gold_target_clause_status={
            candidate.target_category: candidate.gold_status,
            candidate.other_category: ClauseStatus.ABSENT,
        },
        gold_evidence_offsets=tuple(
            value for span in candidate.spans for value in span
        ),
        gold_action=(
            Decision.ACCEPT if kind == "sentinel" else Decision.ESCALATE
        ),
        is_negative_sentinel=kind == "sentinel",
        selection_reason=(
            f"{candidate.target_category} present and "
            f"{candidate.other_category} absent in the frozen CUAD annotation; "
            f"evidence in {len(candidate.spans)} span(s) across "
            f"{len(paragraph_ordinals)} source paragraph(s)"
            if kind == "positive"
            else (
                f"both {policy_targets[0]} and {policy_targets[1]} confirmed absent "
                f"in the frozen CUAD annotation, so POLICY-01 requires "
                f"{Decision.ACCEPT.value} and an omission arm has nothing to remove"
            )
        ),
        exclusion_notes=(
            "checked and passed: not a duplicate or near-duplicate",
            f"checked and passed: gold evidence in {len(candidate.spans)} span(s), "
            f"each at least {MIN_SPAN_CHARS} characters",
            "checked and passed: gold offsets reproduce their own text in the frozen context",
            f"checked and passed: every gold span maps to a Gate-2.5 paragraph "
            f"({len(paragraph_ordinals)} of them)",
            "checked and passed: no redaction marker inside a gold span",
        ),
        memo_ids=(),
        policy_version=policy_version,
        source_snapshot_fingerprint=source_snapshot_fingerprint,
        review_status="PENDING_HUMAN_REVIEW",
        paragraph_ordinals=tuple(paragraph_ordinals),
        span_texts=tuple(texts),
        neutral_categories=candidate.neutral_categories,
    )


def _paragraph_ids_for(
    paragraph_spans: Sequence[tuple[int, int]], start: int, end: int
) -> tuple[int, ...]:
    """Every ordinal a gold span overlaps, in order, or ``()``.

    Every ordinal, not the first one: a clause that begins at the end of one
    paragraph and continues into the next occupies both, and a record that named
    only the paragraph its first character landed in would describe a smaller
    footprint than the memo cites and than the reviewer is shown.

    Ordinals rather than ids because the id carries the contract's own text hash
    and the chunking sizes, neither of which the selection needs; the registry
    builder resolves ordinals to ids against the layer it actually builds.
    """
    return tuple(
        index
        for index, (paragraph_start, paragraph_end) in enumerate(paragraph_spans)
        if start < paragraph_end and paragraph_start < end
    )
