"""Contract text to neutral, deterministic source paragraphs.

This is the bottom of the retrieval stack, and it decides the one thing every
later layer depends on: **what a "paragraph" is, and what it is called**.

Two properties matter more than the exact chunking rule.

**Determinism.** :func:`build_document` is a pure function of the contract text
and the two size parameters. The same text always yields the same paragraphs, in
the same order, with the same ids. Nothing here consults a clock, a random
source, the file system, or the run.

**Neutrality.** A paragraph id is

    ``<12 hex chars of sha256(contract text)>:p<4-digit ordinal>``

which names the contract and the paragraph's position in it, and nothing else.
It does not encode the CUAD clause category, whether the paragraph is a positive
or negative example, where the gold answer sits, which experimental arm the run
is on, or what decision is expected. A model that sees ``3f9a1c0d2e4b:p0017``
learns nothing about the experiment from the identifier, which is what makes it
safe to hand the id to an agent.

The content hash in the id is also what makes cross-contract access structurally
impossible rather than merely discouraged: an id minted for one contract cannot
match a paragraph of another, so ``open_source_span`` rejects it without needing
to consult any register of "foreign" ids.

Offsets into the original, immutable contract text are preserved
(``start_char``/``end_char``), so a paragraph can always be traced back to the
exact span of the frozen CUAD context it came from. The contract text itself is
never modified: it is read, sliced, and left alone.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SourceError",
    "PARAGRAPH_ID_RE",
    "PARAGRAPH_TARGET_CHARS",
    "PARAGRAPH_MAX_CHARS",
    "sha256_text",
    "document_key",
    "is_paragraph_id",
    "SourceParagraph",
    "ContractDocument",
    "build_document",
]

PARAGRAPH_TARGET_CHARS = 1200
"""Aim for roughly this much text per paragraph, at block boundaries."""

PARAGRAPH_MAX_CHARS = 2400
"""Never exceed this much text in one paragraph; oversized blocks are split."""

PARAGRAPH_ID_RE = re.compile(r"^[0-9a-f]{12}:p[0-9]{4}$")
"""The one id shape this layer mints, and the one it recognises as its own."""

_ID_FORMAT = "{key}:p{ordinal:04d}"


class SourceError(RuntimeError):
    """The source layer could not do what was asked of it."""


def sha256_text(text: str) -> str:
    """Hex SHA-256 of a contract's text, as this layer computes it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_key(text: str) -> str:
    """The 12-hex-character key that prefixes every paragraph id of a contract."""
    return sha256_text(text)[:12]


def is_paragraph_id(value: object) -> bool:
    """Whether ``value`` is *shaped* like a paragraph id, whether or not it exists."""
    return isinstance(value, str) and PARAGRAPH_ID_RE.match(value) is not None


class SourceParagraph(BaseModel):
    """One addressable span of one contract.

    Frozen, closed, and carrying no experimental metadata: the id, the contract
    it belongs to, its ordinal position, its exact text, and the half-open
    ``[start_char, end_char)`` span it occupies in the immutable source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paragraph_id: str
    contract_id: str
    ordinal: int = Field(ge=0)
    text: str
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)

    @property
    def length(self) -> int:
        return self.end_char - self.start_char


class ContractDocument(BaseModel):
    """One contract, paragraphized. The unit the retrieval index is built over.

    A document never spans two contracts, so an index built from it cannot
    return a passage from a contract the run is not about.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    text_hash: str
    """``sha256:<64 hex>`` of the exact text this document was built from."""
    paragraphs: tuple[SourceParagraph, ...]

    def paragraph_ids(self) -> tuple[str, ...]:
        return tuple(paragraph.paragraph_id for paragraph in self.paragraphs)

    def paragraph(self, paragraph_id: str) -> SourceParagraph | None:
        """Look one paragraph up by id, or return ``None`` if it is not here."""
        for paragraph in self.paragraphs:
            if paragraph.paragraph_id == paragraph_id:
                return paragraph
        return None

    def __len__(self) -> int:
        return len(self.paragraphs)


