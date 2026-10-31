"""Actual matrix enumeration and scripted agent execution; no paid calls."""
import json
from pathlib import Path

import pytest

from clean_v2_fixture import ROOT, materials, answer, script, request
from pilot01.runtime import ExperimentProfile
from pilot01.schemas import ErrorCondition
from pilot01.experiment.design import ExperimentDesign
from pilot01.experiment.development.neutral_materials import verify_ready_registry
from pilot01.experiment.console import Console
from pilot01.experiment.runspec import build_run_plan, RunPlan, RunPlanError
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.artifacts import read_raw_run


@pytest.fixture(scope="module")
def real_design():
    folder = ROOT / "data/pilot01/clean80_v1"
    registry = verify_ready_registry(folder / "case_registry_v2.json")
    cohorts = json.loads((folder / "researcher_cohorts.json").read_text(encoding="utf-8"))
    return ExperimentDesign(registry, registry.case_ids, cohorts)


@pytest.mark.parametrize("selection,total,contracts,memos", [
    ({}, 1296, 80, 144),
    ({"repeat": 3}, 3888, 80, 144),
    ({"cohort": "old40"}, 648, 40, 72),
    ({"cohort": "new40"}, 648, 40, 72),
    ({"e": ["E0"]}, 720, 80, 80),
    ({"e": ["E1"]}, 576, 64, 64),
    ({"cohort": "new40", "a": ["A1"], "v": ["V0"], "d": ["D2"], "e": ["E1"], "repeat": 3}, 96, 32, 32),
])
def test_actual_80_case_denominators(real_design, selection, total, contracts, memos):
    result = real_design.estimate(selection)
    assert (result["total"], result["contracts"], result["memo_count"]) == (total, contracts, memos)
    assert all(not c.startswith("A0V1") for c in result["conditions"])
    assert result["valid"]


@pytest.mark.parametrize("selection", [
    {"a": []}, {"v": ["V01"]}, {"d": ["D0", "D0"]}, {"e": "E0"},
    {"cohort": []}, {"cohort": "secret"}, {"repeat": True}, {"repeat": 0},
    {"case_ids": ["../../config/api.local.env"]}, {"conditions": ["D0"]},
])
def test_invalid_selection_never_silently_uses_defaults(real_design, selection):
    with pytest.raises(ValueError):
        real_design.estimate(selection)


def test_a0v1_is_explicitly_excluded(real_design):
    result = real_design.estimate({"a": ["A0"], "v": ["V1"]})
    assert result["total"] == 0 and not result["valid"]
    assert result["excluded_conditions"] == ["A0V1D0", "A0V1D1", "A0V1D2"]


def test_new_material_preview_matches_frozen_source_and_memos(real_design):
    rows = real_design.catalog("new40")
    assert len(rows) == 40 and sum(len(r["memo_variants"]) for r in rows) == 72
    for row in rows:
        result = real_design.preview(row["case_id"])
        assert result["contract_text"] == real_design.registry.get(row["case_id"]).contract_text
        assert set(result["memos"]) == set(row["memo_variants"])
        for error, memo in result["memos"].items():
            assert memo == real_design.registry.memo(row["case_id"], ErrorCondition(error)).model_dump(mode="json")
            assert len(memo["claims"]) == 5
        assert "gold" not in result
    with pytest.raises(ValueError):
        real_design.preview("../../config/api.local.env")


def test_e1_plan_is_complete_for_selected_arms_and_rejects_e0_injection(real_design, tmp_path):
    profile = ExperimentProfile(ROOT / "config/experiment_v3.yaml")
    selection = real_design.estimate({"cohort": "new40", "e": ["E1"], "repeat": 3})
    plan = build_run_plan(experiment_id="factorial-enumeration", registry=real_design.registry,
                          conditions=profile.conditions, repeat_count=3, seed=17, models=profile.models,
                          condition_ids=selection["conditions"], case_ids=selection["case_ids"],
                          runtime_config=profile.runtime, profile_fingerprint=profile.fingerprint(),
                          error_conditions=selection["e"])
    assert len(plan.jobs) == 864
    assert not plan.missing_cells(real_design.registry)
    assert not plan.duplicate_cells()
    assert all(j.error_condition == ErrorCondition.E1 for j in plan.jobs)
    plan.write_json(tmp_path / "plan.json")
    restored = RunPlan.load_json(tmp_path / "plan.json")
    assert restored.fingerprint() == plan.fingerprint()
    assert restored.experiment_version == "3"
    with pytest.raises(RunPlanError):
        restored.model_copy(update={"selected_error_conditions": (ErrorCondition.E0,)}).check(profile.conditions, real_design.registry)


