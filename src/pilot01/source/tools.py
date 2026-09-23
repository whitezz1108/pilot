"""The source-tool layer: exactly two read-only operations, and their log.

Two tools. Not three, and nothing that writes:

``search_contract(query)``
    Ranked passages from the contract this run is about, each with a neutral
    paragraph id and a bounded excerpt.

``open_source_span(paragraph_id)``
    The exact text of one paragraph, with its offsets into the immutable source.

Nothing either one returns carries a CUAD category label, a target-category
name, a gold answer offset, an expected decision, an arm label, or a condition
id. That is not a convention here, it is the shape of the types: the tool
output models have no field one could be put in, and the index they read from is
built from paragraph text alone.

**One contract at a time.** A :class:`SourceLibrary` may hold many contracts,
but a node is bound to exactly one for the duration of an invocation, and its
index is built from that one document. There is no global index, so there is no
code path by which a search could return a passage from another contract.

**Availability is a runtime decision, not a build-time one.** The same
:class:`SourceTools` object serves every governance condition; ``available`` is
set from the run's own flags at the start of each invocation. In A0 the tool
surface is empty -- the model is offered nothing, and an attempt to invoke a
tool is refused, recorded, and returned to the model as a failed tool call
rather than crashing the run.

**Every call is logged.** Successful, refused and malformed alike, with the
model-call index it belongs to, the tool name, the arguments, the result ids and
the timestamps. The log is the record; a ledger can be replayed from it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .document import ContractDocument, SourceError, build_document, is_paragraph_id
from .index import BM25Index, EXCERPT_CHARS, excerpt, tokenize
from .ledger import EvidenceAccess, EvidenceLedger

__all__ = [
    "SEARCH_CONTRACT",
    "OPEN_SOURCE_SPAN",
    "SOURCE_TOOL_NAMES",
    "TOOL_ARGUMENTS",
    "DEFAULT_TOP_K",
    "SourceToolError",
    "ToolProtocolError",
    "SearchHit",
    "SourceSpan",
    "ToolRequest",
    "ToolOutcome",
    "ToolCallRecord",
    "ToolCallLog",
    "SourceLibrary",
    "SourceTools",
    "replay_ledger",
]

SEARCH_CONTRACT = "search_contract"
OPEN_SOURCE_SPAN = "open_source_span"

SOURCE_TOOL_NAMES: tuple[str, ...] = (SEARCH_CONTRACT, OPEN_SOURCE_SPAN)
"""The complete tool surface. There is no third tool and no way to add one."""

TOOL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    SEARCH_CONTRACT: ("query",),
    OPEN_SOURCE_SPAN: ("paragraph_id",),
}
"""The exact argument set each tool accepts -- no more, no fewer."""

DEFAULT_TOP_K = 5


class SourceToolError(RuntimeError):
    """A source tool could not answer the request it was given.

    Recoverable by design: the caller turns it into a failed tool result that
    the model sees, rather than aborting the run. A model that asks for a
    paragraph that does not exist should be told so, not have its run killed.
    """


class ToolProtocolError(SourceToolError):
    """A tool *request* was malformed, before any tool was reached.

    Carries whatever could be salvaged of the request so the refusal can still
    be logged against a tool name and its arguments.
    """

    def __init__(self, message: str, *, tool: str = "", arguments: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.tool = tool
        self.arguments = dict(arguments or {})


class SearchHit(BaseModel):
    """One ranked passage. Neutral: an id, a contract, a score, a snippet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paragraph_id: str
    contract_id: str
    score: float
    excerpt: str


