"""Reading contract text out of the one canonical CUAD snapshot.

The rule this module exists to keep is that retrieval has exactly one place to
get contract text from, and it is the file the provenance manifest names. Not a
path someone typed here, not a second copy that drifted: the manifest is read,
the file it points at is hashed, and the hash has to match the one the manifest
recorded before a single paragraph is built. A mismatch is a hard failure, since
the alternative is silently running the pilot against different bytes than the
ones the record describes.

Only ``context`` -- the contract's own words -- is read. The CUAD annotations
that sit beside it (the labelled clauses, their gold answer offsets, the
``is_impossible`` flags) are never touched, and there is no code path here that
could reach them: :func:`_contexts` walks to ``paragraphs[0]["context"]`` and
returns nothing else.

The contract *title* is loaded, because building a case registry later needs a
human-readable handle for a contract. It is experimenter-side data and it stops
here: the source layer's :class:`~pilot01.source.document.ContractDocument` has
no title field, and the only bridge between the two is
:meth:`CuadSource.library`, which passes an id and a text and nothing else. That
matters more than it looks -- CUAD titles name the agreement type, and some of
them name the very clause categories the policy treats as critical, so a title
that reached an agent would be a hint about the answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .document import ContractDocument, sha256_text
from .tools import SourceLibrary

__all__ = [
    "REPO_ROOT",
    "MANIFEST_PATH",
    "CuadContract",
    "CuadSourceError",
    "CuadSource",
    "load_contracts",
    "canonical_annotation_path",
    "recorded_digest",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.md"

_CONTRACT_ID = "CUAD-{ordinal:04d}"
_ROW = re.compile(r"^\|\s*(?P<field>[^|]+?)\s*\|\s*(?P<value>[^|]+?)\s*\|\s*$")


class CuadSourceError(RuntimeError):
    """The canonical CUAD source is missing, unreadable, or not what was recorded."""


def _manifest_rows(path: Path = MANIFEST_PATH) -> dict[str, str]:
    """Parse the ``| field | value |`` rows the provenance script writes."""
    if not path.is_file():
        raise CuadSourceError(
            f"no provenance manifest at {path}; run scripts/cuad_provenance.py first"
        )
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if not match or match.group("field").lower() == "field":
            continue
        rows[match.group("field")] = match.group("value").strip().strip("`")
    return rows


def canonical_annotation_path(manifest: Path = MANIFEST_PATH) -> Path:
    """The canonical file, as the provenance record names it."""
    rows = _manifest_rows(manifest)
    try:
        relative = rows["canonical annotation file"]
    except KeyError:
        raise CuadSourceError(
            f"{manifest} does not name a canonical annotation file"
        ) from None
    return (REPO_ROOT / relative).resolve()


def recorded_digest(manifest: Path = MANIFEST_PATH) -> str:
    rows = _manifest_rows(manifest)
    try:
        return rows["SHA-256 of canonical file"]
    except KeyError:
        raise CuadSourceError(f"{manifest} does not record a digest") from None


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CuadContract:
    """One contract's text, with a neutral id.

    ``title`` is experimenter-side: see the module docstring. It is deliberately
    not part of any model, so nothing that serialises a contract can carry it
    into an agent's context by accident.
    """

    __slots__ = ("contract_id", "title", "text", "ordinal")

    def __init__(self, *, contract_id: str, title: str, text: str, ordinal: int) -> None:
        self.contract_id = contract_id
        self.title = title
        self.text = text
        self.ordinal = ordinal

    @property
    def text_hash(self) -> str:
        return "sha256:" + sha256_text(self.text)

    def document(self) -> ContractDocument:
        """Paragraphize this contract. The only agent-facing product of this type."""
        from .document import build_document

        return build_document(self.contract_id, self.text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CuadContract({self.contract_id!r}, {len(self.text)} chars)"


def _contexts(payload: dict) -> list[tuple[str, str]]:
    """``(title, context)`` for every contract, in file order.

    Walks to the contract's text and stops. The annotations alongside it are not
    read, so no gold offset or category label can enter through this function.
    """
    pairs: list[tuple[str, str]] = []
    for document in payload.get("data", []):
        paragraphs = document.get("paragraphs") or []
        if not paragraphs:
            raise CuadSourceError(
                f"CUAD contract {document.get('title')!r} has no paragraphs to read"
            )
        context = paragraphs[0].get("context")
        if not isinstance(context, str) or not context.strip():
            raise CuadSourceError(
                f"CUAD contract {document.get('title')!r} has no usable context text"
            )
        pairs.append((str(document.get("title", "")), context))
    return pairs


class CuadSource:
    """The canonical snapshot, loaded once and verified against the record."""

    def __init__(self, contracts: tuple[CuadContract, ...], *, digest: str, path: Path) -> None:
        self._contracts = contracts
        self._by_id = {contract.contract_id: contract for contract in contracts}
        self.digest = digest
        self.path = path

    @property
    def contracts(self) -> tuple[CuadContract, ...]:
        return self._contracts

    def __len__(self) -> int:
        return len(self._contracts)

    @property
    def contract_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def contract(self, contract_id: str) -> CuadContract:
        try:
            return self._by_id[contract_id]
        except KeyError:
            raise CuadSourceError(
                f"no contract {contract_id!r} in the canonical source; ids run "
                f"{self.contract_ids[0]}..{self.contract_ids[-1]}"
            ) from None

    def library(self, contract_ids: tuple[str, ...] | None = None) -> SourceLibrary:
        """A library holding only the named contracts -- usually exactly one.

        Restricting here rather than at search time is what makes cross-contract
        leakage structurally impossible: an index built from one document has no
        other document's paragraphs in it to return.
        """
        wanted = self.contract_ids if contract_ids is None else contract_ids
        return SourceLibrary(
            {contract_id: self.contract(contract_id).document() for contract_id in wanted}
        )


def load_contracts(
    *,
    path: Path | None = None,
    manifest: Path = MANIFEST_PATH,
    verify: bool = True,
) -> CuadSource:
    """Load the canonical CUAD contracts, verifying the file against the record.

    ``verify=False`` exists for a caller that has already hashed the file in the
    same process and does not want to pay for 40 MB of SHA-256 twice. It is not
    a way to skip provenance: it is a way to not repeat it.
    """
    annotation = Path(path) if path is not None else canonical_annotation_path(manifest)
    if not annotation.is_file():
        raise CuadSourceError(
            f"the canonical CUAD file {annotation} is absent; fetch the upstream "
            "checkout and run scripts/cuad_provenance.py"
        )
    digest = _digest_of(annotation)
    if verify:
        expected = recorded_digest(manifest)
        if digest != expected:
            raise CuadSourceError(
                f"the canonical CUAD file at {annotation} hashes to {digest}, but "
                f"{manifest} records {expected}; the frozen source has changed and "
                "retrieval must not proceed against unrecorded bytes"
            )

    payload = json.loads(annotation.read_text(encoding="utf-8"))
    contracts = tuple(
        CuadContract(
            contract_id=_CONTRACT_ID.format(ordinal=ordinal),
            title=title,
            text=text,
            ordinal=ordinal,
        )
        for ordinal, (title, text) in enumerate(_contexts(payload))
    )
    if not contracts:
        raise CuadSourceError(f"the canonical CUAD file at {annotation} holds no contracts")
    return CuadSource(contracts, digest=digest, path=annotation)
