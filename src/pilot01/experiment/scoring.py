"""Deriving experimental outcomes from raw artifacts.

This module reads a finished run and says what happened. It is the only place
in the repository that holds both the run's outputs and the case's gold labels
at the same time, and it is deliberately downstream of everything: nothing under
:mod:`pilot01.workflow`, :mod:`pilot01.source` or :mod:`pilot01.model` imports
it, and a test asserts that. Scoring cannot affect the workflow because the
workflow has already finished, and the imports prove it rather than promising
it.

**Rule-based, and no model.** Every metric here is a deterministic function of
the artifacts. There is no LLM-as-judge, no fuzzy match, no heuristic threshold.
A metric that needed judgement would be a different kind of measurement, and
mixing one into the primary outcomes would make the pilot's numbers
irreproducible in a way no amount of documentation repairs.

**Incorrect is not the same as unavailable.** A run that answered wrongly and a
run that never answered are different findings, and the row keeps them apart:
the answer metrics are ``bool | None``, where ``None`` means "there is nothing
to score" and every ``None`` is explained in :attr:`RunScore.notes`. Collapsing
the two would let a protocol failure enter the error rate as a wrong answer,
which is exactly the confusion §7 of the Gate 3 specification forbids.

**Paragraph-level evidence, not string identity.** An evidence citation counts
as recovering the clause when the paragraph the node *opened* overlaps the gold
span, by character range. Requiring the cited text to equal the gold text would
fail every run that opened the right paragraph and quoted a different sentence
from it, which is the designed retrieval unit, not an error.

**No invented numbers.** Token and latency totals are reported only when every
call in the run supplied that field; a partial total is ``None`` with a note,
never a sum over the calls that happened to report one. No price is built in:
:class:`PricingTable` must be supplied by the operator, and without one
``estimated_cost_usd`` is ``None``.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import AgentOutput, ClauseStatus, Decision, ErrorCondition, VerificationBasis
from ..source.document import ContractDocument, is_paragraph_id
from ..source.tools import OPEN_SOURCE_SPAN, SEARCH_CONTRACT, replay_ledger
from ..workflow.state import RunStatus
from .artifacts import RawRun
from .cases import CaseSpec, CaseRegistry

__all__ = [
    "SCORE_SCHEMA_VERSION",
    "PricingTable",
    "CorrectionStage",
    "EvidenceFailure",
    "EvidenceAssessment",
    "RunScore",
    "ExperimentScores",
    "score_run",
    "score_experiment",
    "spans_overlap",
    "write_scores_csv",
]


SCORE_SCHEMA_VERSION = "1"
"""Version of the scored-row schema. Bumped when a column's meaning changes."""


class ScoringError(RuntimeError):
    """A run cannot be scored: its artifacts and its case disagree."""


# --------------------------------------------------------------------------
# Pricing: opt-in, versioned, and never guessed
# --------------------------------------------------------------------------


class PricingTable(BaseModel):
    """Per-model prices, supplied by the operator and versioned.

    Nothing ships in this repository. A price is a fact about a moment in time
    and a vendor's current rate card, so a built-in table would silently become
    a stale number presented as a measurement. Without a table the cost column
    is ``None`` -- which is the honest answer, and the one that makes an
    operator notice they have not supplied prices rather than trusting a figure
    nobody chose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pricing_version: str
    currency: str = "USD"
    input_per_million: dict[str, float] = Field(default_factory=dict)
    output_per_million: dict[str, float] = Field(default_factory=dict)

    def estimate(
        self, *, model_id: str, prompt_tokens: int | None, completion_tokens: int | None
    ) -> float | None:
        """The cost of one call, or ``None`` if it cannot be computed exactly.

        Requires both the price *and* the token count. An estimate from a
        guessed token count would be a guess wearing a decimal point.
        """
        if prompt_tokens is None or completion_tokens is None:
            return None
        input_price = self.input_per_million.get(model_id)
        output_price = self.output_per_million.get(model_id)
        if input_price is None or output_price is None:
            return None
        return round(
            (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000,
            8,
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "PricingTable":
        source = Path(path)
        if not source.is_file():
            raise ScoringError(f"no pricing table at {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScoringError(f"{source} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ScoringError(f"{source} must contain a JSON object")
        return cls.model_validate(payload)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class CorrectionStage(str, Enum):
    """Where, if anywhere, the pipeline recovered the truth the memo lost.

    The interesting comparison is :attr:`MANAGER` against :attr:`COMPLIANCE`:
    the first says the downstream agent was handed a correct assessment, the
    second says it was handed a wrong one and fixed it. Those are different
    organizational findings, and a metric that only reported "the final answer
    was right" would not distinguish them.

    The three non-stages are not stages, and are kept apart for that reason:
    :attr:`ALREADY_CORRECT` means the arm carried no error to correct,
    :attr:`NOT_APPLICABLE` means the omission removed a claim that did not
    assert presence, so "recovering" it is not the same act, and
    :attr:`UNAVAILABLE` means the run produced no output to judge.
    """

    MANAGER = "manager"
    COMPLIANCE = "compliance"
    NEVER = "never"
    ALREADY_CORRECT = "already_correct"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class EvidenceFailure(str, Enum):
    """Why an evidence assessment could not be made, or came out against the run.

    Ordered as they are checked. The first two are not findings about the agent
    -- they say the question cannot be asked -- and the rest are.
    """

    OUTPUT_UNAVAILABLE = "output_unavailable"
    """The node produced no output, so it cited nothing."""

    NO_GOLD_SPAN = "no_gold_span"
    """The case declares no gold evidence span, so there is nothing to overlap."""

    NO_EVIDENCE = "no_evidence"
    """The node cited no evidence ids at all."""

    NOT_SELF_OPENED = "not_self_opened"
    """The node cited evidence but opened none of it; it cited upstream."""

    FOREIGN_EVIDENCE = "foreign_evidence"
    """A cited id is shaped like a paragraph id but is not a paragraph of this contract."""

    NO_GOLD_OVERLAP = "no_gold_overlap"
    """The node opened paragraphs, but none of them overlaps the gold evidence span."""


# --------------------------------------------------------------------------
# The per-node evidence assessment
# --------------------------------------------------------------------------


def spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Whether two half-open ``[start, end)`` character spans intersect.

    Half-open on purpose: a paragraph ending exactly where the next begins does
    not overlap it, which is what "these are two different paragraphs" means.
    """
    return first[0] < second[1] and second[0] < first[1]


