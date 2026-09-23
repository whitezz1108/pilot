"""Gate 2: the CUAD provenance record, and its exclusion from version control.

Requirement 20 is that the raw CUAD directory is not tracked by the parent
repository. That is checked here, together with the things that make the
exclusion safe: the manifest that stands in for the ignored bytes is present,
it is *not* ignored, it records where the data came from and which revision, and
the digest it records is the digest of the file that is actually on disk.

Every test that needs the download is skipped when ``data/raw/`` is absent, so
the suite still runs on a machine that has not fetched the dataset. The tests
that check the ignore rules run either way -- they are about the repository, not
about the bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

import pilot01

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw"
MANIFEST = REPO_ROOT / "data" / "manifest.md"
UPSTREAM_URL = "https://github.com/The-Atticus-Project/cuad"

requires_raw = pytest.mark.skipif(
    not RAW_DIR.exists(),
    reason="data/raw/ is absent; the dataset has not been fetched on this machine",
)


def _manifest_rows() -> dict[str, str]:
    """Parse the ``| field | value |`` table the provenance script writes."""
    rows: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*(?P<field>[^|]+?)\s*\|\s*(?P<value>[^|]+?)\s*\|\s*$", line)
        if not match:
            continue
        field = match.group("field")
        if field.lower() == "field":  # the header row
            continue
        rows[field] = match.group("value").strip().strip("`")
    return rows


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def _is_ignored(path: str) -> bool:
    """``git check-ignore`` exits 0 when the path is ignored."""
    return _git("check-ignore", "-q", path).returncode == 0


# --------------------------------------------------------------------------
# The manifest itself
# --------------------------------------------------------------------------


def test_the_manifest_exists_and_is_not_ignored():
    assert MANIFEST.is_file()
    assert not _is_ignored("data/manifest.md"), (
        "data/manifest.md is the version-controlled record of an ignored download; "
        "ignoring it would leave the raw bytes with no provenance"
    )


def test_only_the_raw_subdirectory_is_ignored():
    """``data/raw/`` is ignored; ``data/`` is not.

    Later Gates add cases, registries and result manifests under ``data/``, and
    those must stay under version control.
    """
    assert _is_ignored("data/raw/anything.json")
    assert not _is_ignored("data/manifest.md")
    assert not _is_ignored("data/cases/CASE-001.json")
    assert not _is_ignored("data/registries/anything.yaml")


def test_nothing_under_the_raw_directory_is_tracked():
    tracked = _git("ls-files", "data/raw").stdout.strip()
    assert tracked == "", f"data/raw/ must not be tracked; git tracks: {tracked}"


def test_the_raw_directory_is_not_reported_as_untracked():
    """Ignored, not merely unstaged: it must not appear in ``git status`` at all."""
    porcelain = _git("status", "--porcelain", "-uall", "data/raw").stdout.strip()
    assert porcelain == "", f"data/raw/ is not properly ignored: {porcelain}"


def test_the_provenance_script_is_trackable():
    """The record is reproducible: the recorder is in the repository, not ignored.

    Checked as "not ignored" rather than "tracked", so the test holds both before
    and after the Gate 2 commit.
    """
    assert (REPO_ROOT / "scripts" / "cuad_provenance.py").is_file()
    assert not _is_ignored("scripts/cuad_provenance.py")


# --------------------------------------------------------------------------
# What the manifest records
# --------------------------------------------------------------------------


def test_the_manifest_records_the_source_and_the_revision():
    rows = _manifest_rows()
    assert rows["source"] == UPSTREAM_URL
    assert re.fullmatch(r"[0-9a-f]{40}", rows["upstream commit"]), rows["upstream commit"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rows["retrieval date"]), rows["retrieval date"]


def test_the_manifest_records_a_canonical_path_and_a_digest():
    rows = _manifest_rows()
    canonical = rows["canonical annotation file"]
    assert canonical.startswith("data/raw/cuad/")
    assert canonical.endswith(".json")
    assert re.fullmatch(r"[0-9a-f]{64}", rows["SHA-256 of canonical file"])


def test_the_manifest_records_the_contract_count():
    rows = _manifest_rows()
    assert int(rows["contracts in canonical file"]) == 510


@requires_raw
def test_the_recorded_canonical_file_exists():
    canonical = REPO_ROOT / _manifest_rows()["canonical annotation file"]
    assert canonical.is_file(), f"manifest points at a missing file: {canonical}"


@requires_raw
def test_the_recorded_digest_matches_the_file_on_disk():
    """The manifest is a provenance claim; this is the check that it is true."""
    rows = _manifest_rows()
    canonical = REPO_ROOT / rows["canonical annotation file"]
    actual = hashlib.sha256(canonical.read_bytes()).hexdigest()
    assert actual == rows["SHA-256 of canonical file"]


@requires_raw
def test_the_recorded_revision_matches_the_local_checkout():
    rows = _manifest_rows()
    head = _git("-C", "data/raw/cuad", "rev-parse", "HEAD").stdout.strip()
    assert head == rows["upstream commit"], (
        "the checkout has moved since the manifest was written; re-run "
        "scripts/cuad_provenance.py and treat the previous digest as stale"
    )


@requires_raw
def test_the_canonical_file_is_the_cuad_annotation_set():
    rows = _manifest_rows()
    payload = json.loads((REPO_ROOT / rows["canonical annotation file"]).read_text("utf-8"))

    assert payload["version"] == rows["CUAD internal version"]
    assert len(payload["data"]) == int(rows["contracts in canonical file"])

    # A CUAD document is a title plus a list of labelled clauses. Spot-check the
    # shape rather than trusting the filename.
    document = payload["data"][0]
    assert set(document) >= {"title", "paragraphs"}
    assert document["paragraphs"], "a CUAD document must carry annotated paragraphs"
    assert {"qas", "context"} <= set(document["paragraphs"][0])


@requires_raw
def test_the_raw_directory_holds_the_archive_it_was_extracted_from():
    rows = _manifest_rows()
    assert (REPO_ROOT / rows["archive"]).is_file()
    assert (REPO_ROOT / rows["extracted under"]).is_dir()


@requires_raw
def test_the_manifest_does_not_claim_to_have_modified_upstream():
    """The record must state the immutability rule it is relying on."""
    text = MANIFEST.read_text(encoding="utf-8")
    assert "immutable" in text.lower()
    assert "not tracked by git" in text.lower() or "excluded from\nversion control" in text
