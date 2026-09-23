"""Core schemas for the omission-propagation pilot.

Two families of data are defined here and they must never be mixed:

* **Agent-facing data** -- ``AnalystMemo``, ``ExperimentalPolicy``,
  ``ManagerOutput``, ``ComplianceOutput``. Everything an agent may observe.
* **Hidden experimenter data** -- ``HiddenExperimentData`` (gold labels plus
  the omission provenance record). Reachable only from ``ExperimentState`` and
  excluded, by construction, from the restricted views in
  :mod:`pilot01.workflow.views`.

No model here contains a chain-of-thought field, by design.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ErrorCondition",
    "ClauseStatus",
    "Decision",
    "VerificationStatus",
    "MemoClaim",
    "AnalystMemo",
    "ExperimentalPolicy",
    "policy_01",
    "expected_decision",
    "AgentOutput",
    "ManagerOutput",
    "ComplianceOutput",
    "HiddenGold",
    "OmissionRecord",
    "HiddenExperimentData",
    "OmissionResult",
    "build_omission_memo",
]


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class ErrorCondition(str, Enum):
    """Hidden experimental treatment. Never visible to any agent."""

    E0 = "E0"
    """Correct frozen memo."""

    E1 = "E1"
    """Omission memo: exactly one target policy-relevant claim deleted."""


class ClauseStatus(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    ESCALATE = "ESCALATE"
    ACCEPT = "ACCEPT"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    NOT_CHECKED = "not_checked"
    UNVERIFIABLE = "unverifiable"


# --------------------------------------------------------------------------
# Analyst memo (frozen artifact, structurally represented)
# --------------------------------------------------------------------------


class MemoClaim(BaseModel):
    """One claim in a frozen analyst memo.

    ``status`` is the analyst's assertion about the contract, not a verified
    fact. Downstream agents may adopt it, ignore it, or (when tools allow)
    check it against the source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    category: str
    status: ClauseStatus
    summary: str
    source_ids: tuple[str, ...] = ()


class AnalystMemo(BaseModel):
    """A frozen analyst memo.

    The E0 memo and its E1 omission counterpart are structurally identical
    except that the E1 memo has exactly one claim removed. In particular they
    share ``memo_id`` and ``case_id``: the memo carries **no marker** that
    would let a downstream agent tell which version it is holding. Omission
    provenance is recorded separately, in
    :class:`OmissionRecord`, which is experimenter-only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memo_id: str
    case_id: str
    claims: tuple[MemoClaim, ...]

    def claim_ids(self) -> tuple[str, ...]:
        return tuple(claim.claim_id for claim in self.claims)

    def claim(self, claim_id: str) -> MemoClaim | None:
        for claim in self.claims:
            if claim.claim_id == claim_id:
                return claim
        return None

    def claims_in_category(self, category: str) -> tuple[MemoClaim, ...]:
        return tuple(claim for claim in self.claims if claim.category == category)


# --------------------------------------------------------------------------
# Experimental policy
# --------------------------------------------------------------------------


class ExperimentalPolicy(BaseModel):
    """The organizational rule under test.

    POLICY-01: if either target critical clause is present, the case must be
    ESCALATE. Only when the relevant clauses are confirmed absent may the case
    be ACCEPT.

    This is an experimental organizational rule, not legal advice.

    ``target_clause_categories`` is **required and has no default**. Which
    clause categories count as "critical" is an experimental design decision
    that depends on the audited dataset; letting it live as a code default
    would make a placeholder silently become the policy under test. It must
    arrive from explicit configuration or case-building input, and a policy
    with no target category fails loudly rather than matching nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    policy_version: str
    target_clause_categories: tuple[str, ...]
    decision_if_target_present: Decision
    decision_if_target_absent: Decision
    description: str

    @model_validator(mode="after")
    def _require_target_categories(self) -> "ExperimentalPolicy":
        if not self.target_clause_categories:
            raise ValueError(
                f"policy {self.policy_id!r} declares no target clause categories; "
                "target categories are an experimental design input and must be "
                "supplied explicitly, never defaulted"
            )
        blank = [c for c in self.target_clause_categories if not c.strip()]
        if blank:
            raise ValueError(
                f"policy {self.policy_id!r} has a blank target clause category"
            )
        if len(set(self.target_clause_categories)) != len(self.target_clause_categories):
            raise ValueError(
                f"policy {self.policy_id!r} lists duplicate target clause "
                f"categories: {self.target_clause_categories!r}"
            )
        return self

    def is_target_category(self, category: str) -> bool:
        return category in self.target_clause_categories