class EvidenceAssessment(BaseModel):
    """What one node cited, what it actually opened, and whether that was the clause.

    ``self_opened`` is replayed from the tool-call log rather than read off the
    node's own claim: the log is the record, and a node that says it opened a
    passage is making a claim the log can check. ``cited`` is what the output
    said; the difference between the two sets is the whole point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    available: bool
    """Whether the node produced an output at all."""

    cited: tuple[str, ...] = ()
    self_opened: tuple[str, ...] = ()
    """Cited ids this node opened itself. Opened-but-uncited ids are not counted."""

    foreign: tuple[str, ...] = ()
    """Cited ids shaped like paragraph ids that are not paragraphs of this contract."""

    opened_spans: tuple[tuple[int, int], ...] = ()
    gold_spans: tuple[tuple[int, int], ...] = ()
    overlapping_ids: tuple[str, ...] = ()
    """Opened ids whose span overlaps a gold span."""

    overlaps_gold: bool | None = None
    """``None`` when the question cannot be asked; see :attr:`failure`."""

    failure: EvidenceFailure | None = None

    @property
    def opened_count(self) -> int:
        return len(self.self_opened)

    @property
    def cited_count(self) -> int:
        return len(self.cited)


def _reported_ids(output: AgentOutput) -> tuple[str, ...]:
    """Every id a node reported as evidence, in the order it reported them.

    Both provenance lists, because both are claims the node is making about what
    it relied on. Keeping them in one sequence here is what lets
    :func:`_assess_evidence` go on asking its one question -- of the ids this
    node reported, which did it actually open? -- rather than growing a second
    code path per provenance class. The class distinction is made where it
    belongs, in the ledger, and the anti-fabrication check in
    :mod:`pilot01.source.verification` reads the two lists separately.
    """
    provenance = output.evidence_provenance
    return tuple(provenance.opened_paragraph_ids) + tuple(provenance.inherited_source_ids)


def _assess_evidence(
    *,
    node: str,
    cited: Sequence[str] | None,
    opened_ids: Sequence[str],
    document: ContractDocument,
    gold_spans: Sequence[tuple[int, int]],
) -> EvidenceAssessment:
    """Assess one node's evidence. See :class:`EvidenceAssessment`."""
    if cited is None:
        return EvidenceAssessment(
            node=node, available=False, failure=EvidenceFailure.OUTPUT_UNAVAILABLE
        )

    cited = tuple(cited)
    opened_set = set(opened_ids)
    self_opened = tuple(evidence_id for evidence_id in cited if evidence_id in opened_set)
    foreign = tuple(
        evidence_id
        for evidence_id in cited
        if is_paragraph_id(evidence_id) and document.paragraph(evidence_id) is None
    )

    opened_spans: list[tuple[int, int]] = []
    overlapping: list[str] = []
    for evidence_id in self_opened:
        paragraph = document.paragraph(evidence_id)
        if paragraph is None:
            continue
        span = (paragraph.start_char, paragraph.end_char)
        opened_spans.append(span)
        if any(spans_overlap(span, gold) for gold in gold_spans) and evidence_id not in overlapping:
            overlapping.append(evidence_id)

    if not gold_spans:
        return EvidenceAssessment(
            node=node,
            available=True,
            cited=cited,
            self_opened=self_opened,
            foreign=foreign,
            opened_spans=tuple(opened_spans),
            failure=EvidenceFailure.NO_GOLD_SPAN,
        )

    overlaps_gold = bool(overlapping)
    if not cited:
        failure = EvidenceFailure.NO_EVIDENCE
    elif not self_opened:
        failure = EvidenceFailure.NOT_SELF_OPENED
    elif foreign:
        failure = EvidenceFailure.FOREIGN_EVIDENCE
    elif not overlaps_gold:
        failure = EvidenceFailure.NO_GOLD_OVERLAP
    else:
        failure = None

    return EvidenceAssessment(
        node=node,
        available=True,
        cited=cited,
        self_opened=self_opened,
        foreign=foreign,
        opened_spans=tuple(opened_spans),
        gold_spans=tuple(gold_spans),
        overlapping_ids=tuple(overlapping),
        overlaps_gold=overlaps_gold,
        failure=failure,
    )


# --------------------------------------------------------------------------
# The scored row
# --------------------------------------------------------------------------


