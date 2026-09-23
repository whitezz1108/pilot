"""Deterministic lexical retrieval over one contract's paragraphs.

BM25, implemented here in about sixty lines rather than pulled in as a
dependency, because the pilot needs exactly three things from a retriever and
none of them is scale:

* **determinism** -- the same query against the same contract returns the same
  passages in the same order, on every machine and every run;
* **no training, no embeddings, no external service** -- a lexical scorer over
  the contract's own words is auditable in a way a vector index is not, and it
  cannot smuggle in a notion of "the right answer" that was learned elsewhere;
* **neutrality** -- the index is built from paragraph text alone. It never sees
  a CUAD category label, a gold offset, an answer span, or a treatment label,
  because none of those is ever passed to it.

Ties are broken by paragraph ordinal, so a query that scores two paragraphs
equally always returns them in source order. That matters more than it sounds:
a retrieval layer whose output order wobbles between runs would make two
otherwise identical experimental runs non-comparable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from .document import SourceParagraph

__all__ = [
    "K1",
    "B",
    "EXCERPT_CHARS",
    "tokenize",
    "excerpt",
    "BM25Index",
]

K1 = 1.2
"""BM25 term-frequency saturation."""

B = 0.75
"""BM25 length normalisation."""

EXCERPT_CHARS = 400
"""How much of a paragraph a search result shows before it is opened."""

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> tuple[str, ...]:
    """Lower-case alphanumeric tokens. The only tokenizer this layer uses."""
    return tuple(_TOKEN.findall(text.lower()))


def _clip(text: str, start: int, width: int) -> str:
    """A window of ``text`` around ``start``, widened to word boundaries."""
    start = max(0, min(start, max(0, len(text) - width)))
    end = min(len(text), start + width)
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    body = text[start:end].strip()
    return f"{'...' if start > 0 else ''}{body}{'...' if end < len(text) else ''}"


def excerpt(text: str, terms: Sequence[str], width: int = EXCERPT_CHARS) -> str:
    """The densest window of ``text`` for ``terms``, deterministically chosen.

    A search result shows a snippet, not the paragraph: ``open_source_span`` is
    the operation that returns the exact source text. Picking the window with
    the most query-term occurrences (earliest wins on a tie) keeps the snippet
    useful without making it a substitute for opening the source.
    """
    if len(text) <= width:
        return text
    wanted = set(terms)
    positions = [
        match.start() for match in _TOKEN.finditer(text.lower()) if match.group() in wanted
    ]
    if not positions:
        return _clip(text, 0, width)

    best_start = positions[0]
    best_count = -1
    for candidate in positions:
        count = sum(1 for other in positions if candidate <= other < candidate + width)
        if count > best_count:
            best_count = count
            best_start = candidate
    return _clip(text, best_start, width)


class BM25Index:
    """A lexical index over the paragraphs of exactly one contract.

    Constructed per contract and per invocation. There is no global index and no
    cache that could carry a paragraph from one contract into another run's
    results.
    """

    def __init__(
        self,
        paragraphs: Iterable[SourceParagraph],
        *,
        k1: float = K1,
        b: float = B,
    ) -> None:
        self._paragraphs = tuple(paragraphs)
        self._k1 = k1
        self._b = b
        self._frequencies = tuple(Counter(tokenize(p.text)) for p in self._paragraphs)
        self._lengths = tuple(sum(f.values()) for f in self._frequencies)
        self._average_length = (
            sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        )

        document_frequency: Counter[str] = Counter()
        for frequency in self._frequencies:
            document_frequency.update(frequency.keys())
        total = len(self._paragraphs)
        self._idf = {
            term: math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for term, df in document_frequency.items()
        }

    @property
    def paragraphs(self) -> tuple[SourceParagraph, ...]:
        return self._paragraphs

    def __len__(self) -> int:
        return len(self._paragraphs)

    def _score(self, index: int, terms: Sequence[str]) -> float:
        frequency = self._frequencies[index]
        length = self._lengths[index] or 1
        average = self._average_length or 1.0
        normaliser = self._k1 * (1.0 - self._b + self._b * length / average)
        score = 0.0
        for term in terms:
            count = frequency.get(term)
            if not count:
                continue
            score += self._idf.get(term, 0.0) * (count * (self._k1 + 1.0)) / (
                count + normaliser
            )
        return score

    def search(self, query: str, *, top_k: int = 5) -> tuple[tuple[SourceParagraph, float], ...]:
        """Rank paragraphs against ``query``.

        Repeated query terms are counted once: BM25 sums over *distinct* query
        terms, and letting a repeated word multiply its own weight would make
        the ranking depend on how the model happened to phrase the query.
        """
        if top_k <= 0:
            return ()
        terms = tuple(dict.fromkeys(tokenize(query)))
        if not terms:
            return ()
        scored = [
            (paragraph, self._score(index, terms))
            for index, paragraph in enumerate(self._paragraphs)
        ]
        hits = [item for item in scored if item[1] > 0.0]
        hits.sort(key=lambda item: (-item[1], item[0].ordinal))
        return tuple(hits[:top_k])
