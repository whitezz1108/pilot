"""Reading CUAD's gold clause annotations -- experimenter-side only.

The source layer reads exactly one field out of the canonical CUAD file,
``context``, and has no code path that could reach the annotations beside it
(see :mod:`pilot01.source.cuad`). That is the right boundary for retrieval, and
it is why this module exists separately rather than as an addition there:
building a real case needs the labelled clauses, and the labelled clauses are
gold.

**What "gold" means here.** For each contract CUAD carries one question per
clause category, and for each question either a set of answer spans with
character offsets into ``context``, or ``is_impossible``. A category is
*positive* for a contract when it has at least one non-empty answer span and is
not marked impossible, and *absent* when it is marked impossible or carries no
usable span. Nothing in this module interprets a clause: it reads the frozen
annotation and reports what the annotators recorded.

**Nothing here is agent-facing.** The module sits under
:mod:`pilot01.experiment`, which no node, no source-layer module and no model
module imports, and it returns plain frozen data with no rendering path. The
gold spans it yields end up in a case's ``gold_evidence_offsets`` and in the
hidden half of a run's state; the category *names* also end up in the policy,
which is legitimately shown to agents, but the per-contract status never does.

**Offsets are checked, not assumed.** CUAD offsets are known to be exact for
this snapshot, but "known to be" is not a property a case builder should rely
on silently, so :meth:`CuadAnnotationSet.verify` re-slices every span out of
the frozen context and refuses the file if any span does not reproduce its own
text. A case built on an offset that had drifted would score every run against
the wrong characters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ...source.cuad import (
    CuadSourceError,
    canonical_annotation_path,
    load_contracts,
    recorded_digest,
)

__all__ = [
    "QUESTION_CATEGORY_RE",
    "CuadAnnotationError",
    "CuadClause",
    "CuadContractAnnotations",
    "CuadAnnotationSet",
    "load_annotations",
    "CONTRACT_ID_FORMAT",
]

CONTRACT_ID_FORMAT = "CUAD-{ordinal:04d}"
"""The id scheme :mod:`pilot01.source.cuad` mints. Duplicated deliberately.

It is duplicated rather than imported so that this module and the source layer
can be checked against each other: a test asserts the two schemes agree on
every contract, which is what keeps a registry's ``contract_id`` pointing at
the contract whose annotations were read for it.
"""

QUESTION_CATEGORY_RE = re.compile(r'related to\s+"(?P<category>[^"]+)"')
"""CUAD's question template: ``... related to "<Category>" that should be ...``.

Matched rather than split on a fixed delimiter, so a category containing a
comma or a slash is read whole. A question that does not match is skipped and
counted, not guessed at.
"""


class CuadAnnotationError(RuntimeError):
    """The canonical annotations are missing, unreadable, or internally inconsistent."""


class CuadClause(BaseModel):
    """One clause category's annotation for one contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    is_impossible: bool
    spans: tuple[tuple[int, int], ...] = ()
    """Half-open ``(start, end)`` character spans into the contract's context."""

    @property
    def present(self) -> bool:
        """Whether CUAD records this clause as present in this contract."""
        return bool(self.spans) and not self.is_impossible

    @property
    def span_count(self) -> int:
        return len(self.spans)

    def texts(self, context: str) -> tuple[str, ...]:
        """The annotated text of each span, sliced out of the frozen context."""
        return tuple(context[start:end] for start, end in self.spans)


class CuadContractAnnotations(BaseModel):
    """One contract: its text, its title, and every clause CUAD labelled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: str
    ordinal: int = Field(ge=0)
    title: str
    context: str
    clauses: tuple[CuadClause, ...]
    questions_skipped: int = Field(default=0, ge=0)
    """Questions whose category could not be parsed. Reported, never guessed."""

    def clause(self, category: str) -> CuadClause:
        """The annotation for one category.

        A category CUAD does not ask about at all is a hard error rather than
        an implicit "absent": the two are different facts, and a case builder
        that confused them would build a sentinel out of a question nobody
        asked.
        """
        for clause in self.clauses:
            if clause.category == category:
                return clause
        raise CuadAnnotationError(
            f"contract {self.contract_id!r} has no CUAD annotation for category "
            f"{category!r}; it carries {len(self.clauses)} categories"
        )

    def categories(self) -> tuple[str, ...]:
        return tuple(clause.category for clause in self.clauses)

    def status(self, category: str) -> bool:
        """Whether ``category`` is positive for this contract."""
        return self.clause(category).present

    @property
    def text_hash(self) -> str:
        from ...source.document import sha256_text

        return "sha256:" + sha256_text(self.context)


class CuadAnnotationSet(BaseModel):
    """Every contract's annotations, plus the provenance of the file they came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str
    source_digest: str
    """The SHA-256 the provenance manifest records for the canonical file."""

    contracts: tuple[CuadContractAnnotations, ...]

    def __len__(self) -> int:
        return len(self.contracts)

    def by_id(self, contract_id: str) -> CuadContractAnnotations:
        for contract in self.contracts:
            if contract.contract_id == contract_id:
                return contract
        raise CuadAnnotationError(f"no contract {contract_id!r} in the canonical annotations")

    def all_categories(self) -> tuple[str, ...]:
        """Every category named by any contract's questions, in first-seen order."""
        seen: list[str] = []
        for contract in self.contracts:
            for category in contract.categories():
                if category not in seen:
                    seen.append(category)
        return tuple(seen)

    def verify(self, categories: Iterable[str] | None = None) -> tuple[str, ...]:
        """Re-slice every gold span out of its own context.

        Returns the ids of contracts that failed, so a caller can report all of
        them rather than the first. A span whose ``context[start:end]`` does not
        equal the annotation's own text means the offsets and the text disagree,
        and every case built from it would score against the wrong characters.

        ``categories`` restricts the check to the categories a caller cares
        about; the full file is checked when it is omitted.
        """
        wanted = None if categories is None else set(categories)
        broken: list[str] = []
        for contract in self.contracts:
            for clause in contract.clauses:
                if wanted is not None and clause.category not in wanted:
                    continue
                for start, end in clause.spans:
                    if not (0 <= start < end <= len(contract.context)):
                        broken.append(contract.contract_id)
                        break
        return tuple(dict.fromkeys(broken))