def test_real_runner_honors_access_requirement_and_disclosure_without_gating(tmp_path):
    _, registry, _ = materials()
    profile = ExperimentProfile(ROOT / "config/experiment_v3.yaml")
    plan = build_run_plan(experiment_id="factorial-scripted", registry=registry, conditions=profile.conditions,
                          repeat_count=1, seed=17, models=profile.models, runtime_config=profile.runtime,
                          profile_fingerprint=profile.fingerprint())
    assert len(plan.jobs) == 18
    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    final = json.loads(answer(pid))
    final["evidence_provenance"].update(opened_paragraph_ids=[], relied_paragraph_ids=[], verification_basis="upstream_only")
    final = json.dumps(final)
    scripts = {j.run_id: {role: (final,) for role in ("manager", "compliance")} for j in plan.jobs}
    # Even a model requesting a forbidden source in A0 cannot get contract text.
    a0job = next(j for j in plan.jobs if j.condition_id == "A0V0D0" and j.error_condition == ErrorCondition.E0)
    scripts[a0job.run_id]["manager"] = (request("search_contract", query="control"), final)
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    runner = ExperimentRunner(plan=plan, registry=registry, conditions=profile.conditions,
                              workflow=profile.workflow, models=profile.models,
                              client_factory=ScriptedClientFactory(scripts), paths=paths, profile=profile)
    batch = runner.run()
    assert len(batch.results) == 18
    initial = {}
    for job in plan.jobs:
        raw = read_raw_run(paths.run_dir(job.run_id))
        assert not raw.artifact.failed
        assert raw.artifact.execution.compliance_output is not None
        assert all(not check.validation_pass for check in raw.verifications)
        for role in ("manager", "compliance"):
            text = raw.model_calls_of(role)[0].rendered_input
            assert job.condition_id not in text and job.run_id not in text and "verification_required" not in text
            assert ("you must search separately" in text) == job.verification_required
            assert ("will be checked" in text) == (job.condition_id[-2:] != "D0")
            if not job.source_access:
                assert "No source tools are available" in text
            initial[(job.error_condition.value, job.condition_id[:4], job.condition_id[-2:], role)] = text.split("\n\nSUBMISSION NOTICE\n")[0]
    denied = read_raw_run(paths.run_dir(a0job.run_id)).tool_calls_of("manager")
    assert len(denied) == 1 and not denied[0].ok and not denied[0].result_ids
    for av in ("A0V0", "A1V0", "A1V1"):
        for error in ("E0", "E1"):
            # Compliance receives a common scripted handoff in every D group.
            for role in ("manager", "compliance"):
                assert len({initial[(error, av, d, role)] for d in ("D0", "D1", "D2")}) == 1


def test_console_saves_factors_and_never_creates_a_plan_for_a0v1(tmp_path, real_design):
    cohorts = [dict(case_id=cid, cohort="old40_development" if cohort == "old40" else "new40_main")
               for cid, cohort in real_design.cohort.items()]
    folder = tmp_path / "data/pilot01/clean80_v1"
    folder.mkdir(parents=True)
    (folder / "researcher_cohorts.json").write_text(json.dumps(cohorts))
    app = Console(root=tmp_path, registry=real_design.registry,
                  profile=ExperimentProfile(ROOT / "config/experiment_v3.yaml"),
                  credential_loader=lambda: [])
    assert app.config()["contracts"] == 80
    assert [r["contracts"] for r in app.config()["cohorts"]] == [80, 40, 40]
    result = app.prepare({"cohort": "new40", "e": ["E1"], "a": ["A1"], "v": ["V1"], "d": ["D2"]})
    assert result["total"] == 32
    assert all(j["a"] == "A1" and j["v"] == "V1" and j["d"] == "D2" and j["memo"] == "E1" for j in result["jobs"])
    with pytest.raises(ValueError):
        app.prepare({"a": ["A0"], "v": ["V1"]})
    assert not list(app.output.rglob("run.json"))
