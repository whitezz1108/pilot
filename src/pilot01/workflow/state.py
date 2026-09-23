"""The full experimental state.

``ExperimentState`` is the single source of truth for a run. It contains both
agent-facing artifacts and hidden experimenter data (gold labels, treatment
labels, omission provenance).

**Agents never receive this object.** Agent nodes receive restricted views
built by :mod:`pilot01.workflow.views`.

Two structural protections live here:

1. Every identity, treatment and case-metadata field is ``frozen`` at the
   pydantic level, so the orchestration layer cannot silently mutate an
   experimental condition part-way through a run.
2. :meth:`ExperimentState.check_treatment_integrity` re-derives the governance
   flags from the condition config and cross-checks the memo against the hidden
   omission record, so a run cannot start from a state whose treatment labels,
   flags, memo and gold disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schemas import (
    AnalystMemo,
    ClauseStatus,
    ComplianceOutput,
    ErrorCondition,
    ExperimentalPolicy,
    HiddenExperimentData,
    HiddenGold,
    ManagerOutput,
    OmissionRecord,
    expected_decision,
    require_policy_targets,
)

if TYPE_CHECKING:  # import cycle: pilot01.config imports pilot01.workflow.transitions
    from ..config import ConditionsConfig

from .transitions import ProtocolError

__all__ = ["RunStatus", "ExperimentState", "TreatmentIntegrityError"]


class TreatmentIntegrityError(ProtocolError):
    """The state's treatment labels, flags, artifacts and gold disagree.

    A subclass of :class:`~pilot01.workflow.transitions.ProtocolError` so the
    runner treats it as what it is: a structural violation that aborts the run
    and is recorded, rather than an observation to be salvaged.
    """


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExperimentState(BaseModel):
    """Complete state of one experimental run.

    Fields marked ``frozen`` are fixed for the lifetime of the run.
    """

    model_config = ConfigDict(extra="forbid")

    # -- identity (frozen) -------------------------------------------------
    run_id: str = Field(frozen=True)
    case_id: str = Field(frozen=True)
    repetition_id: int = Field(frozen=True, ge=0)
    experiment_version: str = Field(frozen=True, default="1")
    workflow_version: str = Field(frozen=True, default="1")
    policy_version: str = Field(frozen=True, default="1")

    # -- treatment / governance (frozen, hidden in part) -------------------
    error_condition: ErrorCondition = Field(frozen=True)
    """HIDDEN. E0 (correct memo) or E1 (omission memo)."""
    condition_id: str = Field(frozen=True)
    """Governance condition id, e.g. ``A1V1``. Not agent-facing."""
    source_access: bool = Field(frozen=True)
    verification_required: bool = Field(frozen=True)

    # -- case metadata (frozen) -------------------------------------------
    contract_id: str = Field(frozen=True)
    contract_text_hash: str = Field(frozen=True)
    target_category: str = Field(frozen=True)
    policy: ExperimentalPolicy = Field(frozen=True)
    """The policy under test. Required: its target clause categories are an
    experimental design input (see :func:`pilot01.schemas.policy_01`), so the
    state cannot fall back to a built-in default policy."""

    # -- artifacts (frozen) ------------------------------------------------
    analyst_memo: AnalystMemo = Field(frozen=True)
    hidden: HiddenExperimentData = Field(frozen=True)

    # -- outputs (mutable: written by the runner) --------------------------
    manager_output: ManagerOutput | None = None
    compliance_output: ComplianceOutput | None = None

    # -- workflow position -------------------------------------------------
    current_node: str = ""
    revision_round: int = Field(default=0, ge=0)
    status: RunStatus = RunStatus.CREATED
    steps_executed: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _check_gold_consistency(self) -> "ExperimentState":
        gold = self.hidden.gold
        require_policy_targets(gold.gold_target_clause_status, self.policy)
        expected = expected_decision(gold.gold_target_clause_status, self.policy)
        if gold.gold_action is not expected:
            assessed = ", ".join(
                f"{category}={status.value}"
                for category, status in sorted(gold.gold_target_clause_status.items())
            )
            raise TreatmentIntegrityError(
                f"gold label inconsistency: gold_action={gold.gold_action.value} "
                f"but policy {self.policy.policy_id!r} maps "
                f"gold_target_clause_status=[{assessed}] to {expected.value}"
            )
        if self.analyst_memo.case_id != self.case_id:
            raise TreatmentIntegrityError(
                f"memo {self.analyst_memo.memo_id!r} belongs to case "
                f"{self.analyst_memo.case_id!r}, state is case {self.case_id!r}"
            )
        return self

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        case_id: str,
        repetition_id: int,
        condition_id: str,
        conditions: ConditionsConfig,
        error_condition: ErrorCondition,
        contract_id: str,
        contract_text_hash: str,
        target_category: str,
        memo: AnalystMemo,
        gold_target_clause_status: Mapping[str, ClauseStatus],
        policy: ExperimentalPolicy,
        gold_evidence_offsets: tuple[int, ...] = (),
        omission: OmissionRecord | None = None,
        experiment_version: str = "1",
        workflow_version: str = "1",
    ) -> "ExperimentState":
        """Build a state, deriving the governance flags from the condition config.

        Callers never pass ``source_access`` / ``verification_required``
        directly, so the flags cannot drift from ``condition_id``. The policy is
        required, because its target clause categories are an audited design
        input rather than a code default.
        """
        if not conditions.is_main(condition_id):
            raise TreatmentIntegrityError(
                f"condition {condition_id!r} is not a permitted main experimental "
                f"condition; main conditions are {conditions.main_conditions}"
            )
        condition = conditions.get(condition_id)
        active_policy = policy

        return cls(
            run_id=run_id,
            case_id=case_id,
            repetition_id=repetition_id,
            experiment_version=experiment_version,
            workflow_version=workflow_version,
            policy_version=active_policy.policy_version,
            error_condition=error_condition,
            condition_id=condition_id,
            source_access=condition.source_access,
            verification_required=condition.verification_required,
            contract_id=contract_id,
            contract_text_hash=contract_text_hash,
            target_category=target_category,
            policy=active_policy,
            analyst_memo=memo,
            hidden=HiddenExperimentData(
                gold=HiddenGold(
                    gold_target_clause_status=dict(gold_target_clause_status),
                    gold_action=expected_decision(
                        gold_target_clause_status, active_policy
                    ),
                    gold_evidence_offsets=gold_evidence_offsets,
                ),
                omission=omission,
            ),
        )

    # -- integrity ---------------------------------------------------------

    def check_treatment_integrity(self, conditions: ConditionsConfig) -> None:
        """Assert treatment labels, flags, memo and hidden record agree.

        Called by the runner before executing any node. Raises
        :class:`TreatmentIntegrityError`.
        """
        if not conditions.is_main(self.condition_id):
            raise TreatmentIntegrityError(
                f"run {self.run_id!r} uses non-main condition {self.condition_id!r}"
            )
        condition = conditions.get(self.condition_id)
        if self.source_access is not condition.source_access:
            raise TreatmentIntegrityError(
                f"source_access={self.source_access} does not match condition "
                f"{self.condition_id} ({condition.source_access})"
            )
        if self.verification_required is not condition.verification_required:
            raise TreatmentIntegrityError(
                f"verification_required={self.verification_required} does not match "
                f"condition {self.condition_id} ({condition.verification_required})"
            )

        omission = self.hidden.omission
        if self.error_condition is ErrorCondition.E0:
            if omission is not None:
                raise TreatmentIntegrityError(
                    f"run {self.run_id!r} is labelled E0 but carries an omission record"
                )
            return

        if omission is None:
            raise TreatmentIntegrityError(
                f"run {self.run_id!r} is labelled E1 but carries no omission record"
            )
        if omission.source_memo_id != self.analyst_memo.memo_id:
            raise TreatmentIntegrityError(
                f"omission record references memo {omission.source_memo_id!r} but the "
                f"loaded memo is {self.analyst_memo.memo_id!r}"
            )
        if len(self.analyst_memo.claims) != omission.e1_claim_count:
            raise TreatmentIntegrityError(
                f"E1 memo has {len(self.analyst_memo.claims)} claims, omission record "
                f"expects {omission.e1_claim_count}"
            )
        # The e0 -> e1 count relation is a property of the record alone and is
        # checked there, so that a deletion and a substitution cannot be
        # confused for one another in one place and not the other.
        if self.analyst_memo.claim(omission.omitted_claim_id) is not None:
            raise TreatmentIntegrityError(
                f"E1 memo still contains the omitted claim {omission.omitted_claim_id!r}"
            )
        if omission.omitted_claim_category != self.target_category:
            raise TreatmentIntegrityError(
                f"omitted claim category {omission.omitted_claim_category!r} is not the "
                f"case target category {self.target_category!r}"
            )
        if omission.is_substitution:
            # A substitution's whole purpose is that the two arms are the same
            # size, so the record is only credible if the claim it names really
            # took the target's place. Checking the index as well as the id is
            # what makes "the E1 order is the E0 order" a fact about the loaded
            # memo rather than a claim about the builder.
            replacement = self.analyst_memo.claim(omission.replacement_claim_id or "")
            if replacement is None:
                raise TreatmentIntegrityError(
                    f"run {self.run_id!r} is a substitution but its E1 memo does not "
                    f"contain the replacement claim {omission.replacement_claim_id!r}"
                )
            if replacement.category != omission.replacement_category:
                raise TreatmentIntegrityError(
                    f"replacement claim {replacement.claim_id!r} has category "
                    f"{replacement.category!r}, omission record says "
                    f"{omission.replacement_category!r}"
                )
            if self.analyst_memo.claim_ids().index(replacement.claim_id) != (
                omission.replacement_index
            ):
                raise TreatmentIntegrityError(
                    f"replacement claim {replacement.claim_id!r} sits at index "
                    f"{self.analyst_memo.claim_ids().index(replacement.claim_id)}, "
                    f"omission record says {omission.replacement_index}; a "
                    "substitution takes the omitted claim's own position"
                )