class RunScore(BaseModel):
    """One tidy row per run: everything the processed layer reports.

    Flat, and deliberately so. A row that nested its findings would be pleasant
    to read in JSON and miserable to analyse in anything else, and the whole
    point of this artifact is that it goes into a dataframe.

    ``notes`` carries the reason for every ``None``. A row that reports
    ``correct=None`` without saying why is a row an analyst has to go back to
    the raw tree to interpret.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    score_schema_version: str = SCORE_SCHEMA_VERSION

    # -- identity ----------------------------------------------------------
    run_id: str
    experiment_id: str
    case_id: str
    contract_id: str
    condition_id: str
    error_condition: ErrorCondition
    source_access: bool
    verification_required: bool
    repeat_index: int = Field(ge=0)
    plan_fingerprint: str

    # -- run outcome -------------------------------------------------------
    run_status: RunStatus
    completed: bool
    protocol_failure: bool
    failure_type: str | None = None
    failure_stage: str | None = None
    verification_failure: bool = False
    tool_failure: bool = False
    model_output_failure: bool = False
    steps_executed: int = Field(default=0, ge=0)
    revision_round: int = Field(default=0, ge=0)

    # -- the case's labels (scorer-only; never in a raw artifact) ----------
    gold_target_clause_status: dict[str, ClauseStatus]
    """One gold label per target category, as the case declares them."""

    gold_action: Decision
    is_negative_sentinel: bool
    target_category: str
    """The target category this case is *about* -- the one E1 removes."""

    @property
    def gold_status(self) -> ClauseStatus:
        """The gold label of the category this case is about.

        A convenience over ``gold_target_clause_status[target_category]``, and
        deliberately named for the *category* rather than the contract: there is
        no contract-level gold status, and a reader who wants one has to say
        which category they mean.
        """
        return self.gold_target_clause_status[self.target_category]

    # -- the answer --------------------------------------------------------
    final_decision: Decision | None = None
    final_action_correct: bool | None = None
    """Whether the Compliance decision equals the case's gold action.

    ``None`` when the run produced no final decision. A run that died is not a
    wrong answer, and folding the two together would inflate the error rate with
    protocol failures.
    """

    # -- fact, action and evidence, kept apart (§6) ------------------------
    #
    # These are the spec's named columns, and they are columns rather than
    # something a reader is expected to recompute. A run whose final action is
    # correct because the agent escalated defensively, while never recovering
    # the clause, must not be describable as a successful correction -- and the
    # way to guarantee that is for the claims to sit in different columns that
    # can disagree.
    manager_target_clause_status: dict[str, ClauseStatus] | None = None
    compliance_target_clause_status: dict[str, ClauseStatus] | None = None
    """What each node assessed, per target category. ``None`` if it produced nothing."""

    @property
    def manager_status(self) -> ClauseStatus | None:
        """What the Manager said about the category this case is about.

        ``None`` when the node produced nothing, or when its assessment does not
        name this category at all -- which ``require_policy_targets`` refuses at
        the node boundary, so in a completed run it means the node failed.
        """
        return (self.manager_target_clause_status or {}).get(self.target_category)

    @property
    def compliance_status(self) -> ClauseStatus | None:
        """What Compliance said about the category this case is about."""
        return (self.compliance_target_clause_status or {}).get(self.target_category)

    manager_clause_status_correct_by_category: dict[str, bool] = Field(default_factory=dict)
    compliance_clause_status_correct_by_category: dict[str, bool] = Field(default_factory=dict)
    """The per-category comparison, so a partly-right assessment stays visible.

    A single boolean over a two-category assessment loses the case where one
    category was right and the other wrong, which is the interesting one: it is
    what an agent that resolved one clause and gave up on the other looks like.
    """

    manager_clause_status_correct: bool | None = None
    compliance_clause_status_correct: bool | None = None
    """Whether *every* target category matched gold. Stricter than before.

    Under ``v1`` there was one status and one comparison. With two categories,
    "correct" means both, and the per-category columns above carry the detail.
    """

    manager_fact_recovery_correct: bool | None = None
    compliance_fact_recovery_correct: bool | None = None
    """Whether the node recovered the specific fact the error destroyed.

    Defined on the case's own target category, and only where its gold label is
    ``present``: that is the claim E1 deletes, so it is the only fact there is
    to recover. ``None`` for a sentinel, where nothing was removed.

    This is the column GATE4-DESIGN-01 asked for. It is *not* independent of
    :attr:`final_action_correct` -- POLICY-01 maps any ``present`` target to
    ESCALATE, so recovering the fact implies the action, and the cell
    ``fact_recovery_correct=True, final_action_correct=False`` is empty by
    construction. Keeping the two as separate columns is what makes that
    implication checkable instead of assumed; the diagnostics check it directly.
    """

    manager_corrected_with_evidence: bool | None = None
    compliance_corrected_with_evidence: bool | None = None

    @property
    def omission_recovered_with_evidence(self) -> bool | None:
        """The confirmed primary outcome for an assessable positive E1 run.

        Either node may recover the omitted fact before the final decision.
        Source-backed recovery is impossible in A0V0, so a completed A0V0 E1
        run returns ``False`` even if it guesses the missing fact correctly.
        E0, sentinels, and protocol failures are outside this denominator.
        """
        if (
            self.error_condition is not ErrorCondition.E1
            or self.is_negative_sentinel
            or not self.completed
        ):
            return None
        values = (
            self.manager_corrected_with_evidence,
            self.compliance_corrected_with_evidence,
        )
        if all(value is None for value in values):
            return None
        return any(value is True for value in values)

    false_escalation: bool | None = None
    """A negative sentinel answered ESCALATE. ``None`` for every other case.

    Not-applicable rather than ``False`` on positive cases: there is no true
    negative to have falsely flagged, so ``False`` there would read as a
    measurement of something that was never at risk.
    """

    # -- error propagation -------------------------------------------------
    error_survival_manager: bool | None = None
    error_survival_compliance: bool | None = None
    correction_stage: CorrectionStage

    # -- evidence ----------------------------------------------------------
    manager_evidence: EvidenceAssessment
    compliance_evidence: EvidenceAssessment

    # -- verification ------------------------------------------------------
    manager_verification_required: bool = False
    manager_verification_checked: bool = False
    manager_verification_satisfied: bool | None = None
    compliance_verification_required: bool = False
    compliance_verification_checked: bool = False
    compliance_verification_satisfied: bool | None = None
    verification_failures: tuple[str, ...] = ()

    # -- declared vs derived verification ----------------------------------
    #
    # The declared value is what the model said it did; the derived value is
    # what the ledger shows it did. The derived one is the verdict -- but the
    # *disagreement* between them is a measurement in its own right, and one
    # that ``v1`` could not take at all, because the two were the same field.
    manager_verification_basis_declared: VerificationBasis | None = None
    manager_verification_basis_derived: VerificationBasis | None = None
    manager_verification_basis_agrees: bool | None = None
    compliance_verification_basis_declared: VerificationBasis | None = None
    compliance_verification_basis_derived: VerificationBasis | None = None
    compliance_verification_basis_agrees: bool | None = None

    # -- claim adoption integrity -----------------------------------------
    manager_adopted_claims: tuple[str, ...] = ()
    compliance_adopted_claims: tuple[str, ...] = ()
    unknown_adopted_claims: tuple[str, ...] = ()
    """Claim ids a node adopted that were not in the material it was given.

    Non-empty means an agent adopted a claim nothing in its permitted input
    could have produced. It is an integrity finding about the run, not a
    research outcome, and it is reported rather than raised because a run that
    does it is still an observation.
    """

    # -- source access -----------------------------------------------------
    #
    # Per role, not just in total: the burden question is which agent spent the
    # calls, and the per-role split cannot be recovered from a total. Totals
    # that are exactly the sum of the columns beside them (search_calls,
    # open_calls, total_tool_calls) are deliberately absent -- a redundant total
    # is a column that can disagree with its own parts.
    manager_searches: int = Field(default=0, ge=0)
    manager_opens: int = Field(default=0, ge=0)
    compliance_searches: int = Field(default=0, ge=0)
    compliance_opens: int = Field(default=0, ge=0)
    manager_self_opened_count: int = Field(default=0, ge=0)
    compliance_self_opened_count: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tool_failures: int = Field(default=0, ge=0)

    # -- usage -------------------------------------------------------------
    model_calls: int = Field(default=0, ge=0)
    manager_model_calls: int = Field(default=0, ge=0)
    compliance_model_calls: int = Field(default=0, ge=0)
    format_repairs: int = Field(default=0, ge=0)
    """Calls spent repairing a response that was not a parseable object.

    The spec's ``repair_calls`` under the name this codebase uses throughout."""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_seconds: float | None = None
    estimated_cost_usd: float | None = None

    # -- diagnostics -------------------------------------------------------
    notes: tuple[str, ...] = ()

    @property
    def cell(self) -> tuple[str, str, str, int]:
        """The experimental cell this row belongs to."""
        return (self.case_id, self.error_condition.value, self.condition_id, self.repeat_index)

    @property
    def produced_answer(self) -> bool:
        return self.final_decision is not None

    @property
    def correct(self) -> bool | None:
        """The short name for :attr:`final_action_correct`."""
        return self.final_action_correct


