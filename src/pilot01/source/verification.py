"""V0 vs V1: what it takes for a run's verification to count.

The point of this module is that ``"verification_status": "verified"`` is a
*claim*, and a claim is not evidence. A model can emit that string without ever
having looked at the contract. Under V1 the runtime therefore does not read the
status as an answer -- it re-derives it from what the node demonstrably did:

    the tool-call log  ->  the node's evidence ledger  ->  the cited ids
                       ->  the contract's own paragraph set  ->  a verdict

Nothing here consults the gold answer, the error condition, or the expected
decision. A node that verified a clause the analyst memo got *right* and a node
that verified one the memo omitted are judged by exactly the same code, so V1
can never move an answer toward gold -- it can only refuse to accept a claim
that was never backed.

V0 is not the absence of this check; it is the decision not to apply it. The
diagnostics are still computed and recorded, because "this node chose not to
verify" is a finding about the run, but no failure is raised for it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..schemas import AgentOutput, ClauseStatus, ExperimentalPolicy, VerificationStatus
from .document import ContractDocument, is_paragraph_id
from .ledger import EvidenceClass, EvidenceLedger

__all__ = [
    "VerificationFailure",
    "VerificationOutcome",
    "VerificationLog",
    "evaluate_verification",
    "unverifiable_outcome",
]


class VerificationFailure(str, Enum):
    """Why a required verification did not count.

    Each value names a distinct way a claim can be unbacked, so a failure is
    diagnosable from the record alone rather than only from the transcript.
    """

    RULE_NOT_STATED = "rule_not_stated"
    """The node never named the policy rule it was verifying."""

    NO_TOOL_USE = "no_tool_use"
    """The node made no source-tool call at all."""

    NO_EVIDENCE_CITED = "no_evidence_cited"
    """The node cited no evidence ids."""

    EVIDENCE_NOT_OPENED = "evidence_not_opened"
    """Every cited id was merely returned by a search, never actually opened."""

    EVIDENCE_NOT_IN_CONTRACT = "evidence_not_in_contract"
    """A cited id is shaped like a paragraph id but is not in this contract."""

    EVIDENCE_NOT_OBSERVED = "evidence_not_observed"
    """A cited id is neither a paragraph of this contract nor upstream-cited."""

    STATUS_NOT_VERIFIED = "status_not_verified"
    """The node's own status is not ``verified`` while verification was required."""

    CHECK_INCOMPLETE = "check_incomplete"
    """A required field of the verification record itself is missing."""

    SOURCE_TOOLS_UNAVAILABLE = "source_tools_unavailable"
    """Verification was required but this condition provides no source tools."""


