"""Where an experiment's files live.

One object owns the layout, so the writer, the scorer and the CLI cannot
disagree about a path. The tree is three levels deep and each level answers a
different question:

``manifests/<experiment_id>/``
    The frozen plan: ``run_plan.json`` (what the code reads back) and
    ``run_plan.csv`` (what a person reads). Written once, before anything runs.
``raw/<experiment_id>/<run_id>/``
    One directory per run, holding that run's logs and its ``run.json``.
``processed/<experiment_id>/``
    Everything derived: the per-run scores and the experiment summary.

**The split between ``raw`` and ``processed`` is load-bearing.** Raw artifacts
are the record; processed artifacts are a projection of it, regenerable at any
time and never read back as an input. Keeping them in different trees is what
makes "delete the processed results and rebuild them" a safe, routine operation
rather than a nervous one -- nothing under ``raw`` can be reached by a bug in a
scoring pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MANIFEST_DIR",
    "RAW_DIR",
    "PROCESSED_DIR",
    "RUN_PLAN_JSON",
    "RUN_PLAN_CSV",
    "BATCH_INDEX_JSON",
    "RUN_SCORES_JSONL",
    "RUN_SCORES_CSV",
    "EXPERIMENT_SUMMARY_JSON",
    "ExperimentPaths",
]

MANIFEST_DIR = "manifests"
RAW_DIR = "raw"
PROCESSED_DIR = "processed"

RUN_PLAN_JSON = "run_plan.json"
RUN_PLAN_CSV = "run_plan.csv"
BATCH_INDEX_JSON = "batch_index.json"
RUN_SCORES_JSONL = "run_scores.jsonl"
RUN_SCORES_CSV = "run_scores.csv"
EXPERIMENT_SUMMARY_JSON = "experiment_summary.json"


@dataclass(frozen=True)
class ExperimentPaths:
    """The paths of one experiment, under one output root."""

    root: Path
    experiment_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    @property
    def manifest_dir(self) -> Path:
        return self.root / MANIFEST_DIR / self.experiment_id

    @property
    def raw_dir(self) -> Path:
        return self.root / RAW_DIR / self.experiment_id

    @property
    def processed_dir(self) -> Path:
        return self.root / PROCESSED_DIR / self.experiment_id

    def run_dir(self, run_id: str) -> Path:
        return self.raw_dir / run_id

    @property
    def run_plan_json(self) -> Path:
        return self.manifest_dir / RUN_PLAN_JSON

    @property
    def run_plan_csv(self) -> Path:
        return self.manifest_dir / RUN_PLAN_CSV

    @property
    def batch_index_json(self) -> Path:
        return self.manifest_dir / BATCH_INDEX_JSON

    @property
    def run_scores_jsonl(self) -> Path:
        return self.processed_dir / RUN_SCORES_JSONL

    @property
    def run_scores_csv(self) -> Path:
        return self.processed_dir / RUN_SCORES_CSV

    @property
    def summary_json(self) -> Path:
        return self.processed_dir / EXPERIMENT_SUMMARY_JSON

    def ensure(self) -> "ExperimentPaths":
        """Create the three trees. Safe to call repeatedly."""
        for directory in (self.manifest_dir, self.raw_dir, self.processed_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self
