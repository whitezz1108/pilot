"""Building the E0 memo for a real case, and the neutral claim the E1 arm substitutes.

**What this is, stated plainly.** These memos are *researcher-constructed*, not
the output of an independent analyst agent. They are deterministic functions of
the frozen CUAD annotation: a claim exists because CUAD records the clause as
present, and it cites the paragraph the annotation's offsets fall in. That is a
weaker thing than "an analyst read the contract and wrote this up", and every
artifact this module produces is labelled accordingly -- the memo's own
``memo_id`` carries the ``DEV`` marker, and the development manifest records
``memo_origin`` next to it. Gate 3 established an offline, deterministic Analyst
path (a frozen-artifact loader rather than a model call); this module fills that
path with real content instead of building a second agent architecture for it.

**Why a fixed five-claim shape.** Every memo holds the same five categories in
the same order: the two policy target categories, then three non-target clauses
from the same contract. Two consequences matter for the experiment:

* the policy is *decidable* from the memo alone -- both target categories are
  always stated, so "present" and "confirmed absent" are both on the page and
  neither is an artefact of what the memo happened to mention;
* the two arms differ by exactly one claim. A fixed shape means the E1 memo is
  the E0 memo with one entry changed, not a shorter document, and §8's
  same-count requirement is a property of the construction rather than
  something to be checked afterwards and hoped for.

**Summaries are paraphrases, never quotations.** No claim's ``summary`` copies
contract text. That is deliberate: an A1V1 run is supposed to *verify* a claim
against the source, and a memo that already contained the clause verbatim would
make that verification a string comparison against something the agent was
handed. The claim states what the analyst concluded and cites where; the
wording is in the source, where a run with tools can go and read it.

**Absent claims cite nothing.** A claim that a clause is absent has no passage
to point at, so its ``source_ids`` is empty. This is not a marker for the
omission treatment -- every memo, E0 and E1 alike, states both target categories
and both are absent-or-present in the same way -- it is simply what an assertion
of absence looks like.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from ...schemas import AnalystMemo, ClauseStatus, MemoClaim
from ...source import ContractDocument
from .selection import MIN_NEUTRAL_CATEGORIES, SelectedCase

__all__ = [
    "CONTEXT_CLAIM_COUNT",
    "MEMO_ORIGIN",
    "SUMMARY_PRESENT",
    "SUMMARY_ABSENT",
    "ClaimProvenance",
    "MemoPlan",
    "memo_id_for",
    "build_memo_plan",
    "build_memo_plans",
]


CONTEXT_CLAIM_COUNT = 3
"""Non-target claims each memo carries beside the two target claims."""

MEMO_ORIGIN = (
    "DEVELOPMENT: researcher-constructed from the frozen CUAD annotation; this is "
    "not an independently generated analyst judgment, and no analyst agent produced it"
)
"""The label every Gate-4 memo carries in the development manifest.

Written down once, here, so that no report, table or downstream artifact has to
characterise these memos from memory. §7 allows a deterministic
researcher-constructed development memo *provided* the distinction from an
independently generated analyst judgment is preserved in metadata; this string
is that preservation.
"""

SUMMARY_PRESENT = (
    "{category}: the analyst records this provision as present in the contract, "
    "and cites the source passage it was read from."
)
"""Template for a claim of presence. ``source_ids`` names where the passage is."""

SUMMARY_ABSENT = (
    "{category}: the analyst records no provision of this kind in the contract. "
    "The category was searched for and not found."
)
"""Template for a claim of absence. Paraphrased, and with no passage to cite."""


class ClaimProvenance(BaseModel):
    """Where one memo claim came from, in CUAD's own terms.

    Experimenter-side. It exists so that a reviewer can ask "why does the memo
    say this?" and get an answer that does not require re-running the builder,
    and so that the E1 manipulation can be audited claim by claim rather than
    only in aggregate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    category: str
    status: ClauseStatus
    origin: str
    """``TARGET``, ``OTHER_TARGET``, ``CONTEXT`` or ``REPLACEMENT``."""

    cuad_category: str
    """The CUAD question category this claim was read from. Equal to ``category``
    for every claim this builder makes, and stated rather than assumed so a
    future builder that renamed a category would have to say so."""

    gold_offsets: tuple[int, ...] = ()
    paragraph_ordinals: tuple[int, ...] = ()
    evidence_chars: int = Field(default=0, ge=0)


