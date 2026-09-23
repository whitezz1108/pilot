"""V0 vs V1: what it takes for a run's verification to count.

The point of this module is that ``"verification_basis": "self_checked"`` is a
*claim*, and a claim is not evidence. A model can emit that string without ever
having looked at the contract. Under V1 the runtime therefore does not read the
declaration as an answer -- it re-derives the basis from what the node
demonstrably did:

    the tool-call log  ->  the node's evidence ledger  ->  the cited ids
                       ->  the contract's own paragraph set  ->  a verdict

Nothing here consults the gold answer, the error condition, or the expected
decision. A node that verified a clause the analyst memo got *right* and a node
that verified one the memo omitted are judged by exactly the same code, so V1
can never move an answer toward gold -- it can only refuse to accept a claim
that was never backed.

The declared basis is not discarded, though. It is recorded beside the derived
one, and whether the two agree is itself a measurement: a node that reports
having checked the contract when the ledger shows it opened nothing is a
different finding from a node that honestly reports relying on the handoff, and
under ``v1`` there was no way to tell them apart.

V0 is not the absence of this check; it is the decision not to apply it. The
diagnostics are still computed and recorded, because "this node chose not to
verify" is a finding about the run, but no failure is raised for it.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..schemas import (
    AgentOutput,
    ClauseStatus,
    ExperimentalPolicy,
    VerificationBasis,
    VerificationStatus,
)
from .document import ContractDocument, is_paragraph_id
from .ledger import EvidenceClass, EvidenceLedger

__all__ = [
    "VerificationFailure",
    "VerificationOutcome",
    "VerificationLog",
    "evaluate_verification",
    "unverifiable_outcome",
    "derive_basis",
]

#: How a derived basis reads as a verdict. The mapping is total over
#: ``VerificationBasis``, so a node's status is a function of what it did.
_STATUS_FOR_BASIS: dict[VerificationBasis, VerificationStatus] = {
    VerificationBasis.SELF_CHECKED: VerificationStatus.VERIFIED,
    VerificationBasis.UPSTREAM_ONLY: VerificationStatus.NOT_CHECKED,
    VerificationBasis.UNAVAILABLE: VerificationStatus.UNVERIFIABLE,
}


class VerificationFailure(str, Enum):
    """Why a required verification did not count.

    Each value names a distinct way a claim can be unbacked, so a failure is
    diagnosable from the record alone rather than only from the transcript.
    """

    RULE_NOT_STATED = "rule_not_stated"
    """The node never named the policy rule it was verifying."""

    NO_TOOL_USE = "no_tool_use"
    """The node made no source-tool call at all."""

    TARGET_NOT_SEARCHED = "target_not_searched"
    """At least one policy target category had no category-named search."""

    NO_EVIDENCE_CITED = "no_evidence_cited"
    """The node reported no evidence ids of any kind."""

    EVIDENCE_NOT_OPENED = "evidence_not_opened"
    """No cited evidence was opened, or an id claimed as opened was not opened."""

    EVIDENCE_NOT_IN_CONTRACT = "evidence_not_in_contract"
    """A reported id is shaped like a paragraph id but is not in this contract."""

    EVIDENCE_NOT_OBSERVED = "evidence_not_observed"
    """A reported id is neither a paragraph of this contract nor upstream-cited."""

    BASIS_NOT_SELF_CHECKED = "basis_not_self_checked"
    """The node's declared ``verification_basis`` is not ``self_checked``.

    This is the V1 protocol requirement stated as a check: a run that demands
    verification is met only by a node that declares it looked at the contract
    itself. The declaration is not taken on trust -- it is compared against what
    the ledger recorded, and a disagreement is reported separately as
    :attr:`CHECK_INCOMPLETE`.
    """

    CHECK_INCOMPLETE = "check_incomplete"
    """The declared basis and the recorded provenance do not agree.

    A node that declares it checked the source while the ledger shows it opened
    nothing, or that declares it relied only on the handoff while the ledger
    shows it opened paragraphs, has produced a record that contradicts itself.
    Either way the record cannot be read as an account of what happened.
    """

    STATUS_NOT_VERIFIED = "status_not_verified"
    """Retained so that v1-era records still parse. Never raised by this code.

    ``v1`` asked the model for a ``verification_status`` field and failed the
    run when it did not say ``verified``. ``v2`` replaced that self-report with
    ``verification_basis``, and the corresponding failure is now
    :attr:`BASIS_NOT_SELF_CHECKED`. The member is kept, rather than deleted, so
    that a stored outcome from the development batch can still be read back.
    """

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
    """The *derived* verdict, computed from the ledger. Never the model's word."""

    target_clause_status: tuple[tuple[str, ClauseStatus], ...] = ()
    """The node's per-category assessment, as pairs so the record stays hashable."""

    opened_paragraph_ids: tuple[str, ...] = ()
    """Paragraph ids the node reported opening itself."""

    inherited_source_ids: tuple[str, ...] = ()
    """Source ids the node reported relying on without opening them."""

    declared_basis: VerificationBasis | None = None
    """What the node said it did, recorded beside the derived value."""

    derived_basis: VerificationBasis | None = None
    """What the ledger shows it did. This is the one the verdict is built from."""

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
        """Paragraph ids this node opened itself, as opposed to citing upstream."""
        return tuple(self.opened_paragraph_ids)

    @property
    def basis_agrees(self) -> bool:
        """Whether the node's account of its own verification matches the record.

        ``None`` is not a disagreement: a run with no basis to declare (the
        degenerate no-tools/no-verification cell) has nothing to agree about.
        """
        if self.declared_basis is None or self.derived_basis is None:
            return True
        return self.declared_basis is self.derived_basis

    def status_for(self, category: str) -> ClauseStatus | None:
        """This node's assessment of one target category, if it made one."""
        for name, status in self.target_clause_status:
            if name == category:
                return status
        return None

    def describe(self) -> str:
        if self.satisfied:
            return "verification satisfied" if self.required else "verification not required"
        return "verification failed: " + ", ".join(f.value for f in self.failures)


