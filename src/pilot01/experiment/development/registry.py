"""Turning the twelve selected cases into a Gate-3 registry, plus the §5 record.

Two artifacts come out of here, and they are different things:

``cases_v1.json``
    A :class:`~pilot01.experiment.cases.CaseRegistry`, in the format Gate 3
    already reads. The run plan, the batch runner and the scorer consume this
    and nothing else, so Gate 4 does not change how an experiment is run -- it
    changes which cases are in it.

``development_cases_v1.json``
    The §5 case record: everything the registry file cannot hold because it is
    experimenter-side. The CUAD title, the selection reason, the exclusions
    that were checked, the claim-level provenance, the memo-pair diff, and the
    review state. This file is for a human and for the audit; nothing at run
    time reads it.

**Why the gold is re-verified here.** The selection module already checked that
every gold span reproduces its own text. This module checks it *again*, against
the text it is about to write into a case spec, because the registry is what
scoring will actually use and "the annotation verified" is not the same claim as
"the case's offsets are right". The check is cheap and the failure it prevents
is silent.

**Why the paragraph ordinals are resolved here.** Selection works in ordinals
because a paragraph id depends on the chunking the registry chooses. This module
builds the layer, then resolves every ordinal to an id and refuses the build if
any does not resolve -- so a gold span the evidence metric could never register
is a build failure rather than a run that quietly scores zero overlap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ...config import PolicyConfig
from ...schemas import ClauseStatus, Decision, ErrorCondition, MemoClaim
from ...source import ContractDocument
from ..cases import CaseRegistry, CaseSpec
from .cuad_annotations import CuadAnnotationSet
from .memoqc import MemoPairDiff, diff_memo_pair
from .memos import MEMO_ORIGIN, ClaimProvenance, MemoPlan, memo_id_for
from .selection import SelectedCase, SelectionReport

__all__ = [
    "DEVELOPMENT_RECORD_VERSION",
    "DevelopmentCaseRecord",
    "DevelopmentCaseSet",
    "build_development_set",
]


DEVELOPMENT_RECORD_VERSION = "1"


class DevelopmentCaseRecord(BaseModel):
    """§5's case record: the registry entry plus everything experimenter-side.

    Wider than :class:`~pilot01.experiment.cases.CaseSpec` on purpose. The case
    spec is the run-time shape and holds only what a run needs; this holds what
    a *reader* needs to audit the case, which is a different and larger set.
    Keeping them separate means neither has to be padded to accommodate the
    other, and means the registry file cannot accidentally grow a field that
    reaches an agent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    contract_id: str
    cuad_title: str
    contract_text_hash: str
    target_category: str
    target_categories: tuple[str, ...]
    gold_target_clause_status: dict[str, ClauseStatus]
    gold_evidence_offsets: tuple[int, ...]
    gold_evidence_texts: tuple[str, ...]
    gold_action: Decision
    is_negative_sentinel: bool
    selection_reason: str
    exclusion_notes: tuple[str, ...]
    memo_ids: tuple[str, ...]
    """One id, shared by both arms. Listed as a tuple so the record can show
    that the arms share it rather than asserting it in prose."""

    memo_origin: str
    policy_version: str
    source_snapshot_fingerprint: str
    review_status: str
    error_conditions: tuple[str, ...]
    omission_target_claim_id: str | None = None
    omission_replacement_claim_id: str | None = None
    paragraph_ids: tuple[str, ...] = ()
    """The resolved Gate-2.5 paragraph ids the gold evidence falls in. Resolved
    from the selection's ordinals against the layer this build actually made."""

    neutral_categories: tuple[str, ...] = ()
    """Non-target clause categories with usable evidence in this contract.

    The pool the E1 arm's replacement claim was drawn from. Recorded in full
    rather than only the one that was chosen, so a reviewer can see what the
    choice was a choice *among* -- and so a replacement that was the only
    available option is distinguishable from one picked out of twenty.
    """

    @property
    def span_count(self) -> int:
        return len(self.gold_evidence_offsets) // 2

    claim_provenance: tuple[ClaimProvenance, ...] = ()
    memo_pair_diff: MemoPairDiff | None = None
    """§8's machine-readable memo-pair diff. ``None`` for a sentinel, which has
    no E1 arm and therefore no pair to diff."""


