"""Counting what the scored rows say, without saying more than they do.

This module aggregates. It computes frequencies and rates, and it stops there:
no confidence intervals, no regression coefficients, no p-values, no plots.
Those are the confirmatory analysis, they need the frozen prompts and the real
cases, and computing them here would put numbers in front of a reader before
the design that gives them meaning exists.

Two rules shape everything below.

**Every rate carries its denominator.** A rate is not a number, it is a pair --
:class:`Rate` refuses to be one without the other. "82% correct" over eleven
runs and over four hundred are the same string and different findings, and a
summary that reported only the first would make them indistinguishable. A rate
whose denominator is zero is ``None``, not ``0.0``: nothing was measured, which
is not the same as nothing was found.

**Unavailable is excluded from the numerator and the denominator.** A run that
produced no answer is not a wrong answer. It is counted, in its own column, as
``unanswered``, so a reader can always reconstruct how many runs a rate is
silent about. This is the §7 distinction, carried through to the aggregate
rather than being lost at the row level where it was made.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .scoring import ExperimentScores, RunScore

__all__ = [
    "SUMMARY_VERSION",
    "Rate",
    "ConditionSummary",
    "CellSummary",
    "ConditionErrorCount",
    "QCSummary",
    "ExperimentSummary",
    "summarise",
]


SUMMARY_VERSION = "1"


class Rate(BaseModel):
    """A proportion that cannot be reported without its denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @property
    def value(self) -> float | None:
        """The proportion, or ``None`` when nothing was in the denominator."""
        if self.denominator == 0:
            return None
        return round(self.numerator / self.denominator, 6)

    @property
    def measurable(self) -> bool:
        return self.denominator > 0

    @classmethod
    def of(cls, values: Iterable[bool | None]) -> "Rate":
        """A rate over the values that are not ``None``.

        The ``None``\\ s are not failures and not successes: they are the runs
        this rate says nothing about, and they are excluded rather than counted
        either way.
        """
        considered = [value for value in values if value is not None]
        return cls(numerator=sum(1 for value in considered if value), denominator=len(considered))

    def describe(self) -> str:
        if not self.measurable:
            return "n/a (no observations)"
        return f"{self.numerator}/{self.denominator} ({self.value:.1%})"


class CellSummary(BaseModel):
    """One experimental cell: case x error condition x governance condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    error_condition: str
    condition_id: str
    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    protocol_failures: int = Field(ge=0)
    unanswered: int = Field(ge=0)
    accuracy: Rate
    correction_stages: dict[str, int] = Field(default_factory=dict)


class ConditionSummary(BaseModel):
    """One governance condition, pooled over every case and repeat.

    ``error_survival_*`` is over the runs where the question applies, which is
    a subset: the E1 arm of a case whose omitted claim asserted presence. Its
    denominator is reported so the subset is visible rather than implied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_id: str
    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    protocol_failures: int = Field(ge=0)
    unanswered: int = Field(ge=0)

    accuracy: Rate
    error_survival_manager: Rate
    error_survival_compliance: Rate
    correction_stages: dict[str, int] = Field(default_factory=dict)

    manager_evidence_overlap: Rate
    compliance_evidence_overlap: Rate

    manager_verification_satisfied: Rate
    compliance_verification_satisfied: Rate

    unknown_adopted_claims: int = Field(default=0, ge=0)
    """Runs in which a node adopted a claim it was never given."""

    mean_tool_calls: float | None = None
    mean_model_calls: float | None = None
    mean_total_tokens: float | None = None
    total_estimated_cost_usd: float | None = None


