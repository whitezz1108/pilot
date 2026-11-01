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
from typing import Literal

from .document import SourceParagraph

__all__ = [
    "K1",
    "B",
    "EXCERPT_CHARS",
    "tokenize",
    "excerpt",
    "select_lexical_excerpt",
    "BM25Index",
]

K1 = 1.2
"""BM25 term-frequency saturation."""

B = 0.75
"""BM25 length normalisation."""

EXCERPT_CHARS = 400
"""How much of a paragraph a search result shows before it is opened."""

_TOKEN = re.compile(r"[a-z0-9]+")

# Only function words that add no useful contract-local discrimination.  This
# is deliberately a short, inspectable list: v2 is still lexical retrieval,
# not a hidden semantic classifier.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "to", "with",
})
_PHRASE_BONUS = 2.0
_COVERAGE_BONUS = 1.5
_PROXIMITY_BONUS = 1.0


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


def _phrase_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """Ordered phrase representation: preserve duplicates and internal stopwords."""
    phrase = list(tokens)
    while phrase and phrase[0] in _STOPWORDS:
        phrase.pop(0)
    while phrase and phrase[-1] in _STOPWORDS:
        phrase.pop()
    return tuple(phrase)


def _keyword_terms(tokens: Sequence[str]) -> tuple[str, ...]:
    """Ranking representation: remove stopwords, then deduplicate in query order."""
    content = tuple(token for token in tokens if token not in _STOPWORDS)
    return tuple(dict.fromkeys(content or tuple(tokens)))


def _bounded_excerpt(text: str, start: int, width: int) -> tuple[str, int, int]:
    """Return an exact source substring no longer than ``width`` characters."""
    if len(text) <= width:
        return text, 0, len(text)
    start = max(0, min(start, len(text) - width))
    end = min(len(text), start + width)
    if start > 0 and not text[start - 1].isspace() and not text[start].isspace():
        boundary = text.find(" ", start, end)
        if boundary != -1:
            start = boundary + 1
    if end < len(text) and not text[end - 1].isspace() and not text[end].isspace():
        boundary = text.rfind(" ", start, end)
        if boundary > start:
            end = boundary
    return text[start:end], start, end


