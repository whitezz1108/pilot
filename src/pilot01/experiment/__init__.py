"""The experiment layer: many runs, one plan, and the scoring that follows.

This package sits *above* :mod:`pilot01.workflow`. It knows how to enumerate an
experiment, execute it, keep every raw artifact, and derive outcomes from them.
Nothing below it knows this package exists -- the dependency runs one way only,
and a test parses the modules under :mod:`pilot01.workflow`,
:mod:`pilot01.source` and :mod:`pilot01.model` to prove that no import points
back here.

The layers, and what each one is the authority for:

``cases``
    What a case is, and where its gold lives. The registry is the frozen
    experimental input.
``runspec``
    The run unit and the plan: stable identities, a reproducible randomized
    order, and a fingerprint over both.
``artifacts``
    What a run leaves on disk, and how it is read back. Raw only; no gold.
``batch``
    Executing a plan, serially, one directory per run, overwriting nothing.
``scoring``
    Deriving one tidy row per run from its raw artifacts and its case's labels.
``summary``
    Counting the rows, with every rate carrying its denominator.
``layout``
    Where all of it lives.
``cli``
    The four commands, none of which spends money by default.

Two invariants are worth stating here because they cut across the modules:

**Scoring cannot affect the workflow.** The dependency direction is the proof:
the workflow layer cannot call into this one, because it does not import it.
Gold is read only after a run has finished, from the registry, never from a
runtime object an agent could reach.

**The raw tree is the record; everything else is a projection.** A scored row
and a summary are regenerable from ``raw/`` at any time, by code that has never
seen the plan. That is what makes it safe to delete and rebuild the processed
tree, and it is tested by doing exactly that.
"""

from __future__ import annotations

from .artifacts import (
    ARTIFACT_VERSION,
    ArtifactError,
    FailureRecord,
    RawRun,
    RunArtifact,
    RunDirectory,
    derive_failure,
    iter_raw_runs,
    read_raw_run,
)
from .batch import (
    BatchError,
    BatchResult,
    ClientFactory,
    ExperimentRunner,
    JobResult,
    JobStatus,
    ScriptedClientFactory,
    load_response_script,
    plan_or_raise,
)
from .cases import (
    REGISTRY_VERSION,
    CaseRegistry,
    CaseRegistryError,
    CaseSpec,
    gold_offsets_for_paragraph,
    is_safe_component,
)
from .layout import ExperimentPaths
from .runspec import (
    EXPERIMENT_VERSION,
    PLAN_VERSION,
    RUN_PLAN_COLUMNS,
    RunPlan,
    RunPlanError,
    RunSpec,
    build_run_plan,
    make_run_id,
    model_fingerprint,
    write_run_plan_csv,
)
from .scoring import (
    SCORE_SCHEMA_VERSION,
    CorrectionStage,
    EvidenceAssessment,
    EvidenceFailure,
    ExperimentScores,
    PricingTable,
    RunScore,
    ScoringError,
    score_experiment,
    score_run,
    spans_overlap,
    write_scores_csv,
)
from .summary import (
    SUMMARY_VERSION,
    CellSummary,
    ConditionErrorCount,
    ConditionSummary,
    ExperimentSummary,
    QCSummary,
    Rate,
    summarise,
)

__all__ = [
    # cases
    "REGISTRY_VERSION",
    "CaseRegistry",
    "CaseRegistryError",
    "CaseSpec",
    "gold_offsets_for_paragraph",
    "is_safe_component",
    # runspec
    "PLAN_VERSION",
    "EXPERIMENT_VERSION",
    "RUN_PLAN_COLUMNS",
    "RunPlan",
    "RunPlanError",
    "RunSpec",
    "build_run_plan",
    "make_run_id",
    "model_fingerprint",
    "write_run_plan_csv",
    # artifacts
    "ARTIFACT_VERSION",
    "ArtifactError",
    "FailureRecord",
    "RawRun",
    "RunArtifact",
    "RunDirectory",
    "derive_failure",
    "iter_raw_runs",
    "read_raw_run",
    # batch
    "BatchError",
    "BatchResult",
    "ClientFactory",
    "ExperimentRunner",
    "JobResult",
    "JobStatus",
    "ScriptedClientFactory",
    "load_response_script",
    "plan_or_raise",
    # layout
    "ExperimentPaths",
    # scoring
    "SCORE_SCHEMA_VERSION",
    "CorrectionStage",
    "EvidenceAssessment",
    "EvidenceFailure",
    "ExperimentScores",
    "PricingTable",
    "RunScore",
    "ScoringError",
    "score_experiment",
    "score_run",
    "spans_overlap",
    "write_scores_csv",
    # summary
    "SUMMARY_VERSION",
    "CellSummary",
    "ConditionSummary",
    "ConditionErrorCount",
    "QCSummary",
    "ExperimentSummary",
    "Rate",
    "summarise",
]
