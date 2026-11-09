"""Recovery treatment through the real plan, rendering and artifact boundaries."""
import json

import pytest

from clean_v2_fixture import ROOT, answer, materials
from pilot01.config import ConditionConfig
from pilot01.experiment.artifacts import read_raw_run
from pilot01.experiment.batch import ExperimentRunner
from pilot01.model import ScriptedModelClient
from pilot01.experiment.console import Console
from pilot01.experiment.records import Records
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.runspec import RunPlan, RunPlanError, build_run_plan
from pilot01.runtime import ExperimentProfile
from pilot01.workflow.nodes.disclosure import VERIFICATION_CHECKLIST


def test_recovery_needs_access_and_verification():
    for access, verification in ((False, False), (False, True), (True, False)):
        with pytest.raises(ValueError, match="recovery requires"):
            ConditionConfig(source_access=access, verification_required=verification,
                            recovery_required=True)


def test_historical_conditions_omit_recovery_flag():
    for name in ("experiment_v2.yaml", "experiment_v3.yaml"):
        profile = ExperimentProfile(ROOT / "config" / name)
        assert all(not condition.recovery_required for condition in profile.conditions.conditions.values())
        assert "recovery_required" not in profile.conditions.model_dump_json()


def test_v4_plan_rendering_and_persistence(tmp_path):
    _, registry, _ = materials()
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    plan = build_run_plan(experiment_id="recovery-scripted", registry=registry,
                          conditions=profile.conditions, repeat_count=1, seed=17,
                          models=profile.models, runtime_config=profile.runtime,
                          profile_fingerprint=profile.fingerprint())
    assert len(plan.jobs) == 6
    assert plan.experiment_version == "4"
    plan.write_json(tmp_path / "plan.json")
    restored = RunPlan.load_json(tmp_path / "plan.json")
    assert restored.fingerprint() == plan.fingerprint()
    with pytest.raises(RunPlanError, match="runtime v4"):
        plan.model_copy(update={"runtime_config": profile.runtime.model_copy(update={"runtime_version": "3"})}).check(
            profile.conditions, registry)
    v2job = next(job for job in plan.jobs if job.condition_id == "A1V2D0")
    tampered = v2job.model_copy(update={"recovery_required": False})
    with pytest.raises(RunPlanError, match="recovery requirement"):
        plan.model_copy(update={"jobs": tuple(tampered if job == v2job else job for job in plan.jobs)}).check(
            profile.conditions, registry)

    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    final = json.loads(answer(pid))
    final["evidence_provenance"].update(opened_paragraph_ids=[], relied_paragraph_ids=[],
                                        verification_basis="upstream_only")
    final = json.dumps(final)
    scripts = {job.run_id: {role: (final,) for role in ("manager", "compliance")}
               for job in plan.jobs}
    def client_factory(*, role, job):
        client = ScriptedModelClient(scripts[job.run_id][role], tool_transport="native")
        client.estimate_request_tokens = lambda request: sum(len(message.content) for message in request.messages)
        return client

    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    runner = ExperimentRunner(plan=plan, registry=registry, conditions=profile.conditions,
                              workflow=profile.workflow, models=profile.models,
                              client_factory=client_factory,
                              paths=paths, profile=profile)
    batch = runner.run()
    assert len(batch.results) == 6
    rendered = {}
    for job in plan.jobs:
        raw = read_raw_run(paths.run_dir(job.run_id))
        assert not raw.artifact.failed
        expected_recovery = job.condition_id == "A1V2D0"
        assert raw.artifact.recovery_required == expected_recovery
        assert raw.artifact.execution.recovery_required == expected_recovery
        for role in ("manager", "compliance"):
            text = raw.model_calls_of(role)[0].rendered_input
            assert (VERIFICATION_CHECKLIST in text) == expected_recovery
            assert "Your objective is to recover" not in text
            assert "Do not assume that an omission exists" not in text
            assert ("you must search separately" in text) == job.verification_required
            for hidden in (job.condition_id, job.run_id, "recovery_required", "verification_required",
                           "gold_action", "gold_target_clause_status", "omission_target_claim_id"):
                assert hidden not in text
            rendered[(job.error_condition.value, job.condition_id, role)] = text
    for error in ("E0", "E1"):
        for role in ("manager", "compliance"):
            v1 = rendered[(error, "A1V1D0", role)]
            v2 = rendered[(error, "A1V2D0", role)]
            assert v2.count(VERIFICATION_CHECKLIST) == 1
            assert v2.replace("\n\n" + VERIFICATION_CHECKLIST, "", 1) == v1


def test_history_metadata_and_job_rows_preserve_recovery_level(tmp_path):
    _, registry, _ = materials()
    profile = ExperimentProfile(ROOT / "config/experiment_recovery_v4.yaml")
    plan = build_run_plan(experiment_id="recovery-history", registry=registry,
                          conditions=profile.conditions, repeat_count=1, seed=17,
                          models=profile.models, runtime_config=profile.runtime,
                          profile_fingerprint=profile.fingerprint())
    for index, job in enumerate(plan.jobs):
        assert Console.job_row(index, job)["v"] == job.condition_id[2:4]
    manifest = tmp_path / "manifests" / plan.experiment_id
    manifest.mkdir(parents=True)
    plan.write_json(manifest / "run_plan.json")
    (manifest / "console_session.json").write_text(json.dumps({"run_ids": plan.run_ids}), encoding="utf-8")
    records = Records(tmp_path, registry)
    metadata = records.list()[0]
    assert metadata["condition_values"]["v"] == ["V0", "V1", "V2"]