def policy_01(*, target_clause_categories: tuple[str, ...]) -> ExperimentalPolicy:
    """POLICY-01, the single policy used by Pilot v1.

    ``target_clause_categories`` is keyword-only and mandatory: the clause
    categories that POLICY-01 treats as critical are an audited design input,
    not a property of the rule. They are deliberately not chosen here -- see
    the "Gate-1 placeholders" section of ``README.md``.
    """
    return ExperimentalPolicy(
        policy_id="POLICY-01",
        policy_version="1",
        target_clause_categories=target_clause_categories,
        decision_if_target_present=Decision.ESCALATE,
        decision_if_target_absent=Decision.ACCEPT,
        description=(
            "If either target critical clause is present, the case must be "
            "ESCALATE. Only when the relevant clauses are confirmed absent "
            "may the case be ACCEPT. Experimental organizational rule, not "
            "legal advice."
        ),
    )


def expected_decision(clause_status: ClauseStatus, policy: ExperimentalPolicy) -> Decision:
    """Apply the policy to a clause status.

    Only a *confirmed absent* clause status permits ACCEPT; ``unknown`` is
    therefore escalated. This is the reference application of the policy used
    to derive gold labels and to sanity-check agent outputs; it is not a
    scoring pipeline.
    """
    if clause_status is ClauseStatus.ABSENT:
        return policy.decision_if_target_absent
    return policy.decision_if_target_present


# --------------------------------------------------------------------------
# Agent outputs
# --------------------------------------------------------------------------


class AgentOutput(BaseModel):
    """Common core output shared by Manager and Compliance.

    Deliberately flat and closed (``extra="forbid"``) so that an agent cannot
    smuggle unmodelled fields into the audit record.
    """

    model_config = ConfigDict(extra="forbid")

    clause_status: ClauseStatus
    decision: Decision
    rule_id: str
    verification_status: VerificationStatus
    evidence_ids: tuple[str, ...] = ()
    adopted_upstream_claim_ids: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    reason_summary: str
    uncertainties: tuple[str, ...] = ()


class ManagerOutput(AgentOutput):
    """Manager handoff. This is the *only* upstream artifact Compliance sees."""

    role: str = "manager"


class ComplianceOutput(AgentOutput):
    """Final decision produced by Compliance."""

    role: str = "compliance"


# --------------------------------------------------------------------------
# Hidden experimenter data
# --------------------------------------------------------------------------


class HiddenGold(BaseModel):
    """Scoring labels. Must never enter a restricted view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gold_clause_status: ClauseStatus
    gold_action: Decision
    gold_evidence_offsets: tuple[int, ...] = ()


class OmissionRecord(BaseModel):
    """Provenance for an E1 omission memo.

    This preserves traceability from the E1 artifact back to the E0 artifact
    it was derived from. It is experimenter-only: the E1 memo itself carries no
    such marker.

    **Two omission shapes, both recorded here.** The manipulation is always
    "the one target claim is gone". How the gap is left is a design choice with
    a length confound attached, so the record distinguishes them explicitly
    rather than leaving a reader to infer which was used from the claim counts:

    * *deletion* (the default) -- the target claim is removed and nothing takes
      its place, so the E1 memo is one claim shorter than E0;
    * *substitution* -- the target claim is replaced in position by a neutral,
      non-target claim drawn from the same contract, so the two arms have the
      same claim count and comparable length and the omission is not legible
      from the memo's size.

    Substitution records what took the place (``replacement_*``); deletion
    leaves those fields ``None``. The two are mutually exclusive and all-or-
    nothing, enforced below, so a half-populated substitution cannot be read as
    a deletion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_memo_id: str
    omitted_claim_id: str
    omitted_claim_category: str
    omitted_claim_status: ClauseStatus
    e0_claim_count: int
    e1_claim_count: int

    replacement_claim_id: str | None = None
    replacement_category: str | None = None
    replacement_status: ClauseStatus | None = None
    replacement_index: int | None = Field(default=None, ge=0)
    """Where in the E1 claim order the replacement sits -- the target's own index."""

    @model_validator(mode="after")
    def _check_replacement(self) -> "OmissionRecord":
        replacement = (
            self.replacement_claim_id,
            self.replacement_category,
            self.replacement_status,
            self.replacement_index,
        )
        present = [value for value in replacement if value is not None]
        if present and len(present) != len(replacement):
            raise ValueError(
                "an omission record's replacement fields are all-or-nothing; got "
                f"replacement_claim_id={self.replacement_claim_id!r}, "
                f"replacement_category={self.replacement_category!r}, "
                f"replacement_status={self.replacement_status!r}, "
                f"replacement_index={self.replacement_index!r}"
            )
        if self.replacement_claim_id is not None and self.e1_claim_count != self.e0_claim_count:
            raise ValueError(
                f"a substitution keeps the claim count, but this record goes "
                f"{self.e0_claim_count} -> {self.e1_claim_count}"
            )
        if self.replacement_claim_id is None and self.e1_claim_count != self.e0_claim_count - 1:
            raise ValueError(
                f"a deletion drops exactly one claim, but this record goes "
                f"{self.e0_claim_count} -> {self.e1_claim_count}"
            )
        return self

    @property
    def is_substitution(self) -> bool:
        """Whether a neutral claim took the omitted claim's place."""
        return self.replacement_claim_id is not None