class MemoPlan(BaseModel):
    """One case's memo, its omission target, and the claim that replaces it.

    ``target_claim_id`` and ``replacement_claim`` are ``None`` for a sentinel.
    §8 is explicit that a sentinel gets E0 only -- there is no present target
    clause for an omission to remove, and manufacturing one would be inventing
    a manipulation the contract does not support.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    memo: AnalystMemo
    target_claim_id: str | None = None
    replacement_claim: MemoClaim | None = None
    provenance: tuple[ClaimProvenance, ...] = ()
    neutral_pool: tuple[str, ...] = ()
    """Every usable non-target category, so the review pack can show what the
    replacement was chosen from and not only what was chosen."""

    @property
    def is_paired(self) -> bool:
        return self.target_claim_id is not None

    def provenance_for(self, claim_id: str) -> ClaimProvenance | None:
        for record in self.provenance:
            if record.claim_id == claim_id:
                return record
        return None


def memo_id_for(case_id: str) -> str:
    """The memo id, shared by both arms.

    §8 requires the two arms to be indistinguishable apart from the one claim,
    and :class:`~pilot01.schemas.AnalystMemo` deliberately carries no arm
    marker. Minting the id from the case alone -- not from the case and the arm
    -- is what makes that structural rather than a convention someone could
    forget.
    """
    return f"{case_id}-MEMO"


def _claim_id(category: str) -> str:
    """A stable claim id derived from the category.

    Derived rather than counted, so that a change to the memo's shape cannot
    silently renumber a claim and change which one an omission record names.
    """
    slug = "".join(character if character.isalnum() else "-" for character in category)
    return "C-" + "-".join(part for part in slug.split("-") if part).upper()


def _paragraph_ids_for(
    document: ContractDocument, offsets: Sequence[tuple[int, int]]
) -> tuple[str, ...]:
    """The ids of every paragraph a span set overlaps, in document order.

    Overlap rather than containment: a CUAD answer span can begin mid-paragraph
    or run across a boundary, and the paragraph a node would have to open to
    read that text is any paragraph the span touches. Requiring containment
    would drop real citations for a reason that has nothing to do with the case.
    """
    ids: list[str] = []
    for paragraph in document.paragraphs:
        if any(
            start < paragraph.end_char and paragraph.start_char < end
            for start, end in offsets
        ):
            ids.append(paragraph.paragraph_id)
    return tuple(ids)


def _span_length(spans: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in spans)


def build_memo_plan(
    case: SelectedCase,
    *,
    document: ContractDocument,
    clause_spans: dict[str, tuple[tuple[int, int], ...]],
    seed: int,
) -> MemoPlan:
    """Build one case's E0 memo, its omission target and its replacement claim.

    ``clause_spans`` maps a CUAD category to that contract's gold spans for it.
    It is passed in rather than read here because reading CUAD is
    :mod:`~pilot01.experiment.development.cuad_annotations`' job, and this
    module should not have a second opinion about what the annotation says.

    The three context claims and the replacement are drawn with a per-case
    seeded generator rather than taken as "the first four usable categories".
    CUAD asks its questions in a fixed order, so the first four usable
    categories would be the same four for nearly every contract -- twelve memos
    with identical context claims, which is twelve samples of one memo shape.
    """
    if len(case.neutral_categories) < MIN_NEUTRAL_CATEGORIES:
        raise ValueError(
            f"case {case.case_id!r} has {len(case.neutral_categories)} usable "
            f"non-target categories but the memo shape needs "
            f"{MIN_NEUTRAL_CATEGORIES}; the selection criteria should have kept it "
            "out of the pool"
        )

    rng = random.Random(f"gate4-memo:{seed}:{case.case_id}")
    pool = list(case.neutral_categories)
    context_categories = rng.sample(pool, CONTEXT_CLAIM_COUNT)
    remaining = [category for category in pool if category not in context_categories]

    target_span_chars = sum(end - start for start, end in case.spans())
    # Among the categories the memo does not already carry, take the one whose
    # evidence is closest in size to the omitted clause's. The summaries are
    # templates, so this does not by itself match the two arms' lengths -- it
    # keeps the *citation* comparable, so the E1 memo does not stand out by
    # pointing at a conspicuously larger or smaller passage than E0 did.
    replacement_category = min(
        remaining,
        key=lambda category: (
            abs(_span_length(clause_spans.get(category, ())) - target_span_chars),
            pool.index(category),
        ),
    )

    claims: list[MemoClaim] = []
    provenance: list[ClaimProvenance] = []

    # The two target categories, in the policy's own order, so the memo states
    # the whole policy input rather than only the part that happens to be
    # interesting for this case.
    for category in case.target_categories:
        present = category == case.target_category and not case.is_negative_sentinel
        named_target = category == case.target_category
        offsets = case.spans() if present else ()
        claim = MemoClaim(
            claim_id=_claim_id(category),
            category=category,
            status=ClauseStatus.PRESENT if present else ClauseStatus.ABSENT,
            summary=(SUMMARY_PRESENT if present else SUMMARY_ABSENT).format(category=category),
            source_ids=_paragraph_ids_for(document, offsets),
        )
        claims.append(claim)
        provenance.append(
            ClaimProvenance(
                claim_id=claim.claim_id,
                category=category,
                status=claim.status,
                origin="TARGET" if named_target else "OTHER_TARGET",
                cuad_category=category,
                gold_offsets=tuple(value for span in offsets for value in span),
                paragraph_ordinals=case.paragraph_ordinals if present else (),
                evidence_chars=target_span_chars if present else 0,
            )
        )

    for category in context_categories:
        offsets = clause_spans.get(category, ())
        claim = MemoClaim(
            claim_id=_claim_id(category),
            category=category,
            status=ClauseStatus.PRESENT,
            summary=SUMMARY_PRESENT.format(category=category),
            source_ids=_paragraph_ids_for(document, offsets),
        )
        claims.append(claim)
        provenance.append(
            ClaimProvenance(
                claim_id=claim.claim_id,
                category=category,
                status=claim.status,
                origin="CONTEXT",
                cuad_category=category,
                gold_offsets=tuple(value for span in offsets for value in span),
                evidence_chars=_span_length(offsets),
            )
        )

    replacement: MemoClaim | None = None
    target_claim_id: str | None = None
    if not case.is_negative_sentinel:
        target_claim_id = _claim_id(case.target_category)
        offsets = clause_spans.get(replacement_category, ())
        replacement = MemoClaim(
            claim_id=_claim_id(replacement_category),
            category=replacement_category,
            status=ClauseStatus.PRESENT,
            summary=SUMMARY_PRESENT.format(category=replacement_category),
            source_ids=_paragraph_ids_for(document, offsets),
        )

    return MemoPlan(
        case_id=case.case_id,
        memo=AnalystMemo(
            memo_id=memo_id_for(case.case_id),
            case_id=case.case_id,
            claims=tuple(claims),
        ),
        target_claim_id=target_claim_id,
        replacement_claim=replacement,
        provenance=tuple(provenance),
        neutral_pool=tuple(case.neutral_categories),
    )


def build_memo_plans(
    cases: Sequence[SelectedCase],
    *,
    documents: dict[str, ContractDocument],
    clause_spans: dict[str, dict[str, tuple[tuple[int, int], ...]]],
    seed: int,
) -> tuple[MemoPlan, ...]:
    """Build every case's memo plan, in case order.

    ``documents`` and ``clause_spans`` are keyed by contract id and built by the
    caller from one read of the frozen file, so a case's memo cites paragraphs
    from the same paragraphization the registry will hand to retrieval.
    """
    plans: list[MemoPlan] = []
    for case in cases:
        document = documents.get(case.contract_id)
        if document is None:
            raise ValueError(
                f"no document was built for contract {case.contract_id!r}, which "
                f"case {case.case_id!r} is about"
            )
        plans.append(
            build_memo_plan(
                case,
                document=document,
                clause_spans=clause_spans.get(case.contract_id, {}),
                seed=seed,
            )
        )
    return tuple(plans)