def derive_basis(*, ledger: EvidenceLedger, source_tools_available: bool) -> VerificationBasis:
    """What the ledger shows the node did, in the vocabulary the node reports in.

    Deliberately computed from the ledger and not from the output: this is the
    value the verdict rests on, and reading it off the node's own declaration
    would make the whole re-derivation circular.

    ``self_checked`` requires an *opened* paragraph, not a search. A search
    returns candidates; opening one is the step that turns a candidate into
    evidence, and the same distinction the V1 gate turns on applies here.
    """
    if not source_tools_available:
        return VerificationBasis.UNAVAILABLE
    if ledger.opened_ids:
        return VerificationBasis.SELF_CHECKED
    return VerificationBasis.UPSTREAM_ONLY


def unverifiable_outcome(note: str, *, node: str = "", invocation: int = 0) -> VerificationOutcome:
    """The outcome for a run that demands verification it cannot possibly do.

    Used for the degenerate governance combination in which verification is
    required but no source tools exist. It is reported as unverifiable rather
    than quietly passed, so that combination cannot masquerade as a satisfied
    gate.
    """
    return VerificationOutcome(
        status=VerificationStatus.UNVERIFIABLE,
        derived_basis=VerificationBasis.UNAVAILABLE,
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

    The conditions of the V1 gate, in the order they are checked:

    1. the policy rule under test is named;
    2. the node searched for every policy target category;
    3. it reported at least one id, and at least one of them was *opened* rather
       than merely returned by a search;
    4. every paragraph-shaped id it reported exists in this contract;
    5. every id it reported was observed by this node (opened, returned, or
       cited upstream to it);
    6. it declares ``self_checked`` -- the V1 protocol requirement;
    7. its declaration agrees with the ledger.

    Conditions 4 and 5 are the anti-fabrication pair: an id that was never
    returned by anything this node saw cannot be reported, however plausible it
    looks. Condition 3 is the anti-theatre rule: a search result is a list of
    candidates, and reading a candidate is the step that turns it into evidence.

    Conditions 6 and 7 are the v2 pair. 6 is the protocol requirement, stated
    against the vocabulary the prompt asks the model to answer in. 7 is the
    check that the answer is true: the declaration is set beside the basis the
    ledger derives, and a mismatch is a fault rather than a difference of
    opinion. Neither reads the declaration as evidence of anything but itself.

    The *status* on the returned outcome is always the derived one. The
    declaration is recorded beside it and never substituted for it.
    """
    provenance = output.evidence_provenance
    reported = tuple(provenance.opened_paragraph_ids) + tuple(
        provenance.inherited_source_ids
    )
    declared = provenance.verification_basis
    derived = derive_basis(ledger=ledger, source_tools_available=source_tools_available)
    observed = ledger.observed_ids
    diagnostics = [
        f"source_tools_available={source_tools_available}",
        f"tool_calls={len(ledger.searches) + len(ledger.opened_ids)}",
        f"searched={len(ledger.searches)}",
        f"opened={len(ledger.opened_ids)}",
        f"upstream_cited={len(ledger.upstream_ids)}",
        f"observed={len(observed)}",
        f"reported={len(reported)}",
        f"declared_basis={declared.value}",
        f"derived_basis={derived.value}",
    ]

    if verification_required and not source_tools_available:
        return unverifiable_outcome(
            "verification is required but this condition provides no source tools",
            node=node,
            invocation=invocation,
        )

    unopened = [e for e in reported if ledger.classify(e) is not EvidenceClass.SELF_OPENED]
    foreign = [
        e
        for e in reported
        if is_paragraph_id(e) and (document is None or document.paragraph(e) is None)
    ]
    unseen = [e for e in reported if e not in observed]
    checked = ledger.used_source_tools()
    def terms(value: str) -> tuple[str, ...]:
        return tuple(re.findall(r"[a-z0-9]+", value.casefold()))

    def names_category(query: str, category: str) -> bool:
        query_terms, category_terms = terms(query), terms(category)
        return bool(category_terms) and any(
            query_terms[start:start + len(category_terms)] == category_terms
            for start in range(len(query_terms) - len(category_terms) + 1)
        )

    unsearched_targets = tuple(
        category
        for category in policy.target_clause_categories
        if not any(names_category(query, category) for query in ledger.searches)
    )
    claimed_opened_but_not = tuple(
        paragraph_id
        for paragraph_id in provenance.opened_paragraph_ids
        if paragraph_id not in ledger.opened_ids
    )

    def outcome(**extra: object) -> VerificationOutcome:
        """Build the verdict from the diagnostics *as they stand at the call*.

        A closure rather than a dict built up front: the diagnostics are added
        to after the counts are computed, and a dict captured earlier would
        quietly drop every one of those additions -- which is exactly the kind
        of silent loss this module exists to prevent.
        """
        return VerificationOutcome(
            target_clause_status=tuple(
                (category, status) for category, status in output.target_clause_status.items()
            ),
            opened_paragraph_ids=tuple(provenance.opened_paragraph_ids),
            inherited_source_ids=tuple(provenance.inherited_source_ids),
            declared_basis=declared,
            derived_basis=derived,
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
            status=_STATUS_FOR_BASIS[derived],
            note="verification was optional in this condition",
        )

    failures: list[VerificationFailure] = []
    if not output.rule_id.strip() or output.rule_id.strip().upper() != policy.policy_id.upper():
        failures.append(VerificationFailure.RULE_NOT_STATED)
    if not checked:
        failures.append(VerificationFailure.NO_TOOL_USE)
    if unsearched_targets:
        diagnostics.append("targets_not_searched=" + ",".join(unsearched_targets))
        failures.append(VerificationFailure.TARGET_NOT_SEARCHED)
    if not reported:
        failures.append(VerificationFailure.NO_EVIDENCE_CITED)
    elif len(unopened) == len(reported) or claimed_opened_but_not:
        failures.append(VerificationFailure.EVIDENCE_NOT_OPENED)
    if foreign:
        failures.append(VerificationFailure.EVIDENCE_NOT_IN_CONTRACT)
    if unseen:
        failures.append(VerificationFailure.EVIDENCE_NOT_OBSERVED)
    if declared is not VerificationBasis.SELF_CHECKED:
        failures.append(VerificationFailure.BASIS_NOT_SELF_CHECKED)
    if declared is not derived:
        failures.append(VerificationFailure.CHECK_INCOMPLETE)

    return outcome(
        status=_STATUS_FOR_BASIS[derived],
        note="verification required and enforced",
        failures=tuple(failures),
    )
