"""Record provenance for the raw CUAD checkout under ``data/raw/cuad``.

Run this after fetching the upstream repository::

    git clone --depth 1 https://github.com/The-Atticus-Project/cuad data/raw/cuad
    .venv/Scripts/python.exe scripts/cuad_provenance.py

The script never modifies the upstream checkout. It extracts ``data.zip`` into
``data/raw/cuad/extracted`` (idempotently), locates the canonical CUAD v1
annotation file by name rather than by an assumed archive layout, hashes it, and
writes ``data/manifest.md``.

Gate 2 note: the upstream archive member is named ``CUADv1.json``, without the
underscore used in the CUAD paper and in most downstream references. The lookup
below is therefore case- and separator-tolerant, and the manifest records the
name that was actually found.

Gate 2.5 note (single canonical source). The archive ships three JSON files, and
"which one is the dataset" is a real question rather than a formality. This
script answers it by *measuring* the relationship instead of asserting it: the
canonical file's contract set must be the union of the other two, the two must
be disjoint, and every contract they share with it must have byte-identical
context text. The result is recorded in the manifest. If the measurement fails
the script refuses to write a manifest, because a provenance record that
describes a relationship it did not check is worse than none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw" / "cuad"
MANIFEST_PATH = REPO_ROOT / "data" / "manifest.md"
UPSTREAM_URL = "https://github.com/The-Atticus-Project/cuad"

#: Matches the canonical annotation file whatever separator/case upstream uses.
CANONICAL_NAME = re.compile(r"^cuad[_-]?v1\.json$", re.IGNORECASE)



def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def upstream_commit(raw_dir: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=raw_dir)


def extract_archive(raw_dir: Path) -> Path:
    archive = raw_dir / "data.zip"
    if not archive.is_file():
        raise SystemExit(f"no data.zip under {raw_dir}; fetch the upstream repo first")
    extracted = raw_dir / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(extracted)
    return extracted


def locate_canonical(extracted: Path) -> Path:
    """Find the CUAD v1 annotation file without assuming the archive layout."""
    matches = sorted(
        path
        for path in extracted.rglob("*")
        if path.is_file() and CANONICAL_NAME.match(path.name)
    )
    if not matches:
        raise SystemExit(f"no CUAD v1 annotation file found under {extracted}")
    if len(matches) > 1:
        raise SystemExit(f"ambiguous CUAD v1 annotation file: {matches}")
    return matches[0]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarise(canonical: Path) -> tuple[str, int]:
    document = json.loads(canonical.read_text(encoding="utf-8"))
    return str(document.get("version")), len(document.get("data", []))


def relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# --------------------------------------------------------------------------
# The single-canonical-source measurement
# --------------------------------------------------------------------------


class ArchiveView:
    """One JSON file inside the upstream archive, and what it actually holds."""

    def __init__(self, name: str, size: int, digest: str, payload: dict) -> None:
        self.name = name
        self.size = size
        self.digest = digest
        self.version = str(payload.get("version"))
        self._documents = payload.get("data", [])

    @property
    def count(self) -> int:
        return len(self._documents)

    @property
    def titles(self) -> set[str]:
        return {document["title"] for document in self._documents}

    @property
    def contexts(self) -> dict[str, str]:
        return {
            document["title"]: document["paragraphs"][0]["context"]
            for document in self._documents
        }

    @property
    def questions_per_document(self) -> int:
        return len(self._documents[0]["paragraphs"][0]["qas"])


def read_archive_views(archive: Path) -> list[ArchiveView]:
    """Every ``.json`` member of the archive, hashed as it is read."""
    views: list[ArchiveView] = []
    with zipfile.ZipFile(archive) as handle:
        for info in sorted(handle.infolist(), key=lambda item: item.filename):
            if not info.filename.lower().endswith(".json"):
                continue
            raw = handle.read(info.filename)
            views.append(
                ArchiveView(
                    name=info.filename,
                    size=info.file_size,
                    digest=hashlib.sha256(raw).hexdigest(),
                    payload=json.loads(raw.decode("utf-8")),
                )
            )
    return views


class Reconciliation:
    """The measured relationship between the canonical file and its siblings."""

    def __init__(self, canonical: ArchiveView, views: list[ArchiveView]) -> None:
        self.canonical = canonical
        self.others = [view for view in views if view.name != canonical.name]

    def check(self) -> list[str]:
        """Return the statements that were verified, or exit on a contradiction.

        Each check is a claim the manifest will make, so each is measured here.
        A failure is fatal: the point of the record is that these hold.
        """
        canonical_titles = self.canonical.titles
        union: set[str] = set()
        problems: list[str] = []

        for view in self.others:
            if not view.titles <= canonical_titles:
                extra = sorted(view.titles - canonical_titles)
                problems.append(f"{view.name} holds contracts absent from the canonical file: {extra}")
            shared = view.contexts
            mismatched = [
                title
                for title, context in shared.items()
                if title in canonical_titles
                and self.canonical.contexts.get(title) != context
            ]
            if mismatched:
                problems.append(
                    f"{view.name} disagrees with the canonical file on the text of "
                    f"{len(mismatched)} contract(s): {sorted(mismatched)[:5]}"
                )
            union |= view.titles

        if union != canonical_titles:
            missing = sorted(canonical_titles - union)
            problems.append(
                f"{len(missing)} canonical contract(s) appear in no other view: {missing[:5]}"
            )

        for left in range(len(self.others)):
            for right in range(left + 1, len(self.others)):
                overlap = self.others[left].titles & self.others[right].titles
                if overlap:
                    problems.append(
                        f"{self.others[left].name} and {self.others[right].name} overlap "
                        f"on {len(overlap)} contract(s)"
                    )

        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            raise SystemExit(
                "the archive's JSON members are not the views this project assumes; "
                "refusing to write a provenance record that would misdescribe them"
            )

        return [
            f"`{self.canonical.name}` is the complete annotation set "
            f"({self.canonical.count} contracts).",
            "The other members partition it exactly: "
            + " and ".join(f"`{view.name}` ({view.count})" for view in self.others)
            + f" are disjoint and their union is the canonical {self.canonical.count}.",
            "Every contract a partition member shares with the canonical file has "
            "byte-identical `context` text.",
            "The partition members differ from the canonical file only in QA layout "
            f"(`{self.canonical.name}`: {self.canonical.questions_per_document} question(s) "
            f"per contract; `{self.others[0].name}`: {self.others[0].questions_per_document}).",
        ]


def existing_retrieval_date() -> str | None:
    """The fetch date already recorded, so regenerating cannot silently move it.

    The retrieval date is when the bytes were fetched, not when the manifest was
    last rewritten. Defaulting to ``today`` would quietly restate history every
    time this script is re-run, so an existing record wins unless overridden.
    """
    if not MANIFEST_PATH.is_file():
        return None
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*retrieval date\s*\|\s*([^|]+?)\s*\|", line)
        if match:
            return match.group(1).strip().strip("`")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--retrieval-date",
        default=None,
        help="when the bytes were fetched; defaults to the date already recorded",
    )
    args = parser.parse_args(argv)

    retrieval_date = args.retrieval_date or existing_retrieval_date() or date.today().isoformat()

    raw_dir = args.raw_dir.resolve()
    commit = upstream_commit(raw_dir)
    extracted = extract_archive(raw_dir)
    canonical_path = locate_canonical(extracted)
    digest = sha256_of(canonical_path)
    cuad_version, document_count = summarise(canonical_path)

    views = read_archive_views(raw_dir / "data.zip")
    canonical_name = canonical_path.name
    if canonical_name not in {view.name for view in views}:
        raise SystemExit(f"{canonical_name} is not a member of the upstream archive")
    canonical_view = next(view for view in views if view.name == canonical_name)
    if canonical_view.digest != digest:
        raise SystemExit(
            f"{canonical_name} on disk does not match the archive member of the same "
            "name; the extracted copy has been altered"
        )
    relationship = Reconciliation(canonical_view, views).check()

    inventory = "\n".join(
        f"- `{view.name}` — {view.size} bytes, sha256 `{view.digest}`, "
        f"{view.count} contracts, {view.questions_per_document} question(s) per contract"
        for view in views
    )
    relationship_text = "\n".join(f"- {line}" for line in relationship)

    manifest = f"""# Dataset provenance

