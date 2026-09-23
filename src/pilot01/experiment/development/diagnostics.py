"""Development diagnostics, and a verdict with the checks declared before the run.

This module reads the Gate-3 scorer's rows and reports what they say. It adds no
statistics: no p-values, no intervals, no regressions, no effect estimates.
Every number here is a count or a proportion with its denominator, and every
proportion that could be misread without one carries it.

**The checks are pre-declared, and that is the whole design.** The thresholds
below were written before any Gate-4 run existed, and they are constants in the
source rather than arguments, so they cannot be chosen after seeing the outcome.
A verdict rule written after the fact is not a verdict rule; it is a description
of the result wearing one's clothes.

**Three kinds of finding, kept apart.** §16 requires an implementation bug to be
separated from a scientific result, and the separation is structural here rather
than a matter of careful prose:

``protocol``
    The experiment did not run the way it was designed to -- a cell is missing, a
    node reached the source in a condition that forbids it, the parser collapsed.
    Nothing about the research question can be read until this is fixed, so a
    failed protocol check is a STOP.
``design``
    The manipulation itself did not hold -- a memo pair that differs in more than
    the registered claim, a run that adopted a claim it was never given. Also a
    STOP: these are properties of the artifacts, and a run over broken artifacts
    measures the breakage.
``readiness``
    The protocol ran and the artifacts held, but the setup is not yet good enough
    to carry into a confirmatory gate -- sentinels escalating at a rate that says
    the cases or the policy wording need work. A REVISE, not a STOP.

**A failure here is not a licence to change the prompts.** §16 is explicit, and
the verdict object says so in its own rationale: outcome differences are the
thing the experiment exists to measure. Nothing in this module writes a prompt,
and nothing in it should be read as suggesting one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ...schemas import ClauseStatus, Decision
from ..cases import CaseRegistry
from ..runspec import RunPlan
from ..scoring import ExperimentScores, RunScore
from ..summary import Rate
from .registry import DevelopmentCaseSet

__all__ = [
    "MAX_PROTOCOL_FAILURE_RATE",
    "MIN_STRUCTURED_OUTPUT_SUCCESS_RATE",
    "MAX_SENTINEL_FALSE_ESCALATION",
    "MAX_UNKNOWN_ADOPTED_CLAIMS",
    "MIN_VERIFICATION_BASIS_AGREEMENT",
    "SentinelMechanism",
    "DevelopmentCell",
    "SentinelConditionRate",
    "SentinelDiagnostics",
    "DiagnosticCheck",
    "DiagnosticVerdict",
    "DevelopmentDiagnostics",
    "build_diagnostics",
    "render_development_table",
]


MAX_PROTOCOL_FAILURE_RATE = 0.20
"""Above this, too many runs died for the accuracy column to mean anything.

One in five. A protocol failure is a run that never produced an answer -- a
transport error, a parser that could not be satisfied, a workflow that raised.
Those are not wrong answers and the scorer keeps them out of the accuracy
denominator, which is right for the per-run figure and dangerous for the
aggregate: at a high enough rate the surviving runs are a biased subset of the
plan, and the accuracy figure describes the survivors rather than the design.
"""

MIN_STRUCTURED_OUTPUT_SUCCESS_RATE = 0.90
"""Below this, the schema is fighting the model rather than measuring it.

A structured-output failure is a call whose response could not be parsed into
the node's schema even after repair. A few are expected; a tenth of them means
the schema or the prompt is the wrong shape for the model, which is an
implementation problem to fix before anything is concluded from the answers.
"""

MAX_SENTINEL_FALSE_ESCALATION = 0.50
"""Above this, the sentinels are not testing what they are there for.

A sentinel exists to give the policy's ACCEPT branch something to be wrong
about. If most sentinel runs escalate, the likely causes are the case's clause
set or the policy wording -- both design questions, neither of them a finding
about an agent, and both worth resolving before a confirmatory gate.
"""

MAX_UNKNOWN_ADOPTED_CLAIMS = 0
"""Runs may not adopt a claim nothing in their permitted input could produce.

Not a tolerance that could be relaxed: a non-zero count means a node's input
carried something outside its view, which is a containment failure rather than
a research observation. Zero is the only value that supports the claim that the
views are restricted.
"""

MIN_VERIFICATION_BASIS_AGREEMENT = 0.90
"""Below this, the verification vocabulary is not tracking what agents do.

A node's declared ``verification_basis`` is compared against the basis the
ledger derives from its tool calls. A disagreement is a real finding about that
run, and a handful are expected -- a model can misjudge whether a search counted
as reading. Disagreement on more than a tenth of runs means the prompt is asking
for a distinction the model cannot map onto its own behaviour, which is an
implementation problem to fix before the gate rather than a behaviour to
interpret.

