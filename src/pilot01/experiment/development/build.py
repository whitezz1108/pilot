"""The Gate-4 development pipeline, in one place.

Four offline steps turn the frozen CUAD annotations into a case registry:

    annotations -> source layer -> selection -> memos -> registry + §5 records

Every one of them is deterministic and none of them calls a model, so the whole
thing can be rebuilt at any time and must come out identical. It lives here
rather than in the build script because more than one thing needs the same set:
the driver that writes the artifacts, the review that checks the prompts against
the real memos, and the tests. A second copy of this sequence would be a second
answer to "what is the Gate-4 development set", and the two could drift.

Nothing in this module reads a credential, opens a socket, or writes a file. It
returns objects; the caller decides where they go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ...config import PolicyConfig, load_policy_v2
from ...source import ContractDocument, build_document
from .cuad_annotations import CuadAnnotationSet, load_annotations
from .memoqc import MemoPairDiff, MemoQCReport, build_memo_qc
from .memos import MemoPlan, build_memo_plans
from .registry import DevelopmentCaseSet, build_development_set
from .selection import (
    GATE4_DEVELOPMENT_SEED,
    SelectionReport,
    near_duplicate_groups,
    select_development_cases,
)

__all__ = ["DevelopmentBuild", "build_gate4_development_set", "memo_qc_for"]


@dataclass(frozen=True)
class DevelopmentBuild:
    """Everything the offline build produced, kept together.

    ``documents`` and ``clause_spans`` are carried alongside the case set
    because the memo QC and the review pack both need the paragraph layer the
    selection was made against, and rebuilding it would risk building a
    different one.
    """

    annotations: CuadAnnotationSet
    policy: PolicyConfig
    documents: Mapping[str, ContractDocument]
    paragraph_spans: Mapping[str, list[tuple[int, int]]]
    clause_spans: Mapping[str, dict[str, list[tuple[int, int]]]]
    duplicate_of: Mapping[str, str]
    selection: SelectionReport
    plans: tuple[MemoPlan, ...]
    case_set: DevelopmentCaseSet

    @property
    def registry(self):
        return self.case_set.registry


def build_gate4_development_set(
    *,
    annotations: CuadAnnotationSet | None = None,
    seed: int = GATE4_DEVELOPMENT_SEED,
    policy: PolicyConfig | None = None,
) -> DevelopmentBuild:
    """Build the twelve-case development set, offline and deterministically.

    The seed is the selection seed and defaults to the declared one. It is a
    parameter so a test can prove that a different seed produces a different but
    equally valid set, not so that a run can quietly pick a more convenient one.

    ``policy`` defaults to the *current* revision of POLICY-01, not to the one
    the Gate-4 batch ran under. The case content is the same either way -- the
    two revisions differ only in the action for an unresolved clause, and a gold
    label is never unresolved -- so the twelve cases, their gold spans and their
    selection fingerprint are identical. What differs is the rule object the
    cases carry, and that object is what renders the POLICY block into every
    prompt. A default that pinned revision 1 would therefore state revision 1's
    rule in a prompt that a revision-2 run is scored against, which is the one
    way this build could silently produce a run that cannot be scored.

    A caller that needs the Gate-4 set as Gate 4 built it passes
    ``policy=load_policy_v1()`` explicitly. That is what the Gate-4 audit
    scripts do, so their frozen fingerprints keep checking the artifacts they
    were frozen against.
    """
    annotations = annotations if annotations is not None else load_annotations()
    policy = policy if policy is not None else load_policy_v2()
    runtime_policy = policy.to_policy()

    documents: dict[str, ContractDocument] = {}
    paragraph_spans: dict[str, list[tuple[int, int]]] = {}
    clause_spans: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for contract in annotations.contracts:
        document = build_document(contract.contract_id, contract.context)
        documents[contract.contract_id] = document
        paragraph_spans[contract.contract_id] = [
            (paragraph.start_char, paragraph.end_char)
            for paragraph in document.paragraphs
        ]
        clause_spans[contract.contract_id] = {
            clause.category: clause.spans for clause in contract.clauses
        }

    duplicate_of = near_duplicate_groups(
        {contract.contract_id: contract.context for contract in annotations.contracts}
    )

    selection = select_development_cases(
        annotations,
        policy_targets=runtime_policy.target_clause_categories,
        policy_version=runtime_policy.policy_version,
        paragraph_spans=paragraph_spans,
        duplicate_of=duplicate_of,
        seed=seed,
    )

    plans = build_memo_plans(
        selection.cases,
        documents=documents,
        clause_spans=clause_spans,
        seed=selection.seed,
    )

    case_set = build_development_set(
        annotations=annotations,
        selection=selection,
        plans=plans,
        documents=documents,
        policy=policy,
    )

    return DevelopmentBuild(
        annotations=annotations,
        policy=policy,
        documents=documents,
        paragraph_spans=paragraph_spans,
        clause_spans=clause_spans,
        duplicate_of=duplicate_of,
        selection=selection,
        plans=tuple(plans),
        case_set=case_set,
    )


def memo_qc_for(case_set: DevelopmentCaseSet) -> tuple[MemoQCReport, tuple[MemoPairDiff, ...]]:
    """The §9 memo QC, and the diffs it was computed from."""
    diffs = tuple(
        record.memo_pair_diff
        for record in case_set.cases
        if record.memo_pair_diff is not None
    )
    qc = build_memo_qc(
        diffs,
        sentinel_case_ids=[
            record.case_id for record in case_set.cases if record.is_negative_sentinel
        ],
    )
    return qc, diffs