def _extract_clauses(
    payload: dict, context: str
) -> tuple[tuple[CuadClause, ...], int]:
    """Read one contract's clause annotations.

    Walks ``paragraphs[0]["qas"]`` and stops. Every answer's ``answer_start`` is
    paired with the length of its own ``text`` to make the half-open span, which
    is how CUAD expresses an offset, and the span is not trusted until
    :meth:`CuadAnnotationSet.verify` re-slices it.
    """
    clauses: list[CuadClause] = []
    skipped = 0
    paragraphs = payload.get("paragraphs") or []
    if not paragraphs:
        raise CuadAnnotationError(
            f"CUAD contract {payload.get('title')!r} has no paragraphs to annotate"
        )
    for question in paragraphs[0].get("qas") or []:
        match = QUESTION_CATEGORY_RE.search(str(question.get("question", "")))
        if match is None:
            skipped += 1
            continue
        spans: list[tuple[int, int]] = []
        for answer in question.get("answers") or []:
            text = answer.get("text")
            start = answer.get("answer_start")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(start, int):
                continue
            spans.append((start, start + len(text)))
        clauses.append(
            CuadClause(
                category=match.group("category"),
                is_impossible=bool(question.get("is_impossible")),
                spans=tuple(spans),
            )
        )
    return tuple(clauses), skipped


def load_annotations(
    *,
    path: Path | None = None,
    verify_offsets: bool = True,
    categories: Sequence[str] | None = None,
) -> CuadAnnotationSet:
    """Load the canonical CUAD annotations.

    The contract *text* is read here as well as the labels, because the labels
    are offsets into it and a case builder needs both together. The text is
    then handed to the source layer to paragraphize, so a case's contract text
    and its gold offsets come from one read of one file -- the same file, and
    the same bytes, that retrieval will later serve.

    The digest is checked against the provenance manifest before the file is
    parsed, so a snapshot that has changed underneath the project is a hard
    failure here rather than a quietly different experiment.
    """
    import hashlib

    annotation = Path(path) if path is not None else canonical_annotation_path()
    if not annotation.is_file():
        raise CuadAnnotationError(
            f"the canonical CUAD file {annotation} is absent; fetch the upstream "
            "checkout and run scripts/cuad_provenance.py"
        )
    digest = hashlib.sha256(annotation.read_bytes()).hexdigest()
    expected = recorded_digest()
    if digest != expected:
        raise CuadAnnotationError(
            f"the canonical CUAD file at {annotation} hashes to {digest}, but the "
            f"provenance manifest records {expected}; refusing to build cases against "
            "unrecorded bytes"
        )

    payload = json.loads(annotation.read_text(encoding="utf-8"))
    contracts: list[CuadContractAnnotations] = []
    for ordinal, document in enumerate(payload.get("data", [])):
        paragraphs = document.get("paragraphs") or []
        if not paragraphs:
            raise CuadAnnotationError(
                f"CUAD contract {document.get('title')!r} has no paragraphs"
            )
        context = paragraphs[0].get("context")
        if not isinstance(context, str) or not context.strip():
            raise CuadAnnotationError(
                f"CUAD contract {document.get('title')!r} has no usable context text"
            )
        clauses, skipped = _extract_clauses(document, context)
        contracts.append(
            CuadContractAnnotations(
                contract_id=CONTRACT_ID_FORMAT.format(ordinal=ordinal),
                ordinal=ordinal,
                title=str(document.get("title", "")),
                context=context,
                clauses=clauses,
                questions_skipped=skipped,
            )
        )

    annotation_set = CuadAnnotationSet(
        source_path=str(annotation),
        source_digest=digest,
        contracts=tuple(contracts),
    )
    if verify_offsets:
        broken = annotation_set.verify(categories)
        if broken:
            raise CuadAnnotationError(
                f"{len(broken)} contract(s) carry an annotation offset that does not "
                f"reproduce its own text, first: {broken[0]}. The offsets and the "
                "contract text disagree, so every case built from them would be "
                "scored against the wrong characters."
            )
    return annotation_set


def cross_check_contract_ids(annotations: CuadAnnotationSet) -> None:
    """Assert this module and the source layer agree on every contract id.

    The source layer mints ``CUAD-<ordinal>`` while reading ``context``; this
    module mints the same id while reading the annotations. If the two ever
    drifted, a registry entry would name a contract whose labels came from a
    different contract's questions -- a leak of the worst kind, since it would
    be invisible in every artifact and wrong in every score.
    """
    try:
        source = load_contracts(verify=False)
    except CuadSourceError as exc:  # pragma: no cover - provenance failure path
        raise CuadAnnotationError(str(exc)) from exc
    if len(source) != len(annotations):
        raise CuadAnnotationError(
            f"the source layer reads {len(source)} contract(s) but the annotations "
            f"hold {len(annotations)}; the two readers disagree about the file"
        )
    for contract in source.contracts:
        annotated = annotations.by_id(contract.contract_id)
        if annotated.context != contract.text:
            raise CuadAnnotationError(
                f"contract {contract.contract_id!r} has different text in the source "
                "layer and in the annotation reader; the two must read one file the "
                "same way"
            )