# --------------------------------------------------------------------------
# Paragraphization
#
# Split on blank lines, pack blocks up to the target size, split anything still
# oversized at a line or word boundary. Every step is a pure function of the
# text, and every span is expressed in offsets into the original string, so the
# mapping back to the frozen source is exact.
# --------------------------------------------------------------------------


def _blocks(text: str) -> list[tuple[int, int]]:
    """Offsets of maximal runs of non-blank lines."""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    index = 0
    length = len(text)
    while index < length:
        newline = text.find("\n", index)
        line_end = length if newline == -1 else newline
        if text[index:line_end].strip():
            if start is None:
                start = index
            end = line_end
        elif start is not None:
            blocks.append((start, end))
            start = None
        index = line_end + 1
    if start is not None:
        blocks.append((start, end))
    return blocks


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _pack(blocks: list[tuple[int, int]], *, target: int, maximum: int) -> list[tuple[int, int]]:
    """Greedily merge adjacent blocks into paragraphs of about ``target`` chars."""
    packed: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for block in blocks:
        if current is None:
            current = block
            continue
        grown = (current[0], block[1])
        if (grown[1] - grown[0]) <= maximum and (current[1] - current[0]) < target:
            current = grown
        else:
            packed.append(current)
            current = block
    if current is not None:
        packed.append(current)
    return packed


def _split_oversized(text: str, span: tuple[int, int], *, maximum: int) -> list[tuple[int, int]]:
    """Break a span wider than ``maximum`` at a line, then word, then hard cut."""
    pieces: list[tuple[int, int]] = []
    start, end = span
    while end - start > maximum:
        window_end = start + maximum
        cut = text.rfind("\n", start, window_end)
        if cut <= start:
            cut = text.rfind(" ", start, window_end)
        if cut <= start:
            cut = window_end
        pieces.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if start < end:
        pieces.append((start, end))
    return pieces


def build_document(
    contract_id: str,
    text: str,
    *,
    target_chars: int = PARAGRAPH_TARGET_CHARS,
    max_chars: int = PARAGRAPH_MAX_CHARS,
) -> ContractDocument:
    """Paragraphize one contract deterministically.

    ``text`` is read and sliced, never written to. Raises :class:`SourceError`
    for an empty contract or incoherent size parameters, because a document with
    no paragraphs would silently make every later layer answer "not found".
    """
    if not contract_id or not contract_id.strip():
        raise SourceError("a contract document needs a non-empty contract_id")
    if not text or not text.strip():
        raise SourceError(f"contract {contract_id!r} has no text to paragraphize")
    if target_chars <= 0:
        raise SourceError(f"target_chars must be positive, got {target_chars}")
    if max_chars < target_chars:
        raise SourceError(
            f"max_chars ({max_chars}) must be at least target_chars ({target_chars})"
        )

    spans: list[tuple[int, int]] = []
    for block in _blocks(text):
        trimmed = _trim(text, *block)
        if trimmed[0] < trimmed[1]:
            spans.append(trimmed)

    final: list[tuple[int, int]] = []
    for packed in _pack(spans, target=target_chars, maximum=max_chars):
        for piece in _split_oversized(text, packed, maximum=max_chars):
            trimmed = _trim(text, *piece)
            if trimmed[0] < trimmed[1]:
                final.append(trimmed)

    key = document_key(text)
    paragraphs: list[SourceParagraph] = []
    for ordinal, (start, end) in enumerate(final):
        # The span is half-open and taken from the original string, so the text
        # a paragraph reports and the text the frozen source holds at those
        # offsets are the same characters by construction.
        body = text[start:end]
        paragraphs.append(
            SourceParagraph(
                paragraph_id=_ID_FORMAT.format(key=key, ordinal=ordinal),
                contract_id=contract_id,
                ordinal=ordinal,
                text=body,
                start_char=start,
                end_char=end,
            )
        )

    return ContractDocument(
        contract_id=contract_id,
        text_hash=f"sha256:{sha256_text(text)}",
        paragraphs=tuple(paragraphs),
    )