# --------------------------------------------------------------------------
# Scoring one run
# --------------------------------------------------------------------------


def _sum_or_none(values: Iterable[int | float | None], label: str, notes: list[str]):
    """Sum a per-call field, or ``None`` if any call failed to report it.

    All-or-nothing on purpose. A total over the calls that happened to report a
    field is a number that looks like a measurement and is not one, and the
    difference is invisible in the output.
    """
    values = list(values)
    if not values:
        return None
    if any(value is None for value in values):
        missing = sum(1 for value in values if value is None)
        notes.append(f"{label}: {missing} of {len(values)} call(s) reported no value")
        return None
    return sum(values)  # type: ignore[arg-type]


def _clause_status_correct_by_category(
    *,
    node: str,
    gold: Mapping[str, ClauseStatus],
    statuses: Mapping[str, ClauseStatus] | None,
    notes: list[str],
) -> dict[str, bool]:
    """Per-category agreement between one node's assessment and gold.

    Every category the case labels is reported, including one the node failed to
    assess at all -- an unanswered category is a disagreement, not a missing
    row, because dropping it would let an agent improve its score by saying less.
    """
    if statuses is None:
        notes.append(f"{node}_clause_status_correct: the node produced no output")
        return {}
    return {
        category: statuses.get(category) is gold_status
        for category, gold_status in gold.items()
    }


def _all_categories_correct(per_category: Mapping[str, bool]) -> bool | None:
    """Whether every labelled category matched. ``None`` if there were none."""
    if not per_category:
        return None
    return all(per_category.values())