def select_lexical_excerpt(
    text: str,
    query: str,
    width: int = EXCERPT_CHARS,
) -> tuple[str, int, int, str]:
    """Choose a deterministic verbatim v2 excerpt and its paragraph offsets.

    Exact ordered phrase occurrences win.  Otherwise choose a window containing
    the greatest number of distinct effective keyword terms; earliest source
    position wins ties.  Stopword frequency therefore cannot pull the window
    away from the substantive hit.
    """
    if len(text) <= width:
        return text, 0, len(text), "full_paragraph"
    query_tokens = tokenize(query)
    phrase = _phrase_tokens(query_tokens)
    matches = tuple(_TOKEN.finditer(text.lower()))
    document_tokens = tuple(match.group() for match in matches)
    if len(phrase) > 1:
        for index in range(len(document_tokens) - len(phrase) + 1):
            if document_tokens[index : index + len(phrase)] == phrase:
                phrase_start = matches[index].start()
                phrase_end = matches[index + len(phrase) - 1].end()
                desired = phrase_start - max(0, (width - (phrase_end - phrase_start)) // 2)
                body, start, end = _bounded_excerpt(text, desired, width)
                return body, start, end, "exact_phrase"

    keywords = set(_keyword_terms(query_tokens))
    positions = [match for match in matches if match.group() in keywords]
    if not positions:
        body, start, end = _bounded_excerpt(text, 0, width)
        return body, start, end, "paragraph_start"
    candidates = {0}
    for match in positions:
        candidates.add(match.start())
        candidates.add(max(0, match.end() - width))
    best_start = 0
    best_coverage = -1
    for candidate in sorted(candidates):
        end = candidate + width
        coverage = len({
            match.group()
            for match in positions
            if candidate <= match.start() and match.end() <= end
        })
        if coverage > best_coverage:
            best_coverage = coverage
            best_start = candidate
    body, start, end = _bounded_excerpt(text, best_start, width)
    return body, start, end, "content_terms"


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
        ranking_version: Literal["bm25-v1", "human-lexical-v2"] = "bm25-v1",
    ) -> None:
        if ranking_version not in {"bm25-v1", "human-lexical-v2"}:
            raise ValueError(f"unknown lexical ranking version: {ranking_version!r}")
        self._paragraphs = tuple(paragraphs)
        self._k1 = k1
        self._b = b
        self._ranking_version = ranking_version
        self._tokens = tuple(tokenize(p.text) for p in self._paragraphs)
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

    @property
    def ranking_version(self) -> str:
        return self._ranking_version

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

    @staticmethod
    def _smallest_covering_window(tokens: Sequence[str], terms: Sequence[str]) -> int | None:
        """Smallest token span containing every distinct query term."""
        wanted = set(terms)
        positions = [(index, token) for index, token in enumerate(tokens) if token in wanted]
        if not positions:
            return None
        counts: Counter[str] = Counter()
        covered = 0
        left = 0
        best: int | None = None
        for right, (position, token) in enumerate(positions):
            counts[token] += 1
            if counts[token] == 1:
                covered += 1
            while covered == len(wanted):
                start = positions[left][0]
                width = position - start + 1
                best = width if best is None else min(best, width)
                old = positions[left][1]
                counts[old] -= 1
                if counts[old] == 0:
                    covered -= 1
                left += 1
        return best

    def diagnostics(self, paragraph: SourceParagraph, query: str) -> dict[str, object]:
        """Return auditable v2 match signals for an already-ranked hit."""
        query_tokens = tokenize(query)
        content = _keyword_terms(query_tokens)
        try:
            index = self._paragraphs.index(paragraph)
        except ValueError:  # pragma: no cover - guard for misuse outside this index
            raise ValueError("paragraph does not belong to this index") from None
        frequency = self._frequencies[index]
        matched = tuple(term for term in content if frequency.get(term))
        positions = self._tokens[index]
        phrase_terms = _phrase_tokens(query_tokens)
        phrase = (
            len(phrase_terms) > 1
            and any(
                tuple(positions[i : i + len(phrase_terms)]) == phrase_terms
                for i in range(len(positions))
            )
        )
        proximity = self._smallest_covering_window(positions, content) if len(matched) == len(content) else None
        return {
            "matched_terms": matched,
            "term_coverage": len(matched) / len(content) if content else 0.0,
            "exact_phrase": phrase,
            "proximity_tokens": proximity,
        }

    def search(self, query: str, *, top_k: int = 5) -> tuple[tuple[SourceParagraph, float], ...]:
        """Rank paragraphs against ``query``.

        Repeated query terms are counted once: BM25 sums over *distinct* query
        terms, and letting a repeated word multiply its own weight would make
        the ranking depend on how the model happened to phrase the query.
        """
        if top_k <= 0:
            return ()
        query_tokens = tokenize(query)
        if not query_tokens:
            return ()
        if self._ranking_version == "bm25-v1":
            rank_terms = tuple(dict.fromkeys(query_tokens))
        else:
            rank_terms = _keyword_terms(query_tokens)
        scored = [
            (paragraph, self._score(index, rank_terms))
            for index, paragraph in enumerate(self._paragraphs)
        ]
        if self._ranking_version == "human-lexical-v2":
            enhanced = []
            for index, (paragraph, score) in enumerate(scored):
                diagnostics = self.diagnostics(paragraph, query)
                coverage = float(diagnostics["term_coverage"])
                proximity = diagnostics["proximity_tokens"]
                if score > 0.0:
                    score += _COVERAGE_BONUS * coverage
                    if diagnostics["exact_phrase"]:
                        score += _PHRASE_BONUS
                    if isinstance(proximity, int) and proximity:
                        score += _PROXIMITY_BONUS * len(rank_terms) / proximity
                enhanced.append((paragraph, score))
            scored = enhanced
        hits = [item for item in scored if item[1] > 0.0]
        hits.sort(key=lambda item: (-item[1], item[0].ordinal))
        return tuple(hits[:top_k])
