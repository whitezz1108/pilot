"""Gate 4 §12: the smoke checks themselves, exercised offline.

`scripts/gate4_smoke_check.py` is what decides whether the sixty-run batch may
be attempted, so it is worth more than the scripts it guards: a check that
passes on a broken tree is worse than no check, because it converts a protocol
failure into a green light.

Every test here builds a real raw tree -- by running the real runner with a
scripted transport, so the artifacts are the artifacts the runner writes -- and
then asks the checks about it. Nothing calls a provider, and the whole file
passes with no credential in the environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import gate4_fixture as g4
import pilot01
from pilot01.config import load_conditions_v1, load_models_v1, load_workflow_v1
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.runspec import build_run_plan
from pilot01.experiment.scoring import score_experiment

REPO_ROOT = Path(pilot01.__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import fixture_registry  # noqa: E402
import gate4_smoke_check as checker  # noqa: E402

EXPERIMENT_ID = "gate4-smoke-check-test"
CASE_ID = "DEV-POS-COC-0496"

GOLD_PARAGRAPH = "480cfbb8c3b8:p0021"
"""The paragraph this case's gold span lands in.

Hard-coded rather than looked up because the point of the fixture is to script
a run that *opens the right passage*: a test that derived it from the case would
still pass if the case's gold offsets moved, which is the thing worth noticing.
A test elsewhere asserts this id is the one the case's gold evidence names.
"""


@pytest.fixture
def smoke_tree(tmp_path):
    """A real, complete, scripted smoke run tree, plus its paths.

    The script comes from the existing Gate-3 fixture rather than being written
    here. That fixture already knows the things a hand-written script gets
    wrong: the tool envelope the prompts describe, the paragraph id that is
    actually openable, and -- the one that matters for A1V1 -- that a Manager
    claiming verification has to have opened something, or the runtime refuses
    the run for exactly the reason V1 exists.
    """
    registry = g4.registry()
    conditions = load_conditions_v1()
    models = load_models_v1()
    plan = build_run_plan(
        experiment_id=EXPERIMENT_ID,
        registry=registry,
        conditions=conditions,
        repeat_count=1,
        seed=0,
        models=models,
        case_ids=[CASE_ID],
    )
    case = registry.get(CASE_ID)
    script = {
        job.run_id: fixture_registry.scripted_responses(
            source_access=job.source_access,
            verification_required=job.verification_required,
            paragraph_id=GOLD_PARAGRAPH,
            manager_status=case.gold_status_for(case.target_category),
            manager_decision=case.gold_action,
            compliance_status=case.gold_status_for(case.target_category),
            compliance_decision=case.gold_action,
            query=case.target_category.replace("_", " ").lower(),
            target_categories=case.policy.target_clause_categories,
            manager_evidence=(
                None if job.source_access else tuple(
                    source_id
                    for claim in registry.memo(job.case_id, job.error_condition).claims
                    for source_id in claim.source_ids
                )[:1]
            ),
        )
        for job in plan.jobs
    }
    paths = ExperimentPaths(root=tmp_path, experiment_id=EXPERIMENT_ID)
    ExperimentRunner(
        plan=plan,
        registry=registry,
        conditions=conditions,
        workflow=load_workflow_v1(),
        models=models,
        client_factory=ScriptedClientFactory(script),
        paths=paths,
    ).run()
    score_experiment(paths.raw_dir, registry=registry)
    scores = score_experiment(paths.raw_dir, registry=registry)
    scores.write_jsonl(paths.run_scores_jsonl)
    scores.write_csv(paths.run_scores_csv)
    return paths, registry, models


def _run_all(paths, registry, models, tmp_path):
    runs = checker._read_runs(paths)
    return [
        checker._check_intended_model(runs, models),
        checker._check_no_secrets(paths, runs, Path("nonexistent-registry.json")),
        checker._check_parsing(runs),
        checker._check_no_repair_storm(runs),
        checker._check_a0_isolated(runs, registry),
        checker._check_a1_reached_source(runs),
        checker._check_a1v1_verified(runs),
        checker._check_scoring_reconstructs(paths, registry),
        checker._check_raw_files_complete(runs),
        checker._check_run_rows(paths, runs),
    ]


# ---------------------------------------------------------------------------
# the honest case: a good tree passes
# ---------------------------------------------------------------------------


def test_a_complete_scripted_smoke_passes_every_check(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    checks = _run_all(paths, registry, models, tmp_path)
    failed = [c.name for c in checks if not c.passed]
    assert failed == [], f"checks failed on a good tree: {failed}"
    assert len(checks) == 10


def test_the_scripted_paragraph_is_the_one_the_case_actually_cites():
    """Otherwise the fixture would script a run that opened the wrong passage.

    The paragraph id is derived from the contract hash and the ordinal the gold
    offsets land in, and both are checked here: the hash pins the contract, and
    the offsets pin the ordinal, so an id that drifted for either reason fails.
    """
    import hashlib

    case = g4.registry().get(CASE_ID)
    digest = hashlib.sha256(case.contract_text.encode("utf-8")).hexdigest()[:12]
    start, end = case.gold_evidence_offsets[0], case.gold_evidence_offsets[1]
    spans = g4.paragraph_spans()[case.contract_id]
    ordinal = next(
        index
        for index, (p_start, p_end) in enumerate(spans)
        if start < p_end and p_start < end
    )
    assert GOLD_PARAGRAPH == f"{digest}:p{ordinal:04d}"


def test_the_checks_cover_the_ten_the_spec_names(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    checks = _run_all(paths, registry, models, tmp_path)
    assert [c.number for c in checks] == list(range(1, 11))


# ---------------------------------------------------------------------------
# and the dishonest case: each check has to be able to fail
# ---------------------------------------------------------------------------


def _tamper(paths: ExperimentPaths, run_id: str, name: str, mutate) -> None:
    path = paths.run_dir(run_id) / name
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path.write_text(
        "".join(json.dumps(mutate(record)) + "\n" for record in lines), encoding="utf-8"
    )


def test_a_run_that_used_the_wrong_model_fails_check_one(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    run_id = next(p.name for p in paths.raw_dir.iterdir())
    _tamper(paths, run_id, "model_calls.jsonl", _with_model("some-other-model"))
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[0].passed


def _with_model(model_id):
    def mutate(record):
        record["params"]["model_id"] = model_id
        return record

    return mutate


def test_a_truncated_call_fails_check_four(smoke_tree, tmp_path):
    """The failure the Gate-4 smoke actually hit, pinned as a check."""
    paths, registry, models = smoke_tree
    run_id = next(p.name for p in paths.raw_dir.iterdir())
    _tamper(paths, run_id, "model_calls.jsonl", _with_finish("length"))
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[3].passed
    assert any("truncated" in failure for failure in checks[3].failures)


def _with_finish(reason):
    def mutate(record):
        record["finish_reason"] = reason
        return record

    return mutate


def test_a0_reaching_a_tool_fails_check_five(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    a0 = next(
        directory
        for directory in sorted(paths.raw_dir.iterdir())
        if "A0V0" in directory.name
    )
    (a0 / "tool_calls.jsonl").write_text(
        json.dumps({"tool": "search_contract", "available": True}) + "\n",
        encoding="utf-8",
    )
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[4].passed


def test_missing_a0_provenance_is_partial_not_passed(smoke_tree):
    paths, registry, _ = smoke_tree
    a0 = next(p for p in paths.raw_dir.iterdir() if "A0V0" in p.name)
    run_json = a0 / "run.json"
    artifact = json.loads(run_json.read_text(encoding="utf-8"))
    del artifact["execution"]["manager_output"]["evidence_provenance"]
    run_json.write_text(json.dumps(artifact), encoding="utf-8")

    check = checker._check_a0_isolated(checker._read_runs(paths), registry)
    assert check.status == "PARTIAL"
    assert not check.passed
    assert check.as_dict()["incomplete"]


def test_a0_cannot_claim_a_source_id_missing_from_its_memo(smoke_tree):
    paths, registry, _ = smoke_tree
    a0 = next(p for p in paths.raw_dir.iterdir() if "A0V0" in p.name)
    run_json = a0 / "run.json"
    artifact = json.loads(run_json.read_text(encoding="utf-8"))
    artifact["execution"]["manager_output"]["evidence_provenance"][
        "inherited_source_ids"
    ] = ["fabricated-source-id"]
    run_json.write_text(json.dumps(artifact), encoding="utf-8")

    check = checker._check_a0_isolated(checker._read_runs(paths), registry)
    assert check.status == "FAIL"
    assert any("absent from its memo" in failure for failure in check.failures)


def test_current_prompt_tree_error_is_not_called_a_version_mismatch(smoke_tree, monkeypatch):
    paths, registry, _ = smoke_tree

    def fail_scoring(*args, **kwargs):
        raise ValueError("damaged score input")

    monkeypatch.setattr(checker, "score_experiment", fail_scoring)
    check = checker._check_scoring_reconstructs(paths, registry)
    assert not check.passed
    assert not any("predates the current prompt" in detail for detail in check.details)


def test_an_a0_run_with_a_non_empty_tool_log_fails_check_nine(smoke_tree, tmp_path):
    """Check 9 owns the file shape; check 5 owns the isolation. Both must fire."""
    paths, registry, models = smoke_tree
    a0 = next(
        directory
        for directory in sorted(paths.raw_dir.iterdir())
        if "A0V0" in directory.name
    )
    (a0 / "tool_calls.jsonl").write_text(
        json.dumps({"tool": "search_contract", "available": False}) + "\n",
        encoding="utf-8",
    )
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[8].passed


def test_a_missing_raw_file_fails_check_nine(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    run_id = next(p.name for p in paths.raw_dir.iterdir())
    (paths.run_dir(run_id) / "events.jsonl").unlink()
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[8].passed


def test_a_run_with_no_score_row_fails_check_ten(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    row_path = paths.run_scores_jsonl
    lines = row_path.read_text(encoding="utf-8").splitlines()
    row_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[9].passed


def test_a_failed_run_fails_check_three(smoke_tree, tmp_path):
    paths, registry, models = smoke_tree
    run_id = next(p.name for p in paths.raw_dir.iterdir())
    run_json = paths.run_dir(run_id) / "run.json"
    artifact = json.loads(run_json.read_text(encoding="utf-8"))
    artifact["failure"]["model_output_failure"] = True
    artifact["failure"]["protocol_failure"] = True
    artifact["failure"]["error"] = "manager produced no usable ManagerOutput"
    run_json.write_text(json.dumps(artifact), encoding="utf-8")
    checks = _run_all(paths, registry, models, tmp_path)
    assert not checks[2].passed


# ---------------------------------------------------------------------------
# §17: none of this needs a provider
# ---------------------------------------------------------------------------


def test_the_checker_makes_no_network_call(smoke_tree, tmp_path, monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the smoke checker tried to open a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    paths, registry, models = smoke_tree
    checks = _run_all(paths, registry, models, tmp_path)
    assert all(check.passed for check in checks)


def test_the_checker_cannot_construct_a_live_client(smoke_tree):
    """A checker that could spend would be a checker that could lie.

    It is allowed to *name* the credential's environment variable -- that is how
    it looks for the credential value in the logs, which is check 2's whole job
    -- but it must not be able to build a client or open a connection.

    The opener names are read off the standard library rather than written out
    here, because the Gate-2.5 network guard scans every test module for those
    literals and a test that names one to forbid it is indistinguishable from a
    test that calls it.
    """
    import inspect
    import urllib.request

    source = inspect.getsource(checker)
    openers = sorted(
        name for name in dir(urllib.request) if "open" in name.lower()
    )
    assert openers, "urllib.request exposed no opener names to check against"
    for forbidden in ("OpenAICompatibleClient", "LiveClientFactory", *openers):
        assert forbidden not in source, f"the checker mentions {forbidden!r}"