class SourceSpan(BaseModel):
    """One opened paragraph: the exact source text and where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    paragraph_id: str
    contract_id: str
    text: str
    start_char: int
    end_char: int


class ToolRequest(BaseModel):
    """A tool call a model asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "ToolRequest":
        """Validate the ``tool_request`` envelope's body.

        Raises :class:`ToolProtocolError` for anything that is not an object
        with a non-empty string ``tool`` and an object ``arguments``.
        """
        if not isinstance(payload, dict):
            raise ToolProtocolError(
                f"a tool request must be an object, got {type(payload).__name__}"
            )
        unexpected = set(payload) - {"tool", "arguments"}
        if unexpected:
            raise ToolProtocolError(
                f"a tool request carries only 'tool' and 'arguments'; got "
                f"{sorted(unexpected)} as well",
                tool=str(payload.get("tool", "")),
                arguments=payload.get("arguments") if isinstance(payload.get("arguments"), dict) else None,
            )
        tool = payload.get("tool")
        arguments = payload.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(tool, str) or not tool.strip():
            raise ToolProtocolError(
                f"a tool request needs a non-empty 'tool' string, got {tool!r}",
                arguments=arguments if isinstance(arguments, dict) else None,
            )
        if not isinstance(arguments, dict):
            raise ToolProtocolError(
                f"'arguments' must be an object, got {type(arguments).__name__}",
                tool=tool.strip(),
            )
        return cls(tool=tool.strip(), arguments=dict(arguments))


class ToolOutcome(BaseModel):
    """What a tool call produced -- including the refusals.

    Every attempt gets one of these, so a run's tool history is complete rather
    than only containing the calls that worked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    result_ids: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ToolCallRecord(BaseModel):
    """One tool call, in full. Private, like the raw model-call log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=0, description="Dense, monotonic, per-log sequence.")
    call_index: int = Field(ge=0, description="The model call this tool call answers.")
    run_id: str | None = None
    node: str
    invocation: int = Field(ge=0)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    available: bool
    """Whether source tools were available to this node at the time."""
    ok: bool
    result_ids: tuple[str, ...] = ()
    error: str | None = None
    requested_at: datetime | None = None
    responded_at: datetime | None = None

    def to_jsonl(self) -> str:
        return self.model_dump_json() + "\n"


class ToolCallLog:
    """An append-only sink for :class:`ToolCallRecord`.

    Deliberately a different type from :class:`~pilot01.model.log.ModelCallLog`:
    a tool call is not a model call, and merging them would make "how many model
    turns did this agent take" ambiguous.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._records: list[ToolCallRecord] = []
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("", encoding="utf-8")

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, record: ToolCallRecord) -> ToolCallRecord:
        """Validate and append one record. The only mutating operation."""
        from ..model.log import assert_no_secrets

        assert_no_secrets(record)
        self._records.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_jsonl())
        return record

    @property
    def records(self) -> tuple[ToolCallRecord, ...]:
        return tuple(self._records)

    def of_node(self, node: str) -> tuple[ToolCallRecord, ...]:
        return tuple(record for record in self._records if record.node == node)

    def of_tool(self, tool: str) -> tuple[ToolCallRecord, ...]:
        return tuple(record for record in self._records if record.tool == tool)

    def failures(self) -> tuple[ToolCallRecord, ...]:
        return tuple(record for record in self._records if not record.ok)

    def __len__(self) -> int:
        return len(self._records)

    def to_jsonl(self) -> str:
        return "".join(record.to_jsonl() for record in self._records)


class SourceLibrary:
    """The contracts a run may consult, keyed by contract id.

    A library is not an agent-facing object: it is the wiring between "which
    case is this run about" and "which contract text does that case have". A
    node resolves exactly one document from it per invocation.
    """

    def __init__(self, documents: Mapping[str, ContractDocument]) -> None:
        self._documents = dict(documents)

    @classmethod
    def from_texts(cls, texts: Mapping[str, str], **kwargs: Any) -> "SourceLibrary":
        """Build a library by paragraphizing each contract's text."""
        return cls(
            {contract_id: build_document(contract_id, text, **kwargs) for contract_id, text in texts.items()}
        )

    @classmethod
    def empty(cls) -> "SourceLibrary":
        return cls({})

    def get(self, contract_id: str) -> ContractDocument:
        try:
            return self._documents[contract_id]
        except KeyError:
            raise SourceToolError(
                f"no contract source is loaded for contract {contract_id!r}; "
                f"the library holds {sorted(self._documents)}"
            ) from None

    def __contains__(self, contract_id: object) -> bool:
        return contract_id in self._documents

    def __len__(self) -> int:
        return len(self._documents)

    @property
    def contract_ids(self) -> tuple[str, ...]:
        return tuple(self._documents)