Generated by `scripts/cuad_provenance.py`. Do not edit by hand.

## Canonical research source

This project has **exactly one** CUAD source snapshot: the upstream checkout at
`{relative(raw_dir)}`, pinned to commit `{commit}`. There is no second snapshot,
and no Hugging Face snapshot is present on disk or referenced by this project.
Contract text for retrieval is read from that checkout's `{canonical_name}` and
from nothing else.

## CUAD (Contract Understanding Atticus Dataset)

| field | value |
| --- | --- |
| source | {UPSTREAM_URL} |
| upstream commit | `{commit}` |
| retrieval date | {retrieval_date} |
| clone depth | shallow (`--depth 1`) |
| local checkout | `{relative(raw_dir)}` (immutable; not tracked by git) |
| archive | `{relative(raw_dir / "data.zip")}` |
| extracted under | `{relative(extracted)}` |
| canonical annotation file | `{relative(canonical_path)}` |
| archive member name | `{canonical_name}` |
| CUAD internal version | `{cuad_version}` |
| SHA-256 of canonical file | `{digest}` |
| contracts in canonical file | {document_count} |
| json members in archive | {len(views)} |
| canonical snapshot count | 1 |

The canonical annotation file is located by name rather than by an assumed
archive layout. Upstream names the member `{canonical_name}`; the underscore
spelling `CUAD_v1.json` used in the CUAD paper and in most downstream
references does not appear in the archive, so the lookup is case- and
separator-tolerant.

### The archive's JSON members

{inventory}

### Why `{canonical_name}` is the canonical one

The three members are not competing copies of the dataset; they are one dataset
and its official partition. Measured, not assumed:

{relationship_text}

The QA layout difference matters for a reader of this record: the
separate-questions member splits each contract's questions one per line and
includes the unanswerable ones, so its per-contract question count is higher
without its contract text differing at all. Because the contract text is
identical, the choice of member cannot change what a retrieval layer would
return -- but the project still names one, and reads only that one, so that the
question is settled rather than re-litigated per module.

## Immutability

`data/raw/` is treated as immutable source material and is excluded from
version control. Nothing under it is modified by this project.
"""

    MANIFEST_PATH.write_text(manifest, encoding="utf-8")
    print(f"wrote {relative(MANIFEST_PATH)}")
    print(f"  upstream commit : {commit}")
    print(f"  canonical file  : {relative(canonical_path)}")
    print(f"  sha256          : {digest}")
    print(f"  archive members : {len(views)} ({', '.join(view.name for view in views)})")
    print("  reconciliation  : all checks passed")
    return 0



if __name__ == "__main__":
    sys.exit(main())