Set at the structured-output threshold's value for the same reason it is set
there: this is the point at which the instrument, not the subject, is the
likeliest explanation.
"""


class SentinelMechanism(str, Enum):
    """How a sentinel run came to escalate despite nothing being present.

    Reported because the pooled rate cannot tell these apart, and they are not
    the same finding:

    ``ESCALATED_ON_UNKNOWN``
        The agent could not determine the target category and escalated on the
        unresolved value. Its *assessment* may be entirely honest; what failed
        is the mapping from "not determined" to an action.
    ``ESCALATED_DESPITE_ABSENT``
        The agent assessed the target category ``absent`` -- the correct
        assessment -- and escalated anyway. The decision contradicts the
        agent's own record, which is an internal-consistency fault rather than
        a judgement call.
    ``ESCALATED_ON_PRESENT``
        The agent assessed the target category ``present``. The fact is wrong,
        not just the action.
    ``UNAVAILABLE``
        The run produced no per-category assessment to classify.
    """

    ESCALATED_ON_UNKNOWN = "escalated_on_unknown"
    ESCALATED_DESPITE_ABSENT = "escalated_despite_absent"
    ESCALATED_ON_PRESENT = "escalated_on_present"
    UNAVAILABLE = "unavailable"


class DevelopmentCell(BaseModel):
    """One cell of §15's table: a governance condition crossed with an error arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_id: str
    error_condition: str
    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    completion_rate: Rate
    protocol_failures: int = Field(ge=0)

    final_action_accuracy: Rate
    fact_recovery_accuracy: Rate
    """§6's fact column, beside the action column rather than inside it.

    GATE4-DESIGN-01 asked for the two to be separable. They are not independent
    -- POLICY-01 maps a present target to ESCALATE, so recovering the fact
    implies the action -- but a reader comparing the two columns can see that
    for themselves, which is the point. A gap between them in either direction
    is a finding; the protocol check ``action_fact_consistency`` asserts the one
    direction that must be empty.
    """

    error_survival_manager: Rate
    error_survival_compliance: Rate
    corrected_with_evidence: Rate

    review_rate: Rate
    """How often REVIEW was the final decision in this cell.

    The answer ``v1`` had no way to give. A cell with a high review rate is not
    a cell of wrong answers -- it is a cell where the agents declined to decide,
    which is a different result and has to be visible as one.
    """

    mean_source_opens: float | None = None
    mean_model_calls: float | None = None


class SentinelConditionRate(BaseModel):
    """One governance condition's sentinel pool, with both denominators shown.

    Two counts, not one. ``runs`` is every sentinel run in the condition;
    ``judged`` is those that produced a decision. When they differ, any rate
    over ``judged`` describes the runs that finished rather than the runs that
    were planned, and the gap is exactly the bias that made the Gate-4 sentinel
    figure unreadable. Both are reported so the gap cannot be invisible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_id: str
    runs: int = Field(ge=0)
    judged: int = Field(ge=0)
    false_escalations: int = Field(ge=0)
    false_escalation_rate: Rate

    @property
    def complete(self) -> bool:
        """Whether every sentinel run in this condition produced a decision."""
        return self.judged == self.runs


class SentinelDiagnostics(BaseModel):
    """The negative sentinels, reported apart from the positive cases.

    Apart, because false escalation is a question only they can answer: a
    positive case *should* escalate, so counting it in a false-escalation rate
    would dilute the rate with cases that were never at risk of a false
    positive. The scorer already marks the field ``None`` for non-sentinels;
    this reports the resulting subset with its denominator.

    **Per condition, and by mechanism.** The Gate-4 review found the pooled
    figure was a rate over three governance conditions at once, over two
    different mechanisms, and -- in one condition -- over a denominator halved
    by runs that never finished. The pooled number is still reported, because it
    is what a reader will look for, but it is no longer the only number, and the
    check that reads it refuses to do so when a condition's pool is incomplete.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    false_escalations: int = Field(ge=0)
    false_escalation_rate: Rate
    by_condition: tuple[SentinelConditionRate, ...] = ()
    mechanisms: dict[str, int] = Field(default_factory=dict)
    """How many false escalations arose each way. See :class:`SentinelMechanism`."""

    @property
    def incomplete_conditions(self) -> tuple[str, ...]:
        """Conditions whose sentinel pool is missing a decision from some run."""
        return tuple(
            rate.condition_id for rate in self.by_condition if not rate.complete
        )


class DiagnosticCheck(BaseModel):
    """One pre-declared check, and what it found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    description: str
    kind: str
    """``protocol``, ``design`` or ``readiness``."""

    verdict_if_failed: str
    """``STOP`` or ``REVISE``."""

    passed: bool | None = None
    """``None`` when the check could not be evaluated, which is not a pass.

    A check that silently became ``True`` because its input was missing would be
    a check that stops working exactly when the experiment is broken enough to
    need it.
    """

    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.passed is False

    @property
    def unevaluated(self) -> bool:
        return self.passed is None


class DiagnosticVerdict(BaseModel):
    """§16's verdict, and the checks it was computed from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str
    """``GO``, ``REVISE`` or ``STOP``."""

    rationale: str
    checks: tuple[DiagnosticCheck, ...]

    implementation_findings: tuple[str, ...] = ()
    design_findings: tuple[str, ...] = ()
    scientific_findings: tuple[str, ...] = ()
    """Outcome differences, recorded and explicitly not acted on.

    §16 forbids rewriting prompts in response to outcome differences. This field
    is where such a difference goes: it is stated, it is attributed to the
    experiment rather than to a defect, and nothing follows from it
    automatically.
    """

    @property
    def stopped(self) -> bool:
        return self.verdict == "STOP"

    def failed_checks(self) -> tuple[DiagnosticCheck, ...]:
        return tuple(check for check in self.checks if check.failed)

    def unevaluated_checks(self) -> tuple[DiagnosticCheck, ...]:
        return tuple(check for check in self.checks if check.unevaluated)