class ConditionErrorCount(BaseModel):
    """The pilot's design table: one governance condition crossed with one arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_id: str
    error_condition: str
    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    accuracy: Rate
    error_survival_manager: Rate
    error_survival_compliance: Rate


class QCSummary(BaseModel):
    """§11's quality-control block: did the experiment run the way it was meant to.

    These are not findings. They are the checks that say whether the findings
    can be read at all -- a batch that lost a cell, or whose runs died at the
    parser, is a batch whose accuracy figure means something different from one
    that did not.

    The plan-derived fields are ``None`` when no plan was supplied rather than
    zero. Zero is a claim ("no cells are missing"); ``None`` is the truth ("the
    plan was not available to check against"), and a reader must be able to tell
    those apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_runs: int | None = None
    attempted_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)

    protocol_failure_rate: Rate
    structured_output_success_rate: Rate
    verification_failure_count: int = Field(ge=0)
    """Runs in which a node's verification verdict did not satisfy the condition."""

    tool_failure_count: int = Field(ge=0)
    """Runs in which at least one tool call was refused or errored."""

    missing_cells: tuple[tuple[str, str, str, int], ...] | None = None
    duplicate_cells: tuple[tuple[str, str, str, int], ...] | None = None

    plan_fingerprint: str | None = None
    """Which plan the cell checks were made against, so the QC block is bound to it."""


class ExperimentSummary(BaseModel):
    """The whole experiment, counted.

    Deliberately descriptive. Every field here is a count or a rate with its
    denominator; nothing here tests a hypothesis or estimates an effect.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary_version: str = SUMMARY_VERSION
    experiment_id: str
    score_schema_version: str
    plan_fingerprints: tuple[str, ...] = ()

    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    protocol_failures: int = Field(ge=0)
    unanswered: int = Field(ge=0)

    accuracy: Rate
    error_survival_manager: Rate
    error_survival_compliance: Rate
    correction_stages: dict[str, int] = Field(default_factory=dict)

    qc: QCSummary

    by_condition: tuple[ConditionSummary, ...] = ()
    by_condition_error: tuple[ConditionErrorCount, ...] = ()
    by_cell: tuple[CellSummary, ...] = ()

    def condition(self, condition_id: str) -> ConditionSummary:
        for summary in self.by_condition:
            if summary.condition_id == condition_id:
                return summary
        raise KeyError(f"no condition {condition_id!r} in this summary")

    def to_payload(self) -> dict:
        return json.loads(self.model_dump_json())

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2) + "\n", encoding="utf-8"
        )
        return target


def _mean(values: Sequence[int | float | None]) -> float | None:
    """The mean over the values that are present, or ``None`` if none are.

    Used only for descriptive quantities where a partial mean is meaningful --
    usage counts, not rates. A rate goes through :class:`Rate`, which refuses to
    drop its denominator.
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 6)