class HiddenExperimentData(BaseModel):
    """Everything in a run that no agent may observe."""

    model_config = ConfigDict(extra="forbid")

    gold: HiddenGold
    omission: OmissionRecord | None = None


class OmissionResult(BaseModel):
    """Return type of :func:`build_omission_memo`.

    ``memo`` is the agent-facing artifact; ``provenance`` is experimenter-only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memo: AnalystMemo
    provenance: OmissionRecord


# --------------------------------------------------------------------------
# Deterministic omission construction (never uses an LLM)
# --------------------------------------------------------------------------


def build_omission_memo(
    memo: AnalystMemo,
    target_claim_id: str,
    *,
    expected_category: str | None = None,
    replacement_claim: MemoClaim | None = None,
) -> OmissionResult:
    """Build the E1 omission memo from an E0 memo by removing one claim.

    The invariant, whichever shape is used: **the named target claim is gone
    from the result**, and nothing else about the memo changes.

    Guarantees, all enforced below and covered by tests:

    * exactly one claim is removed -- the named target;
    * every other claim is carried over unchanged (same object, no rewrite);
    * claim order is preserved -- with a replacement, it occupies the target's
      own index, so the E1 order is the E0 order;
    * ``memo_id``, ``case_id`` are unchanged, so the resulting memo is
      indistinguishable from E0 apart from the one claim that changed;
    * the memo's field set is unchanged -- no omission marker is added;
    * no model of any kind is consulted.

    ``expected_category`` is an optional integrity guard: when given, the
    target claim must belong to that category, so a mis-specified target cannot
    silently delete the wrong clause.

    ``replacement_claim`` selects the *substitution* shape described on
    :class:`OmissionRecord`. When it is ``None`` the claim is deleted and the
    E1 memo is one claim shorter. When it is given, that claim takes the
    target's index and the two arms have the same claim count -- which is what
    a design that wants the omission not to be legible from the memo's length
    needs. The replacement must be a claim the memo does not already hold; this
    function does **not** check that it is off-policy, because "neutral" is a
    statement about the policy under test and the policy is not an argument
    here. :class:`~pilot01.experiment.cases.CaseSpec` makes that check, where
    the policy is available.

    Raises ``ValueError`` if the target claim is not found, if it appears more
    than once, if ``expected_category`` does not match, or if the replacement
    is already in the memo or is the target itself.
    """
    matches = [claim for claim in memo.claims if claim.claim_id == target_claim_id]
    if not matches:
        raise ValueError(
            f"target claim {target_claim_id!r} not present in memo {memo.memo_id!r}; "
            f"available claims: {memo.claim_ids()!r}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"target claim id {target_claim_id!r} is ambiguous in memo "
            f"{memo.memo_id!r} ({len(matches)} occurrences)"
        )

    target = matches[0]
    if expected_category is not None and target.category != expected_category:
        raise ValueError(
            f"target claim {target_claim_id!r} has category {target.category!r}, "
            f"expected {expected_category!r}"
        )

    target_index = memo.claim_ids().index(target_claim_id)

    if replacement_claim is None:
        remaining = tuple(claim for claim in memo.claims if claim.claim_id != target_claim_id)
        replacement_index = None
    else:
        if replacement_claim.claim_id == target_claim_id:
            raise ValueError(
                f"replacement claim {replacement_claim.claim_id!r} is the target "
                "claim; a replacement must be a different claim"
            )
        if memo.claim(replacement_claim.claim_id) is not None:
            raise ValueError(
                f"replacement claim {replacement_claim.claim_id!r} is already in memo "
                f"{memo.memo_id!r}; a substitution replaces the target with a claim "
                "the memo does not otherwise hold, so the claim count is unchanged "
                "and no other claim is disturbed"
            )
        remaining = (
            memo.claims[:target_index]
            + (replacement_claim,)
            + memo.claims[target_index + 1 :]
        )
        replacement_index = target_index

    omission_memo = AnalystMemo(
        memo_id=memo.memo_id,
        case_id=memo.case_id,
        claims=remaining,
    )
    provenance = OmissionRecord(
        source_memo_id=memo.memo_id,
        omitted_claim_id=target.claim_id,
        omitted_claim_category=target.category,
        omitted_claim_status=target.status,
        e0_claim_count=len(memo.claims),
        e1_claim_count=len(remaining),
        replacement_claim_id=None if replacement_claim is None else replacement_claim.claim_id,
        replacement_category=None if replacement_claim is None else replacement_claim.category,
        replacement_status=None if replacement_claim is None else replacement_claim.status,
        replacement_index=replacement_index,
    )
    return OmissionResult(memo=omission_memo, provenance=provenance)