class DevelopmentDiagnostics(BaseModel):
    """§14's development diagnostics: everything the scored rows support, and no more."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: int = 4
    status: str = "DEVELOPMENT"
    experiment_id: str
    note: str = (
        "Development diagnostics for the Gate-4 development batch. Not "
        "confirmatory results; no hypothesis tests were computed and none should "
        "be read into these counts."
    )

    planned_runs: int | None = None
    scored_runs: int = Field(ge=0)
    completed: int = Field(ge=0)
    protocol_failures: int = Field(ge=0)
    protocol_failure_rate: Rate
    structured_output_success_rate: Rate
    verification_failure_count: int = Field(ge=0)
    tool_failure_count: int = Field(ge=0)

    correction_stages: dict[str, int] = Field(default_factory=dict)
    corrected_with_evidence: Rate

    cells: tuple[DevelopmentCell, ...] = ()
    sentinels: SentinelDiagnostics
    verdict: DiagnosticVerdict

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    """``None`` unless a pricing table was explicitly configured.

    Absent rather than zero: no pricing configured means no cost was estimated,
    which is a different statement from an estimated cost of nothing.
    """

    def cell(self, condition_id: str, error_condition: str) -> DevelopmentCell:
        for cell in self.cells:
            if cell.condition_id == condition_id and cell.error_condition == error_condition:
                return cell
        raise KeyError(f"no cell ({condition_id}, {error_condition}) in these diagnostics")

    def to_payload(self) -> dict:
        return json.loads(self.model_dump_json())

    def write_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2) + "\n", encoding="utf-8"
        )
        return target


def _pool(rows: Sequence[RunScore], condition_id: str, error_condition: str) -> DevelopmentCell:
    pool = [
        row
        for row in rows
        if row.condition_id == condition_id and row.error_condition.value == error_condition
    ]
    return DevelopmentCell(
        condition_id=condition_id,
        error_condition=error_condition,
        runs=len(pool),
        completed=sum(1 for row in pool if row.completed),
        completion_rate=Rate.of([row.completed for row in pool]),
        protocol_failures=sum(1 for row in pool if row.protocol_failure),
        final_action_accuracy=Rate.of([row.final_action_correct for row in pool]),
        fact_recovery_accuracy=Rate.of(
            [_fact_recovery(row) for row in pool]
        ),
        error_survival_manager=Rate.of([row.error_survival_manager for row in pool]),
        error_survival_compliance=Rate.of([row.error_survival_compliance for row in pool]),
        corrected_with_evidence=Rate.of(
            [_corrected_with_evidence(row) for row in pool]
        ),
        review_rate=Rate.of(
            [
                None if row.final_decision is None else row.final_decision is Decision.REVIEW
                for row in pool
            ]
        ),
        mean_source_opens=_mean(
            [row.manager_opens + row.compliance_opens for row in pool]
        ),
        mean_model_calls=_mean([row.model_calls for row in pool]),
    )


def _corrected_with_evidence(row: RunScore) -> bool | None:
    """Whether either node recovered the lost fact *and* showed its evidence.

    ``True`` if either node did; ``False`` if both were assessed and neither
    did; ``None`` if neither node's answer was available to judge. The
    distinction matters because a run that died is not a run that failed to
    correct, and the scorer already drew that line per node.
    """
    values = [
        value
        for value in (row.manager_corrected_with_evidence, row.compliance_corrected_with_evidence)
        if value is not None
    ]
    if not values:
        return None
    return any(values)


def _fact_recovery(row: RunScore) -> bool | None:
    """Whether either node recovered the fact the omission destroyed.

    The same either-node shape as :func:`_corrected_with_evidence`, so the two
    columns are comparable: both ask whether *some* node got the fact back, and
    the difference between them is the evidence requirement, not the population.
    """
    values = [
        value
        for value in (row.manager_fact_recovery_correct, row.compliance_fact_recovery_correct)
        if value is not None
    ]
    if not values:
        return None
    return any(values)


def _sentinel_mechanism(row: RunScore) -> SentinelMechanism:
    """Classify how one false-escalating sentinel came to escalate.

    Read off the *Compliance* node's assessment of the case's own target
    category, because that is the node whose decision is the run's answer. The
    categories are the ones the policy names, so a sentinel's gold is ``absent``
    for all of them, and any non-``absent`` assessment is a departure from it.
    """
    statuses = row.compliance_target_clause_status
    if statuses is None:
        return SentinelMechanism.UNAVAILABLE
    assessed = statuses.get(row.target_category)
    if assessed is ClauseStatus.ABSENT:
        return SentinelMechanism.ESCALATED_DESPITE_ABSENT
    if assessed is ClauseStatus.UNKNOWN:
        return SentinelMechanism.ESCALATED_ON_UNKNOWN
    if assessed is ClauseStatus.PRESENT:
        return SentinelMechanism.ESCALATED_ON_PRESENT
    return SentinelMechanism.UNAVAILABLE


def _mean(values: Iterable[int | float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 6)


def _sentinels(rows: Sequence[RunScore], conditions: Sequence[str]) -> SentinelDiagnostics:
    """The sentinel pool, split by condition and classified by mechanism.

    ``conditions`` is every condition present in the batch, not only the ones
    with sentinels: a condition with no sentinel runs reports ``runs=0``, which
    is a statement about the batch. Silently omitting it would make "this
    condition was never run" and "this condition was run and behaved" look the
    same in the table.
    """
    pool = [row for row in rows if row.is_negative_sentinel]
    by_condition: list[SentinelConditionRate] = []
    for condition_id in conditions:
        in_condition = [row for row in pool if row.condition_id == condition_id]
        judged = [row for row in in_condition if row.false_escalation is not None]
        by_condition.append(
            SentinelConditionRate(
                condition_id=condition_id,
                runs=len(in_condition),
                judged=len(judged),
                false_escalations=sum(1 for row in judged if row.false_escalation),
                false_escalation_rate=Rate.of(
                    [row.false_escalation for row in in_condition]
                ),
            )
        )

    mechanisms: dict[str, int] = {}
    for row in pool:
        if not row.false_escalation:
            continue
        key = _sentinel_mechanism(row).value
        mechanisms[key] = mechanisms.get(key, 0) + 1

    return SentinelDiagnostics(
        runs=len(pool),
        completed=sum(1 for row in pool if row.completed),
        false_escalations=sum(1 for row in pool if row.false_escalation),
        false_escalation_rate=Rate.of([row.false_escalation for row in pool]),
        by_condition=tuple(by_condition),
        mechanisms=dict(sorted(mechanisms.items())),
    )


# ---------------------------------------------------------------------------
# The pre-declared checks
# ---------------------------------------------------------------------------


def _check_protocol_failure_rate(rate: Rate) -> DiagnosticCheck:
    passed = rate.value is not None and rate.value <= MAX_PROTOCOL_FAILURE_RATE
    return DiagnosticCheck(
        check_id="protocol_failure_rate",
        description=(
            f"protocol failures at or below {MAX_PROTOCOL_FAILURE_RATE:.0%} of scored runs"
        ),
        kind="protocol",
        verdict_if_failed="STOP",
        passed=passed,
        detail=rate.describe(),
    )


def _check_structured_output(rate: Rate) -> DiagnosticCheck:
    passed = rate.value is not None and rate.value >= MIN_STRUCTURED_OUTPUT_SUCCESS_RATE
    return DiagnosticCheck(
        check_id="structured_output_success_rate",
        description=(
            f"structured output parsed on at least "
            f"{MIN_STRUCTURED_OUTPUT_SUCCESS_RATE:.0%} of model calls"
        ),
        kind="protocol",
        verdict_if_failed="STOP",
        passed=passed,
        detail=rate.describe(),
    )


def _check_plan_complete(
    rows: Sequence[RunScore], plan: RunPlan | None
) -> DiagnosticCheck:
    """Every planned cell ran exactly once, and no unplanned cell ran.

    Missing and duplicated cells are one check rather than two because they are
    one failure: the batch did not execute the design. A duplicated cell
    silently overweights its case; a missing one silently drops it, and either
    makes the aggregate describe something other than the plan.
    """
    if plan is None:
        return DiagnosticCheck(
            check_id="plan_complete",
            description="every planned cell ran exactly once",
            kind="protocol",
            verdict_if_failed="STOP",
            passed=None,
            detail="no plan was available to check the scored rows against",
        )
    planned = {job.cell for job in plan.jobs}
    seen: dict[tuple, int] = {}
    for row in rows:
        seen[row.cell] = seen.get(row.cell, 0) + 1
    missing = sorted(planned - set(seen))
    duplicated = sorted(cell for cell, count in seen.items() if count > 1)
    passed = not missing and not duplicated
    return DiagnosticCheck(
        check_id="plan_complete",
        description="every planned cell ran exactly once",
        kind="protocol",
        verdict_if_failed="STOP",
        passed=passed,
        detail=(
            f"{len(planned)} planned cell(s), {len(seen)} observed"
            + (f"; missing {len(missing)}" if missing else "")
            + (f"; duplicated {len(duplicated)}" if duplicated else "")
        ),
    )


def _check_source_isolation(rows: Sequence[RunScore]) -> DiagnosticCheck:
    """No run without source access touched the source.

    This is the governance manipulation itself. If an A0 run can reach a
    contract, the three conditions are not three conditions and every comparison
    between them is void -- so it is a STOP and not a caveat.
    """
    breaches = [
        row.run_id
        for row in rows
        if not row.source_access
        and (row.manager_searches + row.manager_opens + row.compliance_searches + row.compliance_opens) > 0
    ]
    return DiagnosticCheck(
        check_id="source_isolation",
        description="no run without source access performed a source operation",
        kind="protocol",
        verdict_if_failed="STOP",
        passed=not breaches,
        detail=(
            f"{len(breaches)} run(s) reached the source under a no-source condition"
            if breaches
            else "no no-source run performed a source operation"
        ),
    )


def _check_verification_enforced(rows: Sequence[RunScore]) -> DiagnosticCheck:
    """In the verification condition, verification actually ran.

    A1V1's claim is that verification is *required*, and a requirement the
    runtime never enforces is a prompt instruction rather than a treatment. If
    runs in A1V1 show no verification check, the arm is not the arm it is named.
    """
    required = [row for row in rows if row.verification_required and row.completed]
    unchecked = [row.run_id for row in required if not row.compliance_verification_checked]
    return DiagnosticCheck(
        check_id="verification_enforced",
        description="every completed run in a verification condition had verification checked",
        kind="protocol",
        verdict_if_failed="STOP",
        passed=not unchecked,
        detail=(
            f"{len(unchecked)} of {len(required)} completed verification-condition "
            "run(s) recorded no verification check"
            if unchecked
            else f"all {len(required)} completed verification-condition run(s) checked"
        ),
    )


def _check_unknown_adopted(rows: Sequence[RunScore]) -> DiagnosticCheck:
    total = sum(len(row.unknown_adopted_claims) for row in rows)
    return DiagnosticCheck(
        check_id="no_unknown_adopted_claims",
        description=(
            f"no run adopted a claim its permitted input could not have produced "
            f"(limit {MAX_UNKNOWN_ADOPTED_CLAIMS})"
        ),
        kind="design",
        verdict_if_failed="STOP",
        passed=total <= MAX_UNKNOWN_ADOPTED_CLAIMS,
        detail=f"{total} adopted claim(s) outside the permitted views",
    )


def _check_memo_pairs(case_set: DevelopmentCaseSet | None) -> DiagnosticCheck:
    if case_set is None:
        return DiagnosticCheck(
            check_id="memo_pairs_single_change",
            description="every memo pair differs only in the registered claim",
            kind="design",
            verdict_if_failed="STOP",
            passed=None,
            detail="no development case set was supplied to check",
        )
    diffs = [
        record.memo_pair_diff
        for record in case_set.cases
        if record.memo_pair_diff is not None
    ]
    bad = [
        diff.case_id
        for diff in diffs
        if not diff.is_single_position_change
        or diff.e0_claim_count != diff.e1_claim_count
    ]
    return DiagnosticCheck(
        check_id="memo_pairs_single_change",
        description="every memo pair differs only in the registered claim",
        kind="design",
        verdict_if_failed="STOP",
        passed=not bad,
        detail=(
            f"{len(bad)} pair(s) changed more than the registered claim: {bad[:3]}"
            if bad
            else f"all {len(diffs)} pair(s) differ at exactly one position"
        ),
    )


def _check_sentinel_false_escalation(sentinels: SentinelDiagnostics) -> DiagnosticCheck:
    """The pooled rate, but only when the pool is complete enough to carry it.

    The Gate-4 review found this check reading a rate whose denominator had been
    halved by runs that never finished, in one condition, and pooling that with
    two conditions whose pools were complete. The number was arithmetically
    right and described nothing.

    So an incomplete pool is not a pass and not a failure: the check reports
    ``passed=None`` and names the conditions. That is the honest answer -- the
    rate over the runs that happened to finish is not the rate the design
    planned, and reporting it as either would be worse than reporting nothing.
    """
    detail = sentinels.false_escalation_rate.describe()
    per_condition = "; ".join(
        f"{rate.condition_id} {rate.false_escalation_rate.describe()}"
        f"{'' if rate.complete else f' (incomplete: {rate.judged}/{rate.runs} judged)'}"
        for rate in sentinels.by_condition
        if rate.runs
    )
    if per_condition:
        detail = f"{detail} -- by condition: {per_condition}"
    if sentinels.mechanisms:
        detail += " -- mechanisms: " + ", ".join(
            f"{name}={count}" for name, count in sentinels.mechanisms.items()
        )

    incomplete = sentinels.incomplete_conditions
    if incomplete:
        return DiagnosticCheck(
            check_id="sentinel_false_escalation",
            description=(
                f"negative sentinels escalate at or below "
                f"{MAX_SENTINEL_FALSE_ESCALATION:.0%} (a design question, not a finding)"
            ),
            kind="readiness",
            verdict_if_failed="REVISE",
            passed=None,
            detail=(
                f"not evaluated: condition(s) {list(incomplete)} have sentinel runs "
                f"that produced no decision, so the pooled rate would describe the "
                f"runs that finished rather than the runs that were planned. {detail}"
            ),
        )

    rate = sentinels.false_escalation_rate
    passed = rate.value is None or rate.value <= MAX_SENTINEL_FALSE_ESCALATION
    return DiagnosticCheck(
        check_id="sentinel_false_escalation",
        description=(
            f"negative sentinels escalate at or below "
            f"{MAX_SENTINEL_FALSE_ESCALATION:.0%} (a design question, not a finding)"
        ),
        kind="readiness",
        verdict_if_failed="REVISE",
        passed=passed,
        detail=detail,
    )


def _check_action_fact_consistency(rows: Sequence[RunScore]) -> DiagnosticCheck:
    """Recovering the fact must imply the action, and here that is checked.

    POLICY-01 is a total function from a target category's status to a decision,
    and ``present`` maps to ESCALATE. So a run whose Compliance node recovered
    the omitted fact cannot have reached a final decision other than ESCALATE,
    and a row saying otherwise means the scorer, the policy object and the
    prompt disagree about what the policy says.

    That makes this a protocol check, not a research one: the cell is empty by
    construction, and a non-empty cell is an instrument fault. It is the
    invariant GATE4-DESIGN-01 was about, stated so that it fails loudly instead
    of being assumed.
    """
    violations = [
        row.run_id
        for row in rows
        if row.compliance_fact_recovery_correct is True and row.final_action_correct is False
    ]
    return DiagnosticCheck(
        check_id="action_fact_consistency",
        description=(
            "no run recovered the omitted fact and still reached the wrong final action"
        ),
        kind="protocol",
        verdict_if_failed="STOP",
        passed=not violations,
        detail=(
            f"{len(violations)} run(s) recovered the fact with a wrong final action: "
            f"{violations[:3]}"
            if violations
            else "no run recovered the fact with a wrong final action"
        ),
    )


def _check_verification_basis_agreement(rows: Sequence[RunScore]) -> DiagnosticCheck:
    """What nodes declare about their verification, against what they did.

    The observable ``v1`` could not take: the declared basis and the derived one
    were the same field, so the two could not be compared. A low agreement rate
    is not a finding about agents -- it means the prompt's vocabulary does not
    map onto the behaviour the ledger sees, which is worth fixing before a
    confirmatory gate.
    """
    agreements = [
        row.compliance_verification_basis_agrees
        for row in rows
        if row.completed and row.compliance_verification_basis_derived is not None
    ]
    rate = Rate.of(agreements)
    if not rate.measurable:
        return DiagnosticCheck(
            check_id="verification_basis_agreement",
            description=(
                f"declared verification basis agrees with the derived one on at "
                f"least {MIN_VERIFICATION_BASIS_AGREEMENT:.0%} of completed runs"
            ),
            kind="readiness",
            verdict_if_failed="REVISE",
            passed=None,
            detail="no completed run recorded a derived verification basis",
        )
    passed = rate.value >= MIN_VERIFICATION_BASIS_AGREEMENT
    return DiagnosticCheck(
        check_id="verification_basis_agreement",
        description=(
            f"declared verification basis agrees with the derived one on at "
            f"least {MIN_VERIFICATION_BASIS_AGREEMENT:.0%} of completed runs"
        ),
        kind="readiness",
        verdict_if_failed="REVISE",
        passed=passed,
        detail=f"Compliance {rate.describe()}",
    )


def _check_a1v1_source_use(rows: Sequence[RunScore]) -> DiagnosticCheck:
    """Runs with tools and a verification duty actually went to the source.

    Readiness rather than protocol: an agent could in principle discharge its
    duty without opening anything, so this is not proof of a defect. But if
    almost no A1V1 run opens a source, the arm is measuring a duty nobody
    exercised, and that is worth resolving before a confirmatory gate rather
    than after.
    """
    pool = [
        row
        for row in rows
        if row.source_access and row.verification_required and row.completed
    ]
    if not pool:
        return DiagnosticCheck(
            check_id="a1v1_source_use",
            description="runs with tools and a verification duty open the source",
            kind="readiness",
            verdict_if_failed="REVISE",
            passed=None,
            detail="no completed run in a source-access verification condition",
        )
    opened = sum(
        1 for row in pool if (row.manager_opens + row.compliance_opens) > 0
    )
    passed = opened / len(pool) >= 0.5
    return DiagnosticCheck(
        check_id="a1v1_source_use",
        description="at least half of the tool-and-verify runs open a source passage",
        kind="readiness",
        verdict_if_failed="REVISE",
        passed=passed,
        detail=f"{opened}/{len(pool)} run(s) opened at least one passage",
    )


def _verdict(checks: Sequence[DiagnosticCheck]) -> DiagnosticVerdict:
    """Fold the checks into GO / REVISE / STOP.

    Precedence is deliberate and strict: any failed STOP check stops, whatever
    else is true. A batch that breached source isolation does not become
    reviewable because its sentinels behaved. ``REVISE`` is the verdict only
    when nothing stopped and something at the readiness level failed.
    """
    failed = [check for check in checks if check.failed]
    stopped = [check for check in failed if check.verdict_if_failed == "STOP"]
    revise = [check for check in failed if check.verdict_if_failed == "REVISE"]
    unevaluated = [check for check in checks if check.unevaluated]

    if stopped:
        verdict = "STOP"
        rationale = (
            f"{len(stopped)} pre-declared check(s) failed at the protocol or design "
            "level: "
            + "; ".join(f"{check.check_id} ({check.detail})" for check in stopped)
            + ". The batch did not run the design, so its outcome numbers are not "
            "readable as findings about agents. Fix the implementation or the "
            "manipulation and re-run; do not adjust the prompts in response to "
            "outcome differences, and do not carry these numbers forward."
        )
    elif revise:
        verdict = "REVISE"
        rationale = (
            f"{len(revise)} readiness check(s) failed: "
            + "; ".join(f"{check.check_id} ({check.detail})" for check in revise)
            + ". The protocol ran and the artifacts held, so these are questions "
            "about the cases or the policy rather than about an agent's behaviour. "
            "Resolve them before the confirmatory gate."
        )
    else:
        verdict = "GO"
        rationale = (
            "every pre-declared check passed. The development batch ran the design "
            "it declared. This says nothing about the research question -- it says "
            "the setup is sound enough to carry forward."
        )
    if unevaluated:
        rationale += (
            f" {len(unevaluated)} check(s) could not be evaluated "
            f"({', '.join(check.check_id for check in unevaluated)}) and are not "
            "counted as passes."
        )

    return DiagnosticVerdict(
        verdict=verdict,
        rationale=rationale,
        checks=tuple(checks),
        implementation_findings=tuple(
            f"{check.check_id}: {check.detail}"
            for check in checks
            if check.failed and check.kind == "protocol"
        ),
        design_findings=tuple(
            f"{check.check_id}: {check.detail}"
            for check in checks
            if check.failed and check.kind == "design"
        ),
        scientific_findings=(),
    )


def build_diagnostics(
    scores: ExperimentScores,
    *,
    registry: CaseRegistry,
    plan: RunPlan | None = None,
    case_set: DevelopmentCaseSet | None = None,
    pricing_configured: bool = False,
) -> DevelopmentDiagnostics:
    """Compute §14's diagnostics, §15's table and §16's verdict.

    ``pricing_configured`` is passed in rather than inferred: a run's
    ``estimated_cost_usd`` is ``None`` both when no pricing table was supplied
    and when a supplied table lacked the model, and only the caller knows which
    of those happened. Cost is reported only when a table was explicitly
    configured.
    """
    rows = list(scores.rows)
    conditions = sorted({row.condition_id for row in rows})

    cells: list[DevelopmentCell] = []
    for condition_id in conditions:
        for error_condition in ("E0", "E1"):
            cell = _pool(rows, condition_id, error_condition)
            if cell.runs:
                cells.append(cell)

    completed = sum(1 for row in rows if row.completed)
    protocol_failures = sum(1 for row in rows if row.protocol_failure)
    model_calls = sum(row.model_calls for row in rows)
    format_repairs = sum(row.format_repairs for row in rows)
    structured = Rate(
        numerator=model_calls - format_repairs,
        denominator=model_calls,
    )
    tokens = _sum_or_none([row.total_tokens for row in rows])
    cost = _sum_or_none([row.estimated_cost_usd for row in rows]) if pricing_configured else None

    correction_stages: dict[str, int] = {}
    for row in rows:
        key = row.correction_stage.value
        correction_stages[key] = correction_stages.get(key, 0) + 1

    sentinels = _sentinels(rows, conditions)

    checks = [
        _check_protocol_failure_rate(
            Rate(numerator=protocol_failures, denominator=len(rows))
        ),
        _check_structured_output(structured),
        _check_plan_complete(rows, plan),
        _check_source_isolation(rows),
        _check_verification_enforced(rows),
        _check_unknown_adopted(rows),
        _check_memo_pairs(case_set),
        _check_action_fact_consistency(rows),
        _check_sentinel_false_escalation(sentinels),
        _check_a1v1_source_use(rows),
        _check_verification_basis_agreement(rows),
    ]

    return DevelopmentDiagnostics(
        experiment_id=scores.experiment_id,
        planned_runs=len(plan.jobs) if plan is not None else None,
        scored_runs=len(rows),
        completed=completed,
        protocol_failures=protocol_failures,
        protocol_failure_rate=Rate(numerator=protocol_failures, denominator=len(rows)),
        structured_output_success_rate=structured,
        verification_failure_count=sum(
            1 for row in rows if row.verification_failures
        ),
        tool_failure_count=sum(1 for row in rows if row.tool_failure),
        correction_stages=dict(sorted(correction_stages.items())),
        corrected_with_evidence=Rate.of(
            [_corrected_with_evidence(row) for row in rows]
        ),
        cells=tuple(cells),
        sentinels=sentinels,
        verdict=_verdict(checks),
        model_calls=model_calls,
        tool_calls=sum(row.tool_calls for row in rows),
        total_tokens=tokens,
        estimated_cost_usd=cost,
    )


def _sum_or_none(values: Iterable[int | float | None]) -> int | float | None:
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]


def render_development_table(diagnostics: DevelopmentDiagnostics) -> str:
    """§15's one concise table, plus the sentinel line beneath it."""
    header = (
        f"{'condition':<10} {'arm':<4} {'N':>4} {'compl':>7} {'acc':>10} "
        f"{'fact':>10} {'review':>10} {'mgr surv':>10} {'cmp surv':>10} "
        f"{'corr+ev':>10} {'opens':>7} {'calls':>7} {'proto':>6}"
    )
    lines = [
        f"Gate 4 development diagnostics -- {diagnostics.experiment_id}",
        "DEVELOPMENT ONLY: engineering diagnostics, not study findings.",
        "",
        header,
        "-" * len(header),
    ]
    for cell in diagnostics.cells:
        lines.append(
            f"{cell.condition_id:<10} {cell.error_condition:<4} {cell.runs:>4} "
            f"{cell.completion_rate.describe():>7} "
            f"{cell.final_action_accuracy.describe():>10} "
            f"{cell.fact_recovery_accuracy.describe():>10} "
            f"{cell.review_rate.describe():>10} "
            f"{cell.error_survival_manager.describe():>10} "
            f"{cell.error_survival_compliance.describe():>10} "
            f"{cell.corrected_with_evidence.describe():>10} "
            f"{_fmt(cell.mean_source_opens):>7} {_fmt(cell.mean_model_calls):>7} "
            f"{cell.protocol_failures:>6}"
        )
    lines += [
        "",
        "negative sentinels (false escalation -- reported apart from the table above)",
        f"  runs {diagnostics.sentinels.runs}, completed {diagnostics.sentinels.completed}, "
        f"false escalations {diagnostics.sentinels.false_escalations} "
        f"({diagnostics.sentinels.false_escalation_rate.describe()})",
    ]
    for rate in diagnostics.sentinels.by_condition:
        if not rate.runs:
            lines.append(f"  {rate.condition_id}: no sentinel runs in this condition")
            continue
        lines.append(
            f"  {rate.condition_id}: {rate.false_escalations}/{rate.judged} judged "
            f"of {rate.runs} run(s)"
            + ("" if rate.complete else "  <- INCOMPLETE DENOMINATOR")
        )
    if diagnostics.sentinels.mechanisms:
        lines.append(
            "  mechanisms: "
            + ", ".join(
                f"{name}={count}"
                for name, count in diagnostics.sentinels.mechanisms.items()
            )
        )
    lines += [
        "",
        f"planned runs: {diagnostics.planned_runs}   scored: {diagnostics.scored_runs}   "
        f"completed: {diagnostics.completed}   protocol failures: "
        f"{diagnostics.protocol_failures} "
        f"({diagnostics.protocol_failure_rate.describe()})",
        f"structured-output success: {diagnostics.structured_output_success_rate.describe()}   "
        f"verification failures: {diagnostics.verification_failure_count}   "
        f"tool failures: {diagnostics.tool_failure_count}",
        f"correction stages: "
        + ", ".join(f"{k}={v}" for k, v in diagnostics.correction_stages.items()),
        f"model calls: {diagnostics.model_calls}   tool calls: {diagnostics.tool_calls}   "
        f"total tokens: {diagnostics.total_tokens}",
        "estimated cost: "
        + (
            f"${diagnostics.estimated_cost_usd:.6f}"
            if diagnostics.estimated_cost_usd is not None
            else "not reported (no pricing table was explicitly configured)"
        ),
        "",
        f"VERDICT: {diagnostics.verdict.verdict}",
        f"  {diagnostics.verdict.rationale}",
    ]
    for check in diagnostics.verdict.checks:
        mark = "pass" if check.passed else ("FAIL" if check.failed else "n/a ")
        lines.append(
            f"  [{mark}] {check.check_id} ({check.kind}) -- {check.detail}"
        )
    if diagnostics.verdict.implementation_findings:
        lines.append("")
        lines.append("implementation findings (fix before re-running):")
        lines += [f"  - {item}" for item in diagnostics.verdict.implementation_findings]
    if diagnostics.verdict.design_findings:
        lines.append("")
        lines.append("design findings (about the artifacts, not the agents):")
        lines += [f"  - {item}" for item in diagnostics.verdict.design_findings]
    lines += [
        "",
        "No inferential statistics were computed: no p-values, no intervals, no",
        "regressions, no hypothesis tests. These are development diagnostics.",
    ]
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