class SourceTools:
    """The two read-only source tools, for one agent node, in one run."""

    def __init__(
        self,
        *,
        node: str,
        library: SourceLibrary,
        log: ToolCallLog | None = None,
        top_k: int = DEFAULT_TOP_K,
        excerpt_chars: int = EXCERPT_CHARS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._node = node
        self._library = library
        self._log = log if log is not None else ToolCallLog()
        self._top_k = top_k
        self._excerpt_chars = excerpt_chars
        self._clock = clock
        self._ledger = EvidenceLedger(node=node)
        self._contract_id: str | None = None
        self._document: ContractDocument | None = None
        self._index: BM25Index | None = None
        self._available = False
        #: The model call a tool call answers. Set per invocation, overridden by
        #: :meth:`execute` while it dispatches, so that a tool call made directly
        #: and one made through a model's request are logged the same way.
        self._call_index = 0
        self._invocation = 0
        self._run_id: str | None = None

    # -- inspection --------------------------------------------------------

    @property
    def node(self) -> str:
        return self._node

    @property
    def ledger(self) -> EvidenceLedger:
        return self._ledger

    @property
    def log(self) -> ToolCallLog:
        return self._log

    @property
    def available(self) -> bool:
        return self._available

    @property
    def available_names(self) -> tuple[str, ...]:
        """The tool surface this node currently offers. Empty when unavailable."""
        return SOURCE_TOOL_NAMES if self._available else ()

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The tools this layer can provide at all, regardless of availability.

        Distinct from :attr:`available_names` on purpose. A prompt is built
        before an invocation is bound, and it needs to know what the *condition*
        permits; the runtime needs to know what is available *now*. Deriving the
        advertised surface from this and gating it on the view's own permission
        flag keeps the two consistent without letting a prompt read live state.
        """
        return SOURCE_TOOL_NAMES

    @property
    def document(self) -> ContractDocument | None:
        return self._document

    # -- lifecycle ---------------------------------------------------------

    def begin_invocation(
        self,
        *,
        contract_id: str | None,
        available: bool,
        call_index: int = 0,
        invocation: int = 0,
        run_id: str | None = None,
    ) -> None:
        """Bind the tools to one contract for one invocation, or empty them.

        Called by the node immediately before it runs. Resetting the ledger here
        is what keeps two invocations of the same node from sharing an evidence
        history, and what keeps two nodes from sharing one at all -- each node
        owns its own :class:`SourceTools`.
        """
        self._ledger = EvidenceLedger(node=self._node)
        self._available = bool(available)
        self._contract_id = contract_id
        self._document = None
        self._index = None
        self._call_index = call_index
        self._invocation = invocation
        self._run_id = run_id
        if self._available and contract_id is not None:
            self._document = self._library.get(contract_id)
            self._index = BM25Index(self._document.paragraphs)

    # -- the two tools -----------------------------------------------------

    def search_contract(self, query: str) -> tuple[SearchHit, ...]:
        """Rank this contract's paragraphs against ``query``."""
        outcome, hits = self._invoke(SEARCH_CONTRACT, {"query": query})
        if not outcome.ok:
            raise SourceToolError(outcome.error or "search_contract failed")
        return hits

    def open_source_span(self, paragraph_id: str) -> SourceSpan:
        """Return the exact text of one paragraph of this contract.

        Three refusals, and they are kept distinct on purpose: a malformed id, a
        well-formed id that is not in this contract (which is what a foreign
        contract's id looks like), and an id for a paragraph that does not
        exist. ``document`` is the sole authority on all three.
        """
        outcome, span = self._invoke(OPEN_SOURCE_SPAN, {"paragraph_id": paragraph_id})
        if not outcome.ok:
            raise SourceToolError(outcome.error or "open_source_span failed")
        return span

    # -- dispatch ----------------------------------------------------------

    def execute(
        self,
        request: ToolRequest,
        *,
        call_index: int,
        invocation: int,
        run_id: str | None = None,
    ) -> ToolOutcome:
        """Run one validated tool request and log the outcome.

        The request's tool name and arguments are checked against
        :data:`TOOL_ARGUMENTS` here rather than trusted, so a model that invents
        a third tool or passes an argument nobody defined gets a refusal it can
        read and act on, not a traceback.
        """
        previous = (self._call_index, self._invocation, self._run_id)
        self._call_index, self._invocation, self._run_id = call_index, invocation, run_id
        try:
            outcome, _ = self._invoke(request.tool, dict(request.arguments))
            return outcome
        finally:
            self._call_index, self._invocation, self._run_id = previous

    def reject(
        self,
        *,
        tool: str,
        arguments: Mapping[str, Any] | None,
        error: str,
        call_index: int,
        invocation: int,
        run_id: str | None = None,
    ) -> ToolOutcome:
        """Log a request that never reached a tool, because its envelope was malformed."""
        previous = (self._call_index, self._invocation, self._run_id)
        self._call_index, self._invocation, self._run_id = call_index, invocation, run_id
        try:
            return self._emit(
                ToolOutcome(tool=tool, arguments=dict(arguments or {}), ok=False, error=error)
            )
        finally:
            self._call_index, self._invocation, self._run_id = previous

    # -- internals ---------------------------------------------------------

    def _require_source(self) -> tuple[ContractDocument, BM25Index]:
        if not self._available:
            raise SourceToolError(
                "no source tools are available in this run; the contract cannot be "
                "searched or opened"
            )
        if self._document is None or self._index is None:
            raise SourceToolError(
                f"no contract source is loaded for contract {self._contract_id!r}"
            )
        return self._document, self._index

    def _invoke(self, tool: str, arguments: dict[str, Any]) -> tuple[ToolOutcome, Any]:
        """Check, run, and log one tool call. The single logging point.

        Both entry points -- a direct call to :meth:`search_contract` and a
        dispatched model request -- come through here, so "every search and open
        is logged" holds for every caller rather than only for the model's.
        """
        domain: Any = None
        try:
            if not self._available:
                raise SourceToolError(
                    "no source tools are available in this run; the contract cannot be "
                    "searched or opened"
                )
            if tool not in TOOL_ARGUMENTS:
                raise SourceToolError(
                    f"{tool!r} is not a tool this run provides; the available tools are "
                    f"{list(SOURCE_TOOL_NAMES)}"
                )
            expected = TOOL_ARGUMENTS[tool]
            if set(arguments) != set(expected):
                raise SourceToolError(
                    f"{tool} takes exactly the argument(s) {list(expected)}; got "
                    f"{sorted(arguments)}"
                )
            if tool == SEARCH_CONTRACT:
                domain = self._search(arguments["query"])
                outcome = ToolOutcome(
                    tool=tool,
                    arguments=arguments,
                    ok=True,
                    result_ids=tuple(hit.paragraph_id for hit in domain),
                    payload={"results": [hit.model_dump(mode="json") for hit in domain]},
                )
            else:
                domain = self._open(arguments["paragraph_id"])
                outcome = ToolOutcome(
                    tool=tool,
                    arguments=arguments,
                    ok=True,
                    result_ids=(domain.paragraph_id,),
                    payload=domain.model_dump(mode="json"),
                )
        except SourceToolError as exc:
            outcome = ToolOutcome(tool=tool, arguments=arguments, ok=False, error=str(exc))
        return self._emit(outcome), domain

    def _search(self, query: str) -> tuple[SearchHit, ...]:
        document, index = self._require_source()
        if not isinstance(query, str) or not query.strip():
            raise SourceToolError("search_contract needs a non-empty 'query' string")
        terms = tokenize(query)
        hits = index.search(query, top_k=self._top_k)
        results = tuple(
            SearchHit(
                paragraph_id=paragraph.paragraph_id,
                contract_id=paragraph.contract_id,
                score=round(score, 6),
                excerpt=excerpt(paragraph.text, terms, self._excerpt_chars),
            )
            for paragraph, score in hits
        )
        self._ledger.record_search(query, [hit.paragraph_id for hit in results])
        return results

    def _open(self, paragraph_id: str) -> SourceSpan:
        document, _ = self._require_source()
        if not isinstance(paragraph_id, str) or not paragraph_id.strip():
            raise SourceToolError("open_source_span needs a non-empty 'paragraph_id' string")
        paragraph_id = paragraph_id.strip()
        paragraph = document.paragraph(paragraph_id)
        if paragraph is None:
            if is_paragraph_id(paragraph_id):
                raise SourceToolError(
                    f"{paragraph_id!r} is not a paragraph of contract "
                    f"{document.contract_id!r}; only ids that search_contract returned "
                    "for this contract can be opened"
                )
            raise SourceToolError(
                f"{paragraph_id!r} is not a well-formed paragraph id; ids look like "
                "'0123456789ab:p0007'"
            )
        self._ledger.record_open(paragraph_id)
        return SourceSpan(
            paragraph_id=paragraph.paragraph_id,
            contract_id=paragraph.contract_id,
            text=paragraph.text,
            start_char=paragraph.start_char,
            end_char=paragraph.end_char,
        )

    def _emit(self, outcome: ToolOutcome) -> ToolOutcome:
        now = self._clock() if self._clock is not None else None
        self._log.append(
            ToolCallRecord(
                sequence=len(self._log),
                call_index=self._call_index,
                run_id=self._run_id,
                node=self._node,
                invocation=self._invocation,
                tool=outcome.tool,
                arguments=dict(outcome.arguments),
                available=self._available,
                ok=outcome.ok,
                result_ids=outcome.result_ids,
                error=outcome.error,
                requested_at=now,
                responded_at=now,
            )
        )
        return outcome


def replay_ledger(
    records: Iterable[ToolCallRecord],
    *,
    node: str | None = None,
    upstream: Iterable[str] = (),
) -> EvidenceLedger:
    """Rebuild an :class:`~pilot01.source.ledger.EvidenceLedger` from tool records.

    The runtime ledger is a convenience; the tool-call log is the record.
    Anything the run concluded about a node's evidence access can be recomputed
    from the log alone, which is what makes the conclusion auditable after the
    fact rather than only during the run.

    ``node`` filters to one node's calls. Passing ``None`` replays every record
    given, which is right only for a log that holds one node's calls.

    ``upstream`` is the evidence the node was *handed* -- the memo's cited source
    ids for the Manager, the handoff's
    ``evidence_provenance.inherited_source_ids`` for Compliance. It is an
    argument rather than something read from the log because it is not a tool
    event: no tool was called to obtain it. A caller replaying a whole run
    supplies it from the artifact the node was given, which is exactly where the
    live ledger got it. Omitting it yields a ledger whose ``upstream_ids`` is
    empty, which understates access rather than inventing it -- the safe
    direction for an audit, and the one that makes a missing argument visible as
    an unexplained verification failure rather than a silent pass.
    """
    ledger = EvidenceLedger(node=node or "")
    ledger.record_upstream(upstream)
    for record in records:
        if node is not None and record.node != node:
            continue
        if not record.ok:
            continue
        if record.tool == OPEN_SOURCE_SPAN:
            for paragraph_id in record.result_ids:
                ledger.record_open(paragraph_id)
        elif record.tool == SEARCH_CONTRACT:
            ledger.record_search(str(record.arguments.get("query", "")), record.result_ids)
    return ledger


def evidence_access(
    records: Iterable[ToolCallRecord], *, node: str, upstream: Iterable[str] = ()
) -> EvidenceAccess:
    """The frozen access snapshot for one node, derived from the tool log."""
    return replay_ledger(records, node=node, upstream=upstream).snapshot()


__all__ += ["evidence_access", "SourceError"]
