"""Gate 3, group 8: the raw tree is the record, and the earlier gates still hold.

Two requirements, and they are close to the same requirement seen from two
sides.

**§10: processed results are rebuilt from raw artifacts alone.** The experiment
tree is split into ``raw/`` and ``processed/`` so that this is a safe, routine
operation rather than a nervous one. This file performs it: run a fixture batch,
score it, delete the processed tree, rebuild, compare. Byte identity is the
assertion, because anything weaker would let a rebuild that quietly changed a
number pass.

**§14: the earlier gates still hold.** Gate 3 added a layer above the workflow
and changed nothing below it -- but that is a claim about code, and what matters
is behaviour. The pre-Gate-3 suite is re-run as its own pytest invocation, so a
regression in already-accepted work is reported as one.

What is checked here:

* **The processed tree is a projection of the raw tree.** Delete it, rebuild it,
  get the same bytes -- with the manifest present, and then more sharply with
  nothing on disk but ``raw/``.
* **Scoring never writes to the raw tree.** A scoring and summary pass leaves
  every raw byte as it found it.
* **The raw tree is self-describing.** Copied to a fresh root, it scores the same
  rows: a run's identity and its plan fingerprint live in its own ``run.json``,
  not in the directory it happens to sit in.
* **A failed run is still an observation.** It comes back from the rebuild as a
  failed row rather than being dropped or quietly completed.
* **The experiment layer added no dependency.** Every module in it imports the
  standard library, pydantic, or this package -- no dataframe, no HTTP client,
  no provider SDK.
* **The 571 pre-Gate-3 tests still pass.**
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

import fixture_registry as fixture
import pilot01
from pilot01.experiment import ExperimentPaths, RunPlan, score_experiment, summarise

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

GATE_3_TEST_FILES = frozenset(
    {
        "test_experiment_artifacts.py",
        "test_experiment_batch.py",
        "test_experiment_cli.py",
        "test_experiment_gold_leakage.py",
        "test_experiment_plan.py",
        "test_experiment_scenarios.py",
        "test_experiment_scoring.py",
        "test_gate3_regression.py",
    }
)
"""Files added by Gate 3 -- excluded from the "earlier suite" re-run."""

PRE_GATE_3_TEST_FILES: tuple[str, ...] = tuple(
    sorted(
        path.name
        for path in TESTS_ROOT.glob("test_*.py")
        if path.name not in GATE_3_TEST_FILES
    )
)
"""Every test module that existed before Gate 3, discovered rather than listed."""

PRE_GATE_3_BASELINE = 571
"""Tests passing when Gate 3 began.

Asserted as a floor rather than an equality: adding a test to a pre-Gate 3
module is allowed, and losing one is not, so the number may rise but must never
fall.
"""

CONDITION_IDS = ("A1V0", "A1V1")
CASE_IDS = (fixture.CASE_A, fixture.CASE_B)

PLAN_DERIVED_QC = ("planned_runs", "missing_cells", "duplicate_cells", "plan_fingerprint")
"""§11's QC fields that need the plan, and are ``None`` without it."""


@lru_cache(maxsize=1)
def _registry():
    return fixture.build_registry()