class DevelopmentCaseSet(BaseModel):
    """The twelve cases, the registry they were built into, and the audit around them."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    registry: CaseRegistry
    cases: tuple[DevelopmentCaseRecord, ...]
    selection: SelectionReport
    policy: PolicyConfig

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(record.case_id for record in self.cases)

    def record(self, case_id: str) -> DevelopmentCaseRecord:
        for record in self.cases:
            if record.case_id == case_id:
                return record
        raise KeyError(case_id)

    def composition(self) -> dict[str, int]:
        return self.selection.composition()

    def expected_run_count(self, condition_count: int, repeat_count: int = 1) -> int:
        """What §11's arithmetic should come to, derived rather than hard-coded.

        Derived from the cases' own declared error conditions, so the number the
        plan is asserted against is a consequence of the design and not a
        constant someone could update in one place and forget in another.
        """
        arms = sum(
            len(record.error_conditions) for record in self.cases
        )
        return arms * condition_count * repeat_count

    def to_payload(self) -> dict:
        return {
            "gate": 4,
            "record_version": DEVELOPMENT_RECORD_VERSION,
            "status": "DEVELOPMENT",
            "note": (
                "Real CUAD contracts selected for the Gate-4 development set. "
                "DEVELOPMENT ONLY: this is not the confirmatory sample, these are "
                "not confirmatory results, and no human legal review has been "
                "performed on these cases."
            ),
            "memo_origin": MEMO_ORIGIN,
            "review_status_legend": {
                "PENDING_HUMAN_REVIEW": (
                    "no human has reviewed this case; the state Gate 4 leaves it in"
                ),
                "REVIEWED": "a human reviewed this case (never set by this code)",
                "REJECTED": "a human rejected this case (never set by this code)",
            },
            "policy": {
                "policy_id": self.policy.policy_id,
                "policy_version": self.policy.policy_version,
                "policy_status": self.policy.policy_status,
                "target_clause_categories": list(
                    self.policy.target_clause_categories
                ),
                "clause_status_mapping": {
                    status.value: decision.value
                    for status, decision in self.policy.clause_status_mapping.items()
                },
                # §3 asks the policy record to carry its own digest. It is a
                # digest over the rule's semantics rather than over the YAML, so
                # it moves when the rule moves and stays put when a comment
                # does -- see PolicyConfig.fingerprint.
                "fingerprint": self.policy.fingerprint(),
                "edge_cases": [
                    {
                        "id": entry["id"],
                        "case": entry["case"],
                        "action": str(entry["action"]),
                    }
                    for entry in self.policy.edge_cases
                ],
            },
            "selection": {
                "seed": self.selection.seed,
                "fingerprint": self.selection.fingerprint,
                "composition": self.selection.composition(),
                "pool_sizes": {
                    key: len(value)
                    for key, value in self.selection.candidates.items()
                },
                "exclusion_counts": self.selection.exclusion_counts(),
                "exclusion_criteria": self.selection.to_payload()["exclusion_criteria"],
                "excluded": [
                    entry.model_dump(mode="json") for entry in self.selection.excluded
                ],
            },
            "cases": [record.model_dump(mode="json") for record in self.cases],
        }

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return target


def _verify_gold_spans(
    case: SelectedCase, contract_text: str, annotation_texts: Sequence[str]
) -> None:
    """Re-slice every gold span against the text the case spec will carry.

    Two independent statements, both required:

    * the offsets reproduce their own text in the contract being written into
      the case spec -- so scoring compares against the characters CUAD labelled;
    * the text that comes out equals the text the annotation reader recorded --
      so the selection's copy and the registry's copy are the same gold.

    A mismatch on either is a build failure. Silently continuing would produce a
    case that looks fine and scores against the wrong span.
    """
    offsets = case.gold_evidence_offsets
    spans = [
        (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
    ]
    if len(spans) != len(annotation_texts):
        raise ValueError(
            f"case {case.case_id!r} carries {len(spans)} gold span(s) but "
            f"{len(annotation_texts)} recorded text(s)"
        )
    for (start, end), expected in zip(spans, annotation_texts):
        if not (0 <= start < end <= len(contract_text)):
            raise ValueError(
                f"case {case.case_id!r} has gold span ({start}, {end}) outside its "
                f"contract text of {len(contract_text)} characters"
            )
        actual = contract_text[start:end]
        if actual != expected:
            raise ValueError(
                f"case {case.case_id!r} gold span ({start}, {end}) slices to "
                f"{actual[:60]!r} but the CUAD annotation records {expected[:60]!r}; "
                "the offsets and the contract text disagree"
            )


def _resolve_paragraph_ids(
    case: SelectedCase, document: ContractDocument
) -> tuple[str, ...]:
    """Map the selection's paragraph ordinals onto this layer's paragraph ids."""
    resolved: list[str] = []
    for ordinal in case.paragraph_ordinals:
        if ordinal >= len(document.paragraphs):
            raise ValueError(
                f"case {case.case_id!r} cites paragraph ordinal {ordinal} of contract "
                f"{case.contract_id!r}, which has only {len(document.paragraphs)} "
                "paragraphs in the layer this build made; the chunking used for "
                "selection and the chunking used for the registry have diverged"
            )
        resolved.append(document.paragraphs[ordinal].paragraph_id)
    return tuple(resolved)


def _check_gold_maps_to_paragraph(
    case: SelectedCase, document: ContractDocument, paragraph_ids: Sequence[str]
) -> None:
    """Assert every gold span overlaps at least one resolved paragraph.

    §5 requires it and the evidence metric depends on it: an evidence-overlap
    score compares the paragraphs a node opened against the paragraphs the gold
    falls in, so a gold span that maps to nothing is a case that can never be
    credited with finding its own evidence.
    """
    offsets = case.gold_evidence_offsets
    spans = [
        (offsets[index], offsets[index + 1]) for index in range(0, len(offsets), 2)
    ]
    if not spans:
        return
    overlapping = {
        paragraph.paragraph_id
        for paragraph in document.paragraphs
        if any(
            start < paragraph.end_char and paragraph.start_char < end
            for start, end in spans
        )
    }
    missing = [paragraph_id for paragraph_id in paragraph_ids if paragraph_id not in overlapping]
    if missing or not overlapping:
        raise ValueError(
            f"case {case.case_id!r} has gold evidence that overlaps "
            f"{len(overlapping)} paragraph(s) but cites {list(paragraph_ids)}; every "
            "gold span must fall inside at least one source paragraph"
        )


def build_development_set(
    *,
    annotations: CuadAnnotationSet,
    selection: SelectionReport,
    plans: Sequence[MemoPlan],
    documents: Mapping[str, ContractDocument],
    policy: PolicyConfig,
) -> DevelopmentCaseSet:
    """Build the Gate-3 registry and the §5 records from the selected cases.

    Raises ``ValueError`` on any inconsistency rather than repairing it: every
    check below is a statement the experiment depends on, and a build that
    quietly patched one would ship a case whose gold does not mean what the
    scoring layer assumes it means.
    """
    if len(plans) != len(selection.cases):
        raise ValueError(
            f"the selection holds {len(selection.cases)} case(s) but {len(plans)} "
            "memo plan(s) were built; every selected case needs exactly one memo"
        )
    by_case = {plan.case_id: plan for plan in plans}
    if len(by_case) != len(plans):
        raise ValueError("two memo plans were built for the same case")

    specs: list[CaseSpec] = []
    records: list[DevelopmentCaseRecord] = []

    for case in selection.cases:
        plan = by_case.get(case.case_id)
        if plan is None:
            raise ValueError(f"no memo plan was built for case {case.case_id!r}")
        contract = annotations.by_id(case.contract_id)
        document = documents.get(case.contract_id)
        if document is None:
            raise ValueError(
                f"no document was built for contract {case.contract_id!r}"
            )

        _verify_gold_spans(case, contract.context, case.span_texts)
        paragraph_ids = _resolve_paragraph_ids(case, document)
        _check_gold_maps_to_paragraph(case, document, paragraph_ids)

        error_conditions = (
            (ErrorCondition.E0,)
            if case.is_negative_sentinel
            else (ErrorCondition.E0, ErrorCondition.E1)
        )

        spec = CaseSpec(
            case_id=case.case_id,
            contract_id=case.contract_id,
            contract_text=contract.context,
            target_category=case.target_category,
            policy=policy.to_policy(),
            memo=plan.memo,
            gold_target_clause_status=dict(case.gold_target_clause_status),
            gold_evidence_offsets=case.gold_evidence_offsets,
            is_negative_sentinel=case.is_negative_sentinel,
            error_conditions=error_conditions,
            omission_target_claim_id=plan.target_claim_id,
            omission_replacement_claim=plan.replacement_claim,
        )
        specs.append(spec)

        records.append(
            DevelopmentCaseRecord(
                case_id=case.case_id,
                contract_id=case.contract_id,
                cuad_title=case.cuad_title,
                contract_text_hash=spec.contract_text_hash,
                target_category=case.target_category,
                target_categories=case.target_categories,
                gold_target_clause_status=dict(case.gold_target_clause_status),
                gold_evidence_offsets=case.gold_evidence_offsets,
                gold_evidence_texts=case.span_texts,
                gold_action=spec.gold_action,
                is_negative_sentinel=case.is_negative_sentinel,
                selection_reason=case.selection_reason,
                exclusion_notes=case.exclusion_notes,
                memo_ids=(plan.memo.memo_id,),
                memo_origin=MEMO_ORIGIN,
                policy_version=case.policy_version,
                source_snapshot_fingerprint=case.source_snapshot_fingerprint,
                review_status=case.review_status,
                error_conditions=tuple(
                    condition.value for condition in error_conditions
                ),
                omission_target_claim_id=plan.target_claim_id,
                omission_replacement_claim_id=(
                    plan.replacement_claim.claim_id if plan.replacement_claim else None
                ),
                paragraph_ids=paragraph_ids,
                neutral_categories=case.neutral_categories,
                claim_provenance=plan.provenance,
                memo_pair_diff=None,
            )
        )

    registry = CaseRegistry(specs)

    # The diff is computed from the registry's own memos rather than from the
    # plans, so what gets recorded is a diff of the artifacts the experiment
    # will actually run -- the E1 arm is derived inside CaseRegistry, and a diff
    # taken anywhere else would be a diff of a different object.
    diffed: list[DevelopmentCaseRecord] = []
    for record in records:
        omission = registry.omission_record(record.case_id)
        if omission is None:
            diffed.append(record)
            continue
        diff = diff_memo_pair(
            registry.memo(record.case_id, ErrorCondition.E0),
            registry.memo(record.case_id, ErrorCondition.E1),
            omission,
        )
        diffed.append(record.model_copy(update={"memo_pair_diff": diff}))

    return DevelopmentCaseSet(
        registry=registry,
        cases=tuple(diffed),
        selection=selection,
        policy=policy,
    )


def assert_pair_matches_omission_record(
    registry: CaseRegistry, case_id: str
) -> None:
    """Assert the E1 arm changed exactly the claim the omission record names.

    A second, independent check on the manipulation, written against the
    registry rather than against the diff: the record says *which* claim should
    have gone, and this confirms the memo that actually exists agrees. The diff
    shows what changed; this shows it changed the right thing, and the two are
    different questions.
    """
    omission = registry.omission_record(case_id)
    if omission is None:
        raise ValueError(f"case {case_id!r} has no omission record, so it has no E1 arm")
    e0 = registry.memo(case_id, ErrorCondition.E0)
    e1 = registry.memo(case_id, ErrorCondition.E1)

    if omission.omitted_claim_id in e1.claim_ids():
        raise ValueError(
            f"case {case_id!r}: the E1 memo still contains the claim the omission "
            f"record says was omitted ({omission.omitted_claim_id!r})"
        )
    if omission.is_substitution:
        replacement_id = omission.replacement_claim_id
        replacement: MemoClaim | None = e1.claim(replacement_id or "")
        if replacement is None:
            raise ValueError(
                f"case {case_id!r}: the omission record names "
                f"{replacement_id!r} as the replacement but the E1 memo does not "
                "hold it"
            )
        if replacement.category != omission.replacement_category:
            raise ValueError(
                f"case {case_id!r}: the replacement claim's category is "
                f"{replacement.category!r} but the record says "
                f"{omission.replacement_category!r}"
            )
        if e1.claim_ids().index(replacement.claim_id) != omission.replacement_index:
            raise ValueError(
                f"case {case_id!r}: the replacement sits at index "
                f"{e1.claim_ids().index(replacement.claim_id)} but the record says "
                f"{omission.replacement_index}"
            )
    if e0.memo_id != e1.memo_id:
        raise ValueError(
            f"case {case_id!r}: the two arms carry different memo ids, so a "
            "downstream agent could tell them apart"
        )
    if e0.claim_ids() and len(set(e1.claim_ids())) != len(e1.claim_ids()):
        raise ValueError(f"case {case_id!r}: the E1 memo holds a duplicate claim id")
