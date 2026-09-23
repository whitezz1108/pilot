"""Gate 4: building the first real development set from the CUAD corpus.

Everything here is **development**. Gate 4 moves the pilot off synthetic
fixtures and onto real contracts, which is a large enough step that it needs its
own vocabulary: a real contract has a real title, a real annotation with real
offsets, a real clause set with real neighbours, and a real possibility of being
a near-duplicate of the contract next to it. This subpackage holds that
vocabulary.

**Nothing here is agent-facing.** The modules in this package read CUAD's gold
annotations, build case records, and render a review pack for a human. No node,
no source-layer module and no model module imports any of it -- a test asserts
that -- so a gold label or a contract title cannot reach a prompt by accident.

**The pipeline, in order:**

``cuad_annotations``
    Read the frozen CUAD file's clause annotations, with the offsets checked
    against the text they claim to describe.
``selection``
    Apply the declared exclusion criteria to the whole corpus and pick twelve
    cases -- four of each target category, four sentinels -- with a declared
    seed, before any model is involved.
``memos``
    Build each case's E0 memo from the annotation, and the neutral claim the E1
    arm substitutes for the omitted one.
``registry``
    Turn the selection and the memos into a Gate-3 case registry plus the
    experimenter-side case record.
``memoqc``
    Diff each memo pair and check it against the matched-omission requirements.
``review``
    Render the human review pack, and keep the absence of human review visible.
``diagnostics``
    Read the scorer's rows back as development diagnostics, with a verdict
    computed from checks declared before the run.

**This is not the confirmatory gate.** The cases are a development set, the
memos are researcher-constructed, no human has reviewed the cases, and no
hypothesis is tested on any of it.
"""

from __future__ import annotations

from .cuad_annotations import (
    CuadAnnotationError,
    CuadAnnotationSet,
    CuadClause,
    CuadContractAnnotations,
    load_annotations,
)
from .diagnostics import (
    DevelopmentDiagnostics,
    build_diagnostics,
    render_development_table,
)
from .build import DevelopmentBuild, build_gate4_development_set, memo_qc_for
from .memoqc import MemoPairDiff, MemoQCReport, build_memo_qc, diff_memo_pair
from .memos import MEMO_ORIGIN, MemoPlan, build_memo_plan, build_memo_plans
from .registry import DevelopmentCaseRecord, DevelopmentCaseSet, build_development_set
from .review import render_review_pack, write_review_pack
from .selection import (
    GATE4_DEVELOPMENT_SEED,
    SelectedCase,
    SelectionReport,
    near_duplicate_groups,
    select_development_cases,
    selection_fingerprint,
)

__all__ = [
    "CuadAnnotationError",
    "CuadAnnotationSet",
    "CuadClause",
    "CuadContractAnnotations",
    "load_annotations",
    "GATE4_DEVELOPMENT_SEED",
    "SelectedCase",
    "SelectionReport",
    "near_duplicate_groups",
    "select_development_cases",
    "selection_fingerprint",
    "MEMO_ORIGIN",
    "MemoPlan",
    "build_memo_plan",
    "build_memo_plans",
    "DevelopmentCaseRecord",
    "DevelopmentCaseSet",
    "build_development_set",
    "DevelopmentBuild",
    "build_gate4_development_set",
    "memo_qc_for",
    "MemoPairDiff",
    "MemoQCReport",
    "build_memo_qc",
    "diff_memo_pair",
    "render_review_pack",
    "write_review_pack",
    "DevelopmentDiagnostics",
    "build_diagnostics",
    "render_development_table",
]
