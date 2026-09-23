"""The source layer: contract text, the two read-only tools, and evidence provenance.

Four concerns, in dependency order:

:mod:`~pilot01.source.document`
    Turning one contract's text into deterministic, neutrally-identified
    paragraphs, with offsets back into the immutable original.

:mod:`~pilot01.source.index`
    A BM25 index over those paragraphs, built per contract. Lexical, in-memory,
    deterministic -- no embeddings and no vector store.

:mod:`~pilot01.source.tools`
    Exactly two read-only operations over that index, plus the append-only log
    of every attempt to use them.

:mod:`~pilot01.source.ledger`
    What each node actually observed, with upstream-cited evidence kept
    distinct from evidence the node opened itself.

:mod:`~pilot01.source.verification`
    The V0/V1 verdict, derived from the ledger rather than from what the model
    says about its own work.

:mod:`~pilot01.source.cuad`
    The one place contract text is read from disk: the canonical frozen CUAD
    snapshot, verified against the provenance record.

Nothing in this package reads a gold label, a target category, or the hidden
error condition, and nothing in it can put one into an agent's context.
"""

from __future__ import annotations

from .document import (
    PARAGRAPH_MAX_CHARS,
    PARAGRAPH_TARGET_CHARS,
    ContractDocument,
    SourceError,
    SourceParagraph,
    build_document,
    document_key,
    is_paragraph_id,
    sha256_text,
)
from .index import BM25Index, excerpt, tokenize
from .ledger import EvidenceAccess, EvidenceClass, EvidenceLedger
from .tools import (
    DEFAULT_TOP_K,
    OPEN_SOURCE_SPAN,
    SEARCH_CONTRACT,
    SOURCE_TOOL_NAMES,
    TOOL_ARGUMENTS,
    SearchHit,
    SourceLibrary,
    SourceSpan,
    SourceToolError,
    SourceTools,
    ToolCallLog,
    ToolCallRecord,
    ToolOutcome,
    ToolProtocolError,
    ToolRequest,
    evidence_access,
    replay_ledger,
)
from .verification import (
    VerificationFailure,
    VerificationLog,
    VerificationOutcome,
    evaluate_verification,
    unverifiable_outcome,
)

__all__ = [
    # document
    "PARAGRAPH_MAX_CHARS",
    "PARAGRAPH_TARGET_CHARS",
    "ContractDocument",
    "SourceError",
    "SourceParagraph",
    "build_document",
    "document_key",
    "is_paragraph_id",
    "sha256_text",
    # index
    "BM25Index",
    "excerpt",
    "tokenize",
    # ledger
    "EvidenceAccess",
    "EvidenceClass",
    "EvidenceLedger",
    # tools
    "DEFAULT_TOP_K",
    "OPEN_SOURCE_SPAN",
    "SEARCH_CONTRACT",
    "SOURCE_TOOL_NAMES",
    "TOOL_ARGUMENTS",
    "SearchHit",
    "SourceLibrary",
    "SourceSpan",
    "SourceToolError",
    "SourceTools",
    "ToolCallLog",
    "ToolCallRecord",
    "ToolOutcome",
    "ToolProtocolError",
    "ToolRequest",
    "evidence_access",
    "replay_ledger",
    # verification
    "VerificationFailure",
    "VerificationLog",
    "VerificationOutcome",
    "evaluate_verification",
    "unverifiable_outcome",
]
