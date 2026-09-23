"""What one node actually looked at.

The problem this module exists to prevent is a specific, easy-to-miss failure:
an agent writes ``"evidence_ids": ["3f9a1c0d2e4b:p0017"]`` having never opened
``3f9a1c0d2e4b:p0017`` -- it saw the id in a search result, or in the upstream
handoff, or it made it up -- and a naive pipeline records that as "verified
against source". A verification claim is only worth what the access ledger says
it is worth, so the ledger is kept per node, per invocation, and is the thing
verification is checked against.

Four classes of evidence are distinguished, and they are **not** collapsed into
each other, because later scoring needs the difference:

``SELF_OPENED``
    This node called ``open_source_span`` on the id and read the exact text.
    The only class that counts as verification.

``OBSERVED``
    The id appeared in a search result this node ran, but was never opened. The
    node has seen a snippet, not the clause.

``UPSTREAM_CITED``
    The id arrived in the material this node was *given* -- the analyst memo's
    cited source ids for the Manager, the Manager's ``evidence_ids`` for
    Compliance. Legitimate provenance, and exactly the thing the pilot measures
    the adoption of; it is not evidence this node gathered.

``UNKNOWN``
    None of the above. An id the node could not have obtained from anywhere it
    was permitted to look -- a fabrication, or an id from another contract.

Ledgers are per node. Compliance does **not** inherit the Manager's ledger: the
Manager's tool calls are recorded against the Manager and replayed against the
Manager, so "Compliance relied on evidence it never opened" stays visible
instead of being laundered through a shared history.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import Enum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EvidenceClass",
    "EvidenceAccess",
    "EvidenceLedger",
    "classify_access",
]


def classify_access(
    evidence_id: str,
    *,
    opened_ids: Sequence[str],
    search_result_ids: Sequence[str],
    upstream_ids: Sequence[str],
) -> EvidenceClass:
    """How a node holding these three sets came to hold ``evidence_id``.

    A module-level function because both the live ledger and its frozen snapshot
    answer this question, and two implementations of the same rule is exactly
    how a snapshot and the thing it snapshotted start disagreeing.

    Opening wins over seeing: an id that was both returned by a search and
    opened is ``SELF_OPENED``, because that is what the node actually did with
    it.
    """
    if evidence_id in opened_ids:
        return EvidenceClass.SELF_OPENED
    if evidence_id in search_result_ids:
        return EvidenceClass.OBSERVED
    if evidence_id in upstream_ids:
        return EvidenceClass.UPSTREAM_CITED
    return EvidenceClass.UNKNOWN


class EvidenceClass(str, Enum):
    """How a node came to hold an evidence id."""

    SELF_OPENED = "self_opened"
    OBSERVED = "observed"
    UPSTREAM_CITED = "upstream_cited"
    UNKNOWN = "unknown"


class EvidenceAccess(BaseModel):
    """A frozen snapshot of one node's evidence access. Sorted, so it is stable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    searches: tuple[str, ...] = ()
    search_result_ids: tuple[str, ...] = ()
    opened_ids: tuple[str, ...] = ()
    upstream_ids: tuple[str, ...] = ()

    @property
    def observed_ids(self) -> tuple[str, ...]:
        """Every id this node had access to, by any route.

        Includes ids that arrived through an upstream handoff. "Observed" is not
        a claim that the node gathered the id itself -- :meth:`classify` is what
        distinguishes those -- it is the set of ids the node could legitimately
        cite without inventing anything. Anything outside it was fabricated.
        """
        return tuple(sorted(set(self.search_result_ids) | set(self.opened_ids) | set(self.upstream_ids)))

    @property
    def used_source_tools(self) -> bool:
        """Whether the node consulted the source itself.

        Upstream-cited ids do not count: reading a handoff is not searching a
        contract, and collapsing the two would let a node satisfy a verification
        obligation it never discharged.
        """
        return bool(self.searches or self.opened_ids)

    def classify(self, evidence_id: str) -> EvidenceClass:
        """How this node came to hold ``evidence_id``. See :func:`classify_access`."""
        return classify_access(
            evidence_id,
            opened_ids=self.opened_ids,
            search_result_ids=self.search_result_ids,
            upstream_ids=self.upstream_ids,
        )


class EvidenceLedger:
    """The mutable, per-invocation accumulator behind :class:`EvidenceAccess`.

    Append-only in spirit: the record methods add, and nothing removes. The
    ledger is reset at the start of each invocation, never carried across nodes.
    """

    def __init__(self, node: str = "") -> None:
        self._node = node
        self._searches: list[str] = []
        self._search_result_ids: list[str] = []
        self._opened_ids: list[str] = []
        self._upstream_ids: list[str] = []

    @property
    def node(self) -> str:
        return self._node

    # -- recording ---------------------------------------------------------

    def record_search(self, query: str, paragraph_ids: Sequence[str]) -> None:
        """Record a search this node ran, and the ids it returned."""
        self._searches.append(query)
        for paragraph_id in paragraph_ids:
            if paragraph_id not in self._search_result_ids:
                self._search_result_ids.append(paragraph_id)

    def record_open(self, paragraph_id: str) -> None:
        """Record that this node opened one paragraph and read its exact text."""
        if paragraph_id not in self._opened_ids:
            self._opened_ids.append(paragraph_id)

    def record_upstream(self, evidence_ids: Iterable[str]) -> None:
        """Record ids this node was *given* rather than gathered itself."""
        for evidence_id in evidence_ids:
            if evidence_id not in self._upstream_ids:
                self._upstream_ids.append(evidence_id)

    # -- inspection --------------------------------------------------------

    @property
    def searches(self) -> tuple[str, ...]:
        return tuple(self._searches)

    @property
    def search_result_ids(self) -> tuple[str, ...]:
        return tuple(self._search_result_ids)

    @property
    def opened_ids(self) -> tuple[str, ...]:
        return tuple(self._opened_ids)

    @property
    def upstream_ids(self) -> tuple[str, ...]:
        return tuple(self._upstream_ids)

    @property
    def observed_ids(self) -> tuple[str, ...]:
        """Every id this node had access to, by any route. See the ledger's note."""
        return tuple(
            sorted(set(self._search_result_ids) | set(self._opened_ids) | set(self._upstream_ids))
        )

    def used_source_tools(self) -> bool:
        """Whether the node consulted the source itself, as opposed to citing upstream."""
        return bool(self._searches or self._opened_ids)

    def classify(self, evidence_id: str) -> EvidenceClass:
        """How this node came to hold ``evidence_id``.

        Opening wins over seeing: an id that was both returned by a search and
        opened is ``SELF_OPENED``, because that is what the node actually did
        with it.
        """
        return classify_access(
            evidence_id,
            opened_ids=self._opened_ids,
            search_result_ids=self._search_result_ids,
            upstream_ids=self._upstream_ids,
        )

    def snapshot(self) -> EvidenceAccess:
        return EvidenceAccess(
            node=self._node,
            searches=tuple(self._searches),
            search_result_ids=tuple(self._search_result_ids),
            opened_ids=tuple(self._opened_ids),
            upstream_ids=tuple(self._upstream_ids),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"EvidenceLedger(node={self._node!r}, searches={len(self._searches)}, "
            f"opened={len(self._opened_ids)}, upstream={len(self._upstream_ids)})"
        )