def _fact_recovery_correct(
    *,
    node: str,
    target_category: str,
    gold: Mapping[str, ClauseStatus],
    statuses: Mapping[str, ClauseStatus] | None,
    notes: list[str],
) -> bool | None:
    """Whether one node recovered the fact the omission destroyed.

    Defined on the case's own target category, and applicable only where that
    category's gold label is ``present``: E1 deletes the claim asserting that
    clause, so on the omission arm it is the fact that was lost and on the
    control arm it is the fact that had to survive. A sentinel has no such
    claim, so the column is ``None`` rather than ``False`` -- nothing was
    removed, and ``False`` would read as a failure to recover nothing.
    """
    if statuses is None:
        notes.append(f"{node}_fact_recovery_correct: the node produced no output")
        return None
    gold_status = gold[target_category]
    if gold_status is not ClauseStatus.PRESENT:
        notes.append(
            f"{node}_fact_recovery_correct: not applicable, the case's target "
            "clause is not present, so no fact was removed"
        )
        return None
    return statuses.get(target_category) is gold_status


def _corrected_with_evidence(
    *,
    node: str,
    gold_status: ClauseStatus,
    status: ClauseStatus | None,
    evidence: EvidenceAssessment,
    notes: list[str],
) -> bool | None:
    """§5E: the clause was recovered *and* the recovery was evidence-backed.

    Stricter than a correct status, and checked in the spec's order so the note
    names the first component that failed rather than the last: recovered, then
    cited, then self-opened, then in-contract, then overlapping gold. The last
    four are already collapsed into ``evidence.failure``, which orders them the
    same way.
    """
    if status is None:
        notes.append(f"{node}_corrected_with_evidence: the node produced no output")
        return None
    if gold_status is not ClauseStatus.PRESENT:
        notes.append(
            f"{node}_corrected_with_evidence: not applicable, the case's target "
            "clause is not present, so there is no recovery to evidence"
        )
        return None
    if status is not gold_status:
        notes.append(
            f"{node}_corrected_with_evidence: false, target_not_recovered "
            f"(clause_status={status.value}, gold={gold_status.value})"
        )
        return False
    if evidence.failure is not None:
        notes.append(
            f"{node}_corrected_with_evidence: false, {evidence.failure.value}"
        )
        return False
    return True


def _false_escalation(
    *,
    is_negative_sentinel: bool,
    final_decision: Decision | None,
    notes: list[str],
) -> bool | None:
    """§5F: a negative sentinel answered ESCALATE.

    ``None`` for positive cases -- not ``False``. ``False`` on a positive case
    would read as "this run correctly declined to flag a false positive", which
    is a measurement of something the case never put at risk.
    """
    if not is_negative_sentinel:
        notes.append(
            "false_escalation: not applicable, the case's gold action is not ACCEPT"
        )
        return None
    if final_decision is None:
        notes.append("false_escalation: the run produced no final decision")
        return None
    return final_decision is Decision.ESCALATE


def _correction_stage(
    *,
    error_condition: ErrorCondition,
    gold_status: ClauseStatus,
    manager_status: ClauseStatus | None,
    compliance_status: ClauseStatus | None,
    notes: list[str],
) -> CorrectionStage:
    if manager_status is None or compliance_status is None:
        notes.append(
            "correction_stage: the run produced no usable output from both agents"
        )
        return CorrectionStage.UNAVAILABLE
    if error_condition is ErrorCondition.E0:
        return CorrectionStage.ALREADY_CORRECT
    if gold_status is not ClauseStatus.PRESENT:
        return CorrectionStage.NOT_APPLICABLE
    if manager_status is gold_status:
        return CorrectionStage.MANAGER
    if compliance_status is gold_status:
        return CorrectionStage.COMPLIANCE
    return CorrectionStage.NEVER


def _error_survival(
    *,
    node: str,
    error_condition: ErrorCondition,
    gold_status: ClauseStatus,
    status: ClauseStatus | None,
    notes: list[str],
) -> bool | None:
    """Whether the omission still shows in one node's conclusion.

    Applicable exactly when the arm carried the error (E1) and the error was
    the loss of a claim asserting presence. In that case the node "recovered"
    by concluding PRESENT, and the error survived if it concluded anything else.
    Every other combination returns ``None`` with a note, and the note plus the
    row's own ``run_status`` and ``protocol_failure`` columns is enough to tell
    "not applicable" from "the run produced nothing".
    """
    if status is None:
        notes.append(f"error_survival_{node}: the node produced no output")
        return None
    if error_condition is ErrorCondition.E0:
        notes.append(f"error_survival_{node}: not applicable, the arm carried no error")
        return None
    if gold_status is not ClauseStatus.PRESENT:
        notes.append(
            f"error_survival_{node}: not applicable, the omitted claim did not assert "
            "presence"
        )
        return None
    return status is not gold_status