def _stage_counts(rows: Iterable[RunScore]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.correction_stage.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _pool(rows: Sequence[RunScore], condition_id: str) -> ConditionSummary:
    return ConditionSummary(
        condition_id=condition_id,
        runs=len(rows),
        completed=sum(1 for row in rows if row.completed),
        protocol_failures=sum(1 for row in rows if row.protocol_failure),
        unanswered=sum(1 for row in rows if not row.produced_answer),
        accuracy=Rate.of(row.correct for row in rows),
        error_survival_manager=Rate.of(row.error_survival_manager for row in rows),
        error_survival_compliance=Rate.of(row.error_survival_compliance for row in rows),
        correction_stages=_stage_counts(rows),
        manager_evidence_overlap=Rate.of(row.manager_evidence.overlaps_gold for row in rows),
        compliance_evidence_overlap=Rate.of(
            row.compliance_evidence.overlaps_gold for row in rows
        ),
        manager_verification_satisfied=Rate.of(
            row.manager_verification_satisfied for row in rows
        ),
        compliance_verification_satisfied=Rate.of(
            row.compliance_verification_satisfied for row in rows
        ),
        unknown_adopted_claims=sum(1 for row in rows if row.unknown_adopted_claims),
        mean_tool_calls=_mean([row.tool_calls for row in rows]),
        mean_model_calls=_mean([row.model_calls for row in rows]),
        mean_total_tokens=_mean([row.total_tokens for row in rows]),
        total_estimated_cost_usd=(
            None
            if any(row.estimated_cost_usd is None for row in rows) or not rows
            else round(sum(row.estimated_cost_usd for row in rows), 8)  # type: ignore[misc]
        ),
    )


def _cell(rows: Sequence[RunScore]) -> CellSummary:
    first = rows[0]
    return CellSummary(
        case_id=first.case_id,
        error_condition=first.error_condition.value,
        condition_id=first.condition_id,
        runs=len(rows),
        completed=sum(1 for row in rows if row.completed),
        protocol_failures=sum(1 for row in rows if row.protocol_failure),
        unanswered=sum(1 for row in rows if not row.produced_answer),
        accuracy=Rate.of(row.correct for row in rows),
        correction_stages=_stage_counts(rows),
    )


def _condition_error(rows: Sequence[RunScore]) -> ConditionErrorCount:
    first = rows[0]
    return ConditionErrorCount(
        condition_id=first.condition_id,
        error_condition=first.error_condition.value,
        runs=len(rows),
        completed=sum(1 for row in rows if row.completed),
        accuracy=Rate.of(row.correct for row in rows),
        error_survival_manager=Rate.of(row.error_survival_manager for row in rows),
        error_survival_compliance=Rate.of(row.error_survival_compliance for row in rows),
    )


def _qc(
    rows: Sequence[RunScore], plan, registry  # noqa: ANN001
) -> QCSummary:
    """§11's QC block. The plan-derived checks need the plan, and say so when absent."""
    return QCSummary(
        planned_runs=None if plan is None else len(plan.jobs),
        attempted_runs=len(rows),
        completed_runs=sum(1 for row in rows if row.completed),
        failed_runs=sum(1 for row in rows if not row.completed),
        protocol_failure_rate=Rate.of(row.protocol_failure for row in rows),
        structured_output_success_rate=Rate.of(
            not row.model_output_failure for row in rows
        ),
        verification_failure_count=sum(1 for row in rows if row.verification_failure),
        tool_failure_count=sum(1 for row in rows if row.tool_failure),
        missing_cells=None if plan is None or registry is None else plan.missing_cells(registry),
        duplicate_cells=None if plan is None else plan.duplicate_cells(),
        plan_fingerprint=None if plan is None else plan.fingerprint(),
    )


def summarise(
    scores: ExperimentScores,
    *,
    plan=None,  # noqa: ANN001
    registry=None,  # noqa: ANN001
) -> ExperimentSummary:
    """Count a scored experiment.

    Cells are emitted in sorted order and conditions in the order they first
    appear in the rows, so two summaries of the same rows are identical
    documents rather than merely equivalent ones.

    ``plan`` and ``registry`` are optional and used for one thing only: the cell
    completeness checks in :class:`QCSummary`. Passing them does not change a
    single scored number, because every number here comes from the rows -- which
    is what keeps the summary reconstructible from raw artifacts alone.
    """
    rows = list(scores.rows)

    cells: dict[tuple[str, str, str], list[RunScore]] = {}
    for row in rows:
        key = (row.case_id, row.error_condition.value, row.condition_id)
        cells.setdefault(key, []).append(row)

    conditions: dict[str, list[RunScore]] = {}
    for row in rows:
        conditions.setdefault(row.condition_id, []).append(row)

    condition_error: dict[tuple[str, str], list[RunScore]] = {}
    for row in rows:
        condition_error.setdefault(
            (row.condition_id, row.error_condition.value), []
        ).append(row)

    return ExperimentSummary(
        experiment_id=scores.experiment_id,
        score_schema_version=scores.score_schema_version,
        plan_fingerprints=scores.plan_fingerprints,
        runs=len(rows),
        completed=sum(1 for row in rows if row.completed),
        protocol_failures=sum(1 for row in rows if row.protocol_failure),
        unanswered=sum(1 for row in rows if not row.produced_answer),
        accuracy=Rate.of(row.correct for row in rows),
        error_survival_manager=Rate.of(row.error_survival_manager for row in rows),
        error_survival_compliance=Rate.of(row.error_survival_compliance for row in rows),
        correction_stages=_stage_counts(rows),
        qc=_qc(rows, plan, registry),
        by_condition=tuple(_pool(conditions[key], key) for key in sorted(conditions)),
        by_condition_error=tuple(
            _condition_error(condition_error[key]) for key in sorted(condition_error)
        ),
        by_cell=tuple(_cell(cells[key]) for key in sorted(cells)),
    )