def digest_tree(root: Path) -> dict[str, str]:
    """Every file under a tree, by relative path, hashed.

    Compared as a whole mapping rather than as a single digest so a mismatch
    names the file that changed.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_fixture_batch(root: Path, *, behaviour=None):
    """A small batch, with a real manifest beside it.

    Two cases, two governance conditions, both error conditions, one repeat:
    eight jobs. The plan is written to the manifest the way ``plan`` writes it,
    so the tree this file deletes and rebuilds is the tree an operator has.
    """
    plan = fixture.make_plan(
        registry=_registry(),
        condition_ids=CONDITION_IDS,
        case_ids=CASE_IDS,
    )
    paths = fixture.experiment_paths(root)
    paths.ensure()
    plan.write_json(paths.run_plan_json)
    fixture.execute(plan, _registry(), paths, behaviour=behaviour)
    return plan, paths


def rebuild_processed(paths: ExperimentPaths, *, plan=None) -> dict[str, str]:
    """The processed tree, rebuilt from ``raw/`` the way the CLI builds it.

    Mirrors ``_cmd_score`` followed by ``_cmd_summary``: re-score from raw, write
    the rows, then re-score again for the summary rather than reading the rows
    back. Re-scoring twice is not redundant -- it is the same claim as the one
    under test, applied to the summary as well.
    """
    scores = score_experiment(paths.raw_dir, registry=_registry())
    scores.write_jsonl(paths.run_scores_jsonl)
    scores.write_csv(paths.run_scores_csv)
    summary = summarise(
        score_experiment(paths.raw_dir, registry=_registry()),
        plan=plan,
        registry=_registry(),
    )
    summary.write_json(paths.summary_json)
    return digest_tree(paths.processed_dir)


# --------------------------------------------------------------------------
# §10: rebuild processed results from raw
# --------------------------------------------------------------------------


def test_the_fixture_batch_is_worth_rebuilding(tmp_path):
    """Guard the guard: one row per job, and not every row the same."""
    plan, paths = run_fixture_batch(tmp_path / "outputs")
    scores = score_experiment(paths.raw_dir, registry=_registry())

    assert len(scores) == len(plan.jobs) == 8
    assert all(row.completed for row in scores)
    # The manifest is real, not a fixture detail: the plan reads back off disk
    # as the plan that produced these runs.
    assert RunPlan.load_json(paths.run_plan_json).fingerprint() == plan.fingerprint()
    # Both conditions and both error conditions really do differ, so a rebuild
    # that collapsed them would be caught rather than passing on identical rows.
    assert {row.condition_id for row in scores} == set(CONDITION_IDS)
    assert {row.case_id for row in scores} == set(CASE_IDS)
    assert {row.error_condition.value for row in scores} == {"E0", "E1"}


def test_processed_results_are_rebuilt_from_raw_alone(tmp_path):
    """§10's five steps, with the manifest still present.

    1. execute fixture batch; 2. score it; 3. delete processed output;
    4. regenerate processed output; 5. byte-identical result.
    """
    plan, paths = run_fixture_batch(tmp_path / "outputs")

    first = rebuild_processed(paths, plan=plan)
    assert set(first) == {"run_scores.jsonl", "run_scores.csv", "experiment_summary.json"}

    # 3. Delete the processed output. Nothing else moves.
    raw_before = digest_tree(paths.raw_dir)
    shutil.rmtree(paths.processed_dir)
    assert not paths.processed_dir.exists()

    # 4. Regenerate it, from raw.
    second = rebuild_processed(paths, plan=plan)

    # 5. Byte-identical.
    assert second == first
    # ...and the raw tree was not part of the churn.
    assert digest_tree(paths.raw_dir) == raw_before


def test_the_rebuild_needs_nothing_but_the_raw_tree(tmp_path):
    """The sharp version: delete the manifest too, and the rows still come back.

    The scores are byte-identical because a run's identity, its cell and its
    plan fingerprint are all in its own ``run.json``. The *summary* is not
    byte-identical, and the difference is the point: §11's cell-completeness
    fields are the only things that need the plan, and without it they are
    ``None`` -- "the plan was not available to check against" -- rather than
    zero, which would be the false claim that no cell is missing.
    """
    plan, paths = run_fixture_batch(tmp_path / "outputs")
    first = rebuild_processed(paths, plan=plan)
    before = json.loads(paths.summary_json.read_text(encoding="utf-8"))
    assert before["runs"] == 8
    assert before["completed"] == 8
    assert before["qc"]["planned_runs"] == 8

    # Nothing survives but the raw tree: no plan, no plan CSV, no batch index.
    shutil.rmtree(paths.manifest_dir)
    shutil.rmtree(paths.processed_dir)
    assert not paths.run_plan_json.exists()

    second = rebuild_processed(paths, plan=None)

    for name in ("run_scores.jsonl", "run_scores.csv"):
        assert second[name] == first[name], name

    after = json.loads(paths.summary_json.read_text(encoding="utf-8"))

    # The plan-derived fields are the *only* difference. Everything a summary
    # can know from raw artifacts alone comes back identical.
    assert {k: v for k, v in after.items() if k != "qc"} == {
        k: v for k, v in before.items() if k != "qc"
    }
    assert {k: v for k, v in after["qc"].items() if k not in PLAN_DERIVED_QC} == {
        k: v for k, v in before["qc"].items() if k not in PLAN_DERIVED_QC
    }
    for field in PLAN_DERIVED_QC:
        assert after["qc"][field] is None, field


def test_scoring_never_writes_to_the_raw_tree(tmp_path):
    """The raw tree is the record; a projection must not edit it.

    Checked with digests over every raw file rather than over the run
    directories, so an added, removed or rewritten file all count.
    """
    plan, paths = run_fixture_batch(tmp_path / "outputs")
    before = digest_tree(paths.raw_dir)
    assert before

    rebuild_processed(paths, plan=plan)
    rebuild_processed(paths, plan=None)

    assert digest_tree(paths.raw_dir) == before


def test_two_scoring_passes_agree_row_for_row(tmp_path):
    """Determinism, independent of the bytes on disk.

    Byte identity is checked above; this checks the rows themselves, so a change
    that made the writer stable while the scorer was not would still be caught.
    """
    _, paths = run_fixture_batch(tmp_path / "outputs")

    first = score_experiment(paths.raw_dir, registry=_registry())
    second = score_experiment(paths.raw_dir, registry=_registry())

    assert [row.run_id for row in first] == [row.run_id for row in second]
    assert [row.model_dump() for row in first] == [row.model_dump() for row in second]
    # Sorted by run id, so two passes cannot differ by iteration order alone.
    assert [row.run_id for row in first] == sorted(row.run_id for row in first)


def test_the_raw_tree_can_be_copied_and_scores_the_same_rows(tmp_path):
    """A run directory is self-describing.

    Copied to a fresh root with a different experiment id on the path, the rows
    are the same rows: no absolute path, no dependence on the manifest, and no
    dependence on the order the directories were created in.
    """
    _, paths = run_fixture_batch(tmp_path / "outputs")
    original = [row.model_dump() for row in score_experiment(paths.raw_dir, registry=_registry())]

    moved = tmp_path / "elsewhere" / "raw" / paths.experiment_id
    shutil.copytree(paths.raw_dir, moved)

    copied = [row.model_dump() for row in score_experiment(moved, registry=_registry())]
    assert copied == original
    # The copied tree is not empty and holds no absolute reference to its origin.
    assert copied
    for path in moved.rglob("*.json"):
        assert str(tmp_path) not in path.read_text(encoding="utf-8"), path.name


def test_a_failed_run_survives_the_rebuild_as_a_failure(tmp_path):
    """§7: a run that died is not a wrong answer, and it is not nothing either.

    The manager is scripted to cite a paragraph that exists in no contract. Under
    A1V1 the runtime refuses the citation and the run fails; under A1V0 nothing
    checks it, so the same citation is merely foreign evidence. Both are
    observations, both are scored, and the rebuild returns them unchanged.
    """

    def cite_a_paragraph_that_does_not_exist(job, registry):
        if not job.verification_required:
            return fixture.default_responses(job, registry)
        return fixture.scripted_responses(
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=fixture.OPENABLE_PARAGRAPH[job.case_id],
            manager_evidence=("PARAGRAPH-NOT-IN-ANY-CONTRACT",),
        )

    plan, paths = run_fixture_batch(
        tmp_path / "outputs", behaviour=cite_a_paragraph_that_does_not_exist
    )
    first = score_experiment(paths.raw_dir, registry=_registry())

    failed = [row for row in first if not row.completed]
    completed = [row for row in first if row.completed]
    assert failed and completed, "the fixture must produce both kinds of row"
    for row in failed:
        assert row.condition_id == "A1V1"
        assert row.protocol_failure is True
        # No final decision, so correctness is unknown rather than wrong.
        assert row.final_action_correct is None
        assert any("no final decision" in note for note in row.notes)
    # The A1V0 runs of the same script completed: nothing checked the citation.
    assert {row.condition_id for row in completed} == {"A1V0"}

    shutil.rmtree(paths.processed_dir)
    second = score_experiment(paths.raw_dir, registry=_registry())

    assert [row.model_dump() for row in second] == [row.model_dump() for row in first]


# --------------------------------------------------------------------------
# The experiment layer added no dependency
# --------------------------------------------------------------------------


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, by parsing it rather than importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_the_experiment_layer_imports_nothing_new(tmp_path):
    """Gate 3 added infrastructure, not dependencies.

    A serial batch runner needs a scheduler, a dataframe and an HTTP client about
    equally badly: not at all. The experiment package is held to the standard
    library, pydantic, and this package. A provider SDK or a plotting library
    arriving here would mean the layer had started doing something the gate did
    not ask for.
    """
    package = Path(pilot01.__file__).resolve().parent / "experiment"
    allowed_third_party = {"pydantic"}

    offenders: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*.py")):
        for name in imported_modules(path):
            root = name.split(".")[0]
            if root in sys.stdlib_module_names or root == "pilot01":
                continue
            if root in allowed_third_party:
                continue
            offenders.append((path.name, name))

    assert not offenders, offenders
    # ...and the scan found something to scan.
    assert (package / "scoring.py").exists()
    assert "pydantic" in imported_modules(package / "scoring.py")


# --------------------------------------------------------------------------
# §14: the earlier gates still hold
# --------------------------------------------------------------------------


def test_the_pre_gate_3_suite_is_discovered_and_non_trivial():
    """Guard the guard: a re-run of nothing would pass trivially."""
    assert PRE_GATE_3_TEST_FILES
    assert "test_isolation.py" in PRE_GATE_3_TEST_FILES
    assert "test_gate25_regression.py" in PRE_GATE_3_TEST_FILES
    assert not (set(PRE_GATE_3_TEST_FILES) & GATE_3_TEST_FILES)


def test_every_pre_gate_3_test_still_passes():
    """Gate 1 through Gate 2.5, re-run as its own pytest invocation.

    A separate process on purpose: a failure inside the earlier suite is a
    regression in work that was already accepted, and running it here means it is
    reported as one -- with the earlier suite's own output -- rather than folded
    into a Gate 3 failure.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *[str(TESTS_ROOT / name) for name in PRE_GATE_3_TEST_FILES],
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the pre-Gate-3 suite no longer passes:\n{result.stdout[-4000:]}"
    )
    assert " passed" in result.stdout, result.stdout[-2000:]
    reported = int(result.stdout.rsplit(" passed", 1)[0].split()[-1])
    assert reported >= PRE_GATE_3_BASELINE, (
        f"{reported} pre-Gate-3 tests passed, down from the {PRE_GATE_3_BASELINE} "
        "recorded when Gate 3 began: a test was lost rather than kept passing"
    )