def score_run(
    raw: RawRun,
    *,
    case: CaseSpec,
    document: ContractDocument,
    pricing: PricingTable | None = None,
) -> RunScore:
    """Derive one run's row from its raw artifacts and its case's labels.

    ``case`` and ``document`` are the only gold-bearing inputs, and they come
    from the registry -- never from the raw tree, which has no gold in it and
    refuses to carry any.
    """
    artifact = raw.artifact
    execution = artifact.execution
    if artifact.case_id != case.case_id:
        raise ScoringError(
            f"run {artifact.run_id!r} is about case {artifact.case_id!r} but was "
            f"scored against case {case.case_id!r}"
        )

    notes: list[str] = []
    gold_spans = case.gold_spans()
    manager_output = execution.manager_output
    compliance_output = execution.compliance_output

    # -- evidence, replayed from the tool log ------------------------------
    manager_ledger = replay_ledger(raw.tool_calls, node="manager")
    compliance_ledger = replay_ledger(raw.tool_calls, node="compliance")
    manager_evidence = _assess_evidence(
        node="manager",
        cited=None if manager_output is None else _reported_ids(manager_output),
        opened_ids=manager_ledger.opened_ids,
        document=document,
        gold_spans=gold_spans,
    )
    compliance_evidence = _assess_evidence(
        node="compliance",
        cited=None if compliance_output is None else _reported_ids(compliance_output),
        opened_ids=compliance_ledger.opened_ids,
        document=document,
        gold_spans=gold_spans,
    )

    # -- the answer --------------------------------------------------------
    final_decision = None if compliance_output is None else compliance_output.decision
    if final_decision is None:
        notes.append("final_action_correct: the run produced no final decision")
        final_action_correct = None
    else:
        final_action_correct = final_decision is case.gold_action

    # The per-category assessments, and the single "the case's own target
    # category" value the older metrics below are defined on. Those metrics --
    # error survival, correction stage, corrected-with-evidence -- were all
    # written about the clause the case is about, and E1 deletes exactly that
    # clause's claim, so they keep their meaning unchanged when read through the
    # target category rather than through a contract-level status.
    gold = case.gold_target_clause_status
    target_category = case.target_category
    target_gold_status = case.gold_status_for(target_category)

    manager_statuses = None if manager_output is None else dict(manager_output.target_clause_status)
    compliance_statuses = (
        None if compliance_output is None else dict(compliance_output.target_clause_status)
    )
    manager_status = None if manager_statuses is None else manager_statuses.get(target_category)
    compliance_status = (
        None if compliance_statuses is None else compliance_statuses.get(target_category)
    )

    # -- verification ------------------------------------------------------
    manager_verifications = raw.verifications_of("manager")
    compliance_verifications = raw.verifications_of("compliance")
    verification_failures: list[str] = []
    for outcome in raw.verifications:
        for failure in outcome.failures:
            if failure.value not in verification_failures:
                verification_failures.append(failure.value)

    def satisfied(outcomes: Sequence) -> bool | None:
        if not outcomes:
            return None
        return all(outcome.satisfied for outcome in outcomes)

    def basis(outcomes: Sequence, attr: str) -> VerificationBasis | None:
        """The basis one node declared, or the one the ledger derived.

        ``None`` when the node was never assessed, and also when its verdicts
        disagree with each other -- a node with two invocations that derived
        different bases has no single value, and picking one would invent a fact.
        """
        values = {getattr(outcome, attr) for outcome in outcomes}
        values.discard(None)
        if len(values) != 1:
            return None
        return values.pop()

    def basis_agrees(outcomes: Sequence) -> bool | None:
        if not outcomes:
            return None
        return all(outcome.basis_agrees for outcome in outcomes)

    manager_by_category = _clause_status_correct_by_category(
        node="manager", gold=gold, statuses=manager_statuses, notes=notes
    )
    compliance_by_category = _clause_status_correct_by_category(
        node="compliance", gold=gold, statuses=compliance_statuses, notes=notes
    )

    # -- claim adoption ----------------------------------------------------
    manager_adopted = (
        ()
        if manager_output is None
        else manager_output.evidence_provenance.upstream_claim_ids
    )
    compliance_adopted = (
        ()
        if compliance_output is None
        else compliance_output.evidence_provenance.upstream_claim_ids
    )
    # The Manager's permitted claim vocabulary is the memo it was actually
    # given. ``omission_target_claim_id`` names the claim the *E1* arm deletes,
    # so it is subtracted only on an E1 run: on the E0 arm the memo still
    # contains that claim, and a Manager that adopts it is reading its input
    # correctly. Subtracting it unconditionally would report the ordinary case as
    # an integrity finding, which is the one thing this metric must not do.
    omitted: set[str] = set()
    if raw.artifact.error_condition is ErrorCondition.E1 and case.omission_target_claim_id:
        omitted = {case.omission_target_claim_id}
    memo_claim_ids = {claim.claim_id for claim in case.memo.claims} - omitted
    # Compliance sees the handoff, and the handoff carries the Manager's own
    # adopted ids. Anything beyond that, Compliance could not have been given.
    compliance_vocabulary = set(manager_adopted)
    unknown_adopted: list[str] = []
    for claim_id in manager_adopted:
        if claim_id not in memo_claim_ids and claim_id not in unknown_adopted:
            unknown_adopted.append(claim_id)
    for claim_id in compliance_adopted:
        if claim_id not in compliance_vocabulary and claim_id not in unknown_adopted:
            unknown_adopted.append(claim_id)

    # -- usage -------------------------------------------------------------
    calls = raw.model_calls
    prompt_tokens = _sum_or_none(
        [None if call.usage is None else call.usage.prompt_tokens for call in calls],
        "prompt_tokens",
        notes,
    )
    completion_tokens = _sum_or_none(
        [None if call.usage is None else call.usage.completion_tokens for call in calls],
        "completion_tokens",
        notes,
    )
    total_tokens = _sum_or_none(
        [None if call.usage is None else call.usage.total_tokens for call in calls],
        "total_tokens",
        notes,
    )
    latency_seconds = _sum_or_none(
        [call.latency_seconds for call in calls], "latency_seconds", notes
    )

    estimated_cost: float | None = None
    if pricing is None:
        notes.append("estimated_cost_usd: no pricing table was supplied")
    else:
        per_call = [
            pricing.estimate(
                model_id=call.params.model_id,
                prompt_tokens=None if call.usage is None else call.usage.prompt_tokens,
                completion_tokens=(
                    None if call.usage is None else call.usage.completion_tokens
                ),
            )
            for call in calls
        ]
        if per_call and all(value is not None for value in per_call):
            estimated_cost = round(sum(per_call), 8)  # type: ignore[arg-type]
        else:
            notes.append(
                "estimated_cost_usd: a call's model or token usage is not in the "
                "pricing table"
            )

    manager_searches = sum(
        1 for record in raw.tool_calls if record.node == "manager" and record.tool == SEARCH_CONTRACT
    )
    manager_opens = sum(
        1 for record in raw.tool_calls if record.node == "manager" and record.tool == OPEN_SOURCE_SPAN
    )
    compliance_searches = sum(
        1
        for record in raw.tool_calls
        if record.node == "compliance" and record.tool == SEARCH_CONTRACT
    )
    compliance_opens = sum(
        1
        for record in raw.tool_calls
        if record.node == "compliance" and record.tool == OPEN_SOURCE_SPAN
    )

    failure = artifact.failure
    return RunScore(
        run_id=artifact.run_id,
        experiment_id=artifact.experiment_id,
        case_id=artifact.case_id,
        contract_id=artifact.contract_id,
        condition_id=artifact.condition_id,
        error_condition=artifact.error_condition,
        source_access=artifact.source_access,
        verification_required=artifact.verification_required,
        repeat_index=artifact.repeat_index,
        plan_fingerprint=artifact.plan_fingerprint,
        run_status=execution.status,
        completed=execution.status is RunStatus.COMPLETED,
        protocol_failure=bool(failure and failure.protocol_failure),
        failure_type=None if failure is None else failure.failure_type,
        failure_stage=None if failure is None else failure.failure_stage,
        verification_failure=bool(failure and failure.verification_failure),
        tool_failure=bool(failure and failure.tool_failure),
        model_output_failure=bool(failure and failure.model_output_failure),
        steps_executed=execution.steps_executed,
        revision_round=execution.revision_round,
        gold_target_clause_status=dict(gold),
        gold_action=case.gold_action,
        is_negative_sentinel=case.is_negative_sentinel,
        target_category=target_category,
        final_decision=final_decision,
        final_action_correct=final_action_correct,
        manager_target_clause_status=manager_statuses,
        compliance_target_clause_status=compliance_statuses,
        manager_clause_status_correct_by_category=manager_by_category,
        compliance_clause_status_correct_by_category=compliance_by_category,
        manager_clause_status_correct=_all_categories_correct(manager_by_category),
        compliance_clause_status_correct=_all_categories_correct(compliance_by_category),
        manager_fact_recovery_correct=_fact_recovery_correct(
            node="manager",
            target_category=target_category,
            gold=gold,
            statuses=manager_statuses,
            notes=notes,
        ),
        compliance_fact_recovery_correct=_fact_recovery_correct(
            node="compliance",
            target_category=target_category,
            gold=gold,
            statuses=compliance_statuses,
            notes=notes,
        ),
        manager_corrected_with_evidence=_corrected_with_evidence(
            node="manager",
            gold_status=target_gold_status,
            status=manager_status,
            evidence=manager_evidence,
            notes=notes,
        ),
        compliance_corrected_with_evidence=_corrected_with_evidence(
            node="compliance",
            gold_status=target_gold_status,
            status=compliance_status,
            evidence=compliance_evidence,
            notes=notes,
        ),
        false_escalation=_false_escalation(
            is_negative_sentinel=case.is_negative_sentinel,
            final_decision=final_decision,
            notes=notes,
        ),
        error_survival_manager=_error_survival(
            node="manager",
            error_condition=artifact.error_condition,
            gold_status=target_gold_status,
            status=manager_status,
            notes=notes,
        ),
        error_survival_compliance=_error_survival(
            node="compliance",
            error_condition=artifact.error_condition,
            gold_status=target_gold_status,
            status=compliance_status,
            notes=notes,
        ),
        correction_stage=_correction_stage(
            error_condition=artifact.error_condition,
            gold_status=target_gold_status,
            manager_status=manager_status,
            compliance_status=compliance_status,
            notes=notes,
        ),
        manager_evidence=manager_evidence,
        compliance_evidence=compliance_evidence,
        manager_verification_required=bool(
            manager_verifications and manager_verifications[0].required
        ),
        manager_verification_checked=bool(
            manager_verifications and all(o.checked for o in manager_verifications)
        ),
        manager_verification_satisfied=satisfied(manager_verifications),
        compliance_verification_required=bool(
            compliance_verifications and compliance_verifications[0].required
        ),
        compliance_verification_checked=bool(
            compliance_verifications and all(o.checked for o in compliance_verifications)
        ),
        compliance_verification_satisfied=satisfied(compliance_verifications),
        verification_failures=tuple(verification_failures),
        manager_verification_basis_declared=basis(
            manager_verifications, "declared_basis"
        ),
        manager_verification_basis_derived=basis(manager_verifications, "derived_basis"),
        manager_verification_basis_agrees=basis_agrees(manager_verifications),
        compliance_verification_basis_declared=basis(
            compliance_verifications, "declared_basis"
        ),
        compliance_verification_basis_derived=basis(
            compliance_verifications, "derived_basis"
        ),
        compliance_verification_basis_agrees=basis_agrees(compliance_verifications),
        manager_adopted_claims=tuple(manager_adopted),
        compliance_adopted_claims=tuple(compliance_adopted),
        unknown_adopted_claims=tuple(unknown_adopted),
        manager_model_calls=sum(1 for call in calls if call.role == "manager"),
        compliance_model_calls=sum(1 for call in calls if call.role == "compliance"),
        manager_self_opened_count=len(manager_evidence.self_opened),
        compliance_self_opened_count=len(compliance_evidence.self_opened),
        manager_searches=manager_searches,
        manager_opens=manager_opens,
        compliance_searches=compliance_searches,
        compliance_opens=compliance_opens,
        tool_calls=len(raw.tool_calls),
        tool_failures=sum(1 for record in raw.tool_calls if not record.ok),
        model_calls=len(calls),
        format_repairs=sum(1 for call in calls if call.is_repair),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_seconds=latency_seconds,
        estimated_cost_usd=estimated_cost,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# Scoring a whole experiment
# --------------------------------------------------------------------------


def _flatten(prefix: str, value: object, out: dict[str, object]) -> None:
    """Flatten a row into CSV columns, recursing into nested models.

    Mechanical, and derived from the model rather than from a hand-written
    column list: a column that is added to :class:`RunScore` and forgotten here
    would be a silently missing measurement, and there is no way to forget one.
    """
    if isinstance(value, BaseModel):
        for name, field_value in value:
            _flatten(f"{prefix}_{name}" if prefix else name, field_value, out)
        return
    if isinstance(value, (tuple, list)):
        out[prefix] = ";".join(str(item) for item in value)
        return
    if isinstance(value, Enum):
        out[prefix] = value.value
        return
    out[prefix] = value


def _columns(rows: Sequence[RunScore]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        flat: dict[str, object] = {}
        _flatten("", row, flat)
        for name in flat:
            if name not in columns:
                columns.append(name)
    return columns


def write_scores_csv(rows: Sequence[RunScore], path: str | Path) -> Path:
    """Write the scored rows as CSV, one row per run, columns from the model."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = _columns(rows)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            flat: dict[str, object] = {}
            _flatten("", row, flat)
            writer.writerow({name: flat.get(name, "") for name in columns})
    return target


class ExperimentScores(BaseModel):
    """Every run's row, in a stable order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    score_schema_version: str = SCORE_SCHEMA_VERSION
    experiment_id: str
    plan_fingerprints: tuple[str, ...] = ()
    rows: tuple[RunScore, ...] = ()

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def by_run_id(self, run_id: str) -> RunScore:
        for row in self.rows:
            if row.run_id == run_id:
                return row
        raise ScoringError(f"no scored row for run {run_id!r}")

    def of_cell(
        self, *, case_id: str, error_condition: ErrorCondition, condition_id: str
    ) -> tuple[RunScore, ...]:
        return tuple(
            row
            for row in self.rows
            if row.case_id == case_id
            and row.error_condition is error_condition
            and row.condition_id == condition_id
        )

    def write_jsonl(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(f"{row.model_dump_json()}\n" for row in self.rows), encoding="utf-8"
        )
        return target

    def write_csv(self, path: str | Path) -> Path:
        return write_scores_csv(self.rows, path)


def score_experiment(
    raw_root: str | Path,
    *,
    registry: CaseRegistry,
    pricing: PricingTable | None = None,
    run_ids: Sequence[str] | None = None,
) -> ExperimentScores:
    """Score every complete run under a raw-experiment root.

    Reads the raw tree and the registry, and nothing else. The plan is not an
    input: a run's identity is in its own ``run.json``, so processed results can
    be rebuilt from raw artifacts alone -- which is the property §10 of the
    Gate 3 specification asks for, and the one that makes deleting the processed
    tree a safe operation.

    Sorted by run id so two scoring passes produce byte-identical output.
    """
    selected = None if run_ids is None else set(run_ids)
    rows: list[RunScore] = []
    fingerprints: list[str] = []
    experiment_id = ""

    for raw in _iter_sorted(raw_root):
        if selected is not None and raw.run_id not in selected:
            continue
        experiment_id = experiment_id or raw.artifact.experiment_id
        if raw.artifact.plan_fingerprint not in fingerprints:
            fingerprints.append(raw.artifact.plan_fingerprint)
        rows.append(
            score_run(
                raw,
                case=registry.get(raw.artifact.case_id),
                document=registry.document(raw.artifact.case_id),
                pricing=pricing,
            )
        )

    return ExperimentScores(
        experiment_id=experiment_id,
        plan_fingerprints=tuple(fingerprints),
        rows=tuple(rows),
    )


def _iter_sorted(raw_root: str | Path) -> Iterable[RawRun]:
    """Complete runs under a root, in run-id order.

    A thin wrapper over :func:`~pilot01.experiment.artifacts.iter_raw_runs` that
    returns an empty iterator for a missing root instead of raising: scoring a
    tree that has not been run yet yields no rows, which is the truthful answer,
    and the summary layer reports the empty result as such.
    """
    from .artifacts import iter_raw_runs

    return iter_raw_runs(raw_root)