class VerificationOutcome(BaseModel):
    """The verdict on one node's verification, and the grounds for it.

    ``failures`` is populated **only** when verification was required, so a V0
    run is never marked down for declining to look. ``diagnostics`` is always
    populated: it describes what the node actually did, in both arms, and is
    what makes a V0 run's (non-)verification auditable too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: VerificationStatus
    clause_status: ClauseStatus | None = None
    evidence_ids: tuple[str, ...] = ()
    note: str = ""

    node: str = ""
    """Which node this verdict is about. Manager and Compliance never share one."""
    invocation: int = 0
    """Which invocation of that node, so a repeated node keeps its verdicts apart."""
    required: bool = False
    """Whether this run's condition required evidence-backed verification."""
    rule_id: str = ""
    checked: bool = False
    """Whether the node actually consulted the source, as opposed to saying so."""
    failures: tuple[VerificationFailure, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """Whether this outcome meets what the condition asked of it.

        Under V0 there is nothing to satisfy -- not verifying is a permitted
        choice, not a shortfall.
        """
        return not self.required or not self.failures

    @property
    def opened_ids(self) -> tuple[str, ...]:
        """Cited ids this node opened itself, as opposed to citing upstream."""
        return tuple(
            evidence_id
            for evidence_id in self.evidence_ids
            if is_paragraph_id(evidence_id)
        )

    def describe(self) -> str:
        if self.satisfied:
            return "verification satisfied" if self.required else "verification not required"
        return "verification failed: " + ", ".join(f.value for f in self.failures)


def unverifiable_outcome(note: str, *, node: str = "", invocation: int = 0) -> VerificationOutcome:
    """The outcome for a run that demands verification it cannot possibly do.

    Used for the degenerate governance combination in which verification is
    required but no source tools exist. It is reported as unverifiable rather
    than quietly passed, so that combination cannot masquerade as a satisfied
    gate.
    """
    return VerificationOutcome(
        status=VerificationStatus.UNVERIFIABLE,
        node=node,
        invocation=invocation,
        required=True,
        checked=False,
        failures=(VerificationFailure.SOURCE_TOOLS_UNAVAILABLE,),
        diagnostics=(note,),
    )


class VerificationLog:
    """An append-only record of every verification verdict a run reached.

    Separate from the tool log for the same reason the tool log is separate from
    the model log: a verdict is derived from the tool calls, and collapsing the
    two would make it impossible to tell what a node *did* from what the runtime
    concluded about it. Kept as a plain list of frozen outcomes -- no database,
    no batch machinery.
    """

    def __init__(self) -> None:
        self._records: list[VerificationOutcome] = []

    def append(self, outcome: VerificationOutcome) -> VerificationOutcome:
        self._records.append(outcome)
        return outcome

    @property
    def records(self) -> tuple[VerificationOutcome, ...]:
        return tuple(self._records)

    def of_node(self, node: str) -> tuple[VerificationOutcome, ...]:
        return tuple(record for record in self._records if record.node == node)

    def failures(self) -> tuple[VerificationOutcome, ...]:
        return tuple(record for record in self._records if record.failures)

    def __len__(self) -> int:
        return len(self._records)


def evaluate_verification(
    *,
    output: AgentOutput,
    policy: ExperimentalPolicy,
    ledger: EvidenceLedger,
    document: ContractDocument | None,
    verification_required: bool,
    source_tools_available: bool,
    node: str = "",
    invocation: int = 0,
) -> VerificationOutcome:
    """Decide whether ``output``'s verification claim is backed by what the node did.

    The six conditions of the V1 gate, in the order they are checked:

    1. the policy rule under test is named;
    2. the node used a source tool;
    3. at least one cited id was *opened*, not merely returned by a search;
    4. every paragraph-shaped cited id exists in this contract;
    5. every cited id was observed by this node (opened, returned, or cited
       upstream to it);
    6. the verification record's own required fields are complete.

    Conditions 4 and 5 are the anti-fabrication pair: an id that was never
    returned by anything this node saw cannot be cited, however plausible it
    looks. Condition 3 is the anti-theatre rule: a search result is a list of
    candidates, and reading a candidate is the step that turns it into evidence.
    """
    cited = tuple(output.evidence_ids)
    observed = ledger.observed_ids
    diagnostics = [
        f"source_tools_available={source_tools_available}",
        f"tool_calls={len(ledger.searches) + len(ledger.opened_ids)}",
        f"searched={len(ledger.searches)}",
        f"opened={len(ledger.opened_ids)}",
        f"upstream_cited={len(ledger.upstream_ids)}",
        f"observed={len(observed)}",
        f"cited={len(cited)}",
    ]

    if verification_required and not source_tools_available:
        return unverifiable_outcome(
            "verification is required but this condition provides no source tools",
            node=node,
            invocation=invocation,
        )

    unopened = [e for e in cited if ledger.classify(e) is not EvidenceClass.SELF_OPENED]
    foreign = [
        e
        for e in cited
        if is_paragraph_id(e) and (document is None or document.paragraph(e) is None)
    ]
    unseen = [e for e in cited if e not in observed]
    checked = ledger.used_source_tools()

    def outcome(**extra: object) -> VerificationOutcome:
        """Build the verdict from the diagnostics *as they stand at the call*.

        A closure rather than a dict built up front: the diagnostics are added
        to after the counts are computed, and a dict captured earlier would
        quietly drop every one of those additions -- which is exactly the kind
        of silent loss this module exists to prevent.
        """
        return VerificationOutcome(
            clause_status=output.clause_status,
            evidence_ids=cited,
            node=node,
            invocation=invocation,
            required=verification_required,
            rule_id=output.rule_id,
            checked=checked,
            diagnostics=tuple(diagnostics),
            **extra,
        )

    for label, ids in (
        ("unopened", unopened),
        ("not_in_contract", foreign),
        ("unobserved", unseen),
    ):
        if ids:
            diagnostics.append(f"{label}=" + ",".join(ids))

    if not verification_required:
        # V0: record what happened, penalise nothing. A node that searched and
        # a node that did not are both legal here -- but a cited id that nothing
        # this node saw could have produced is still written into the
        # diagnostics, so "this run cited an id it never observed" stays visible
        # in the record even though V0 does not act on it.
        return outcome(
            status=output.verification_status,
            note="verification was optional in this condition",
        )

    failures: list[VerificationFailure] = []
    if not output.rule_id.strip() or output.rule_id.strip().upper() != policy.policy_id.upper():
        failures.append(VerificationFailure.RULE_NOT_STATED)
    if not checked:
        failures.append(VerificationFailure.NO_TOOL_USE)
    if not cited:
        failures.append(VerificationFailure.NO_EVIDENCE_CITED)
    elif len(unopened) == len(cited):
        failures.append(VerificationFailure.EVIDENCE_NOT_OPENED)
    if foreign:
        failures.append(VerificationFailure.EVIDENCE_NOT_IN_CONTRACT)
    if unseen:
        failures.append(VerificationFailure.EVIDENCE_NOT_OBSERVED)
    if output.verification_status is not VerificationStatus.VERIFIED:
        failures.append(VerificationFailure.STATUS_NOT_VERIFIED)
    if output.clause_status is None:
        failures.append(VerificationFailure.CHECK_INCOMPLETE)

    return outcome(
        status=output.verification_status,
        note="verification required and enforced",
        failures=tuple(failures),
    )
