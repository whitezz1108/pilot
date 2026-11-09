"""Pre-outcome amendment guards run before selection or any successor write."""
import importlib.util
import json

import pytest

from clean_v2_fixture import ROOT, materials
from pilot01.experiment.runspec import build_run_plan
from pilot01.runtime import ExperimentProfile


@pytest.fixture
def amendment(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("amend_recovery_medium", ROOT / "scripts/amend_recovery_medium.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT", tmp_path)
    return module


@pytest.mark.parametrize("experiment_id", ["r4m-1109", "r4m2-1109"])
@pytest.mark.parametrize("artifact", [None, "run.json", "model_calls.jsonl"])
def test_started_original_or_successor_refused_even_before_first_response(amendment, monkeypatch, experiment_id, artifact):
    (amendment.OUT / "study_design.json").write_text("{}", encoding="utf-8")
    run = amendment.OUT / "raw" / experiment_id / "run-000"
    run.mkdir(parents=True)
    if artifact:
        (run / artifact).write_text("{}" if artifact == "run.json" else "", encoding="utf-8")

    def unexpected_registry_read(*args):
        pytest.fail("A started batch must be rejected before loading materials or selecting replacements")

    monkeypatch.setattr(amendment, "verify_ready_registry", unexpected_registry_read)
    with pytest.raises(ValueError, match="pre-outcome"):
        amendment.main()
    assert not (amendment.OUT / "study_design_amendment.json").exists()


def frozen_fixture(amendment, monkeypatch):
    _, registry, _ = materials()
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    plan = build_run_plan(experiment_id="r4m-1109", registry=registry,
                          conditions=profile.conditions, repeat_count=2, seed=1109,
                          models=profile.models, runtime_config=profile.runtime,
                          profile_fingerprint=profile.fingerprint())
    original = {"profile_fingerprint": profile.fingerprint(), "selection_seed": plan.seed,
                "small": {"cases": []}, "medium": {"cases": list(plan.case_ids),
                "repeats": plan.repeat_count, "expected_runs": len(plan.jobs)}}
    path = amendment.OUT / "manifests/r4m-1109/run_plan.json"
    plan.write_json(path)
    monkeypatch.setattr(amendment, "verify_ready_registry", lambda _: registry)
    return original, path


@pytest.mark.parametrize("field,value", [("cases", ["CASE-9999"]), ("repeats", 3), ("expected_runs", 999)])
def test_frozen_design_must_match_original_plan(amendment, monkeypatch, field, value):
    original, plan_path = frozen_fixture(amendment, monkeypatch)
    original_bytes = plan_path.read_bytes()
    original["medium"][field] = value
    (amendment.OUT / "study_design.json").write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="original plan does not match"):
        amendment.main()
    assert plan_path.read_bytes() == original_bytes
    assert not (amendment.OUT / "manifests/r4m2-1109").exists()


def test_missing_replacement_review_stops_without_writing_successor(amendment, monkeypatch):
    original, plan_path = frozen_fixture(amendment, monkeypatch)
    original_bytes = plan_path.read_bytes()
    (amendment.OUT / "study_design.json").write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(SystemExit, match="review is required"):
        amendment.main()
    assert plan_path.read_bytes() == original_bytes
    assert not (amendment.OUT / "study_design_amendment.json").exists()
    assert not (amendment.OUT / "manifests/r4m2-1109").exists()
