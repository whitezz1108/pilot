import json
from pathlib import Path

import pytest

from clean_v2_fixture import materials, profile, script, answer, request, TARGETS
from pilot01.experiment.runspec import build_run_plan, RunPlanError
from pilot01.experiment.batch import ExperimentRunner, ScriptedClientFactory
from pilot01.experiment.layout import ExperimentPaths
from pilot01.experiment.artifacts import read_raw_run
from pilot01.experiment.scoring_v2 import analyze_plan
from pilot01.workflow.nodes.disclosure import render_disclosure


def execute(tmp_path, *, statuses=None, missing=False, direct_open=False, malformed=False, malformed_compliance=False):
    _, registry, _ = materials()
    p = profile()
    plan = build_run_plan(experiment_id="clean-fixture", registry=registry, conditions=p.conditions,
                          repeat_count=1, seed=17, models=p.models, runtime_config=p.runtime,
                          profile_fingerprint=p.fingerprint())
    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    responses = {j.run_id: {role: script(pid, (statuses or {}).get(j.condition_id, "present"), missing=missing)
                            for role in ("manager", "compliance")} for j in plan.jobs}
    if direct_open:
        responses = {j.run_id: {role: (request("open_source_span", paragraph_id=pid), answer(pid))
                                for role in ("manager", "compliance")} for j in plan.jobs}
    if malformed:
        responses = {j.run_id: {role: ("not JSON", "still not JSON")
                                for role in ("manager", "compliance")} for j in plan.jobs}
    if malformed_compliance:
        for roles in responses.values():
            roles["compliance"] = ("not JSON", "still not JSON")
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    runner = ExperimentRunner(plan=plan, registry=registry, conditions=p.conditions, workflow=p.workflow,
                              models=p.models, client_factory=ScriptedClientFactory(responses), paths=paths, profile=p)
    batch = runner.run()
    return plan, registry, paths, batch


def test_real_two_node_flow_six_cells_and_disclosure_only(tmp_path):
    plan, registry, paths, batch = execute(tmp_path)
    assert len(batch.results) == 6
    raw = {j.cell: read_raw_run(paths.run_dir(j.run_id)) for j in plan.jobs}
    for error in ("E0", "E1"):
        initial = []
        for d in ("D0", "D1", "D2"):
            r = raw[("CASE-0001", error, d, 0)]
            assert not r.artifact.failed
            assert r.artifact.validator_version == "validator_v3"
            assert r.artifact.runtime_config["max_tool_requests_per_node"] == 16
            assert r.artifact.runtime_config["verification_mode"] == "audit_only"
            assert r.artifact.disclosure_level == d
            assert r.artifact.model_calls == 8 and r.artifact.tool_calls == 6
            assert all(c.params.max_output_tokens == 32768 and c.params.timeout_seconds == 360 for c in r.model_calls)
            first = r.model_calls_of("manager")[0].rendered_input
            initial.append(first.split("\n\nSUBMISSION NOTICE\n")[0])
            assert "PILOT-POS" not in first and "verification_required" not in first
            assert "max" not in first.lower() or "most 16 tools" in first
            if d == "D0":
                assert "will be checked" not in first
            if d == "D1":
                assert render_disclosure("D1") in first and "Perform a successful search tagged" not in first
            if d == "D2":
                assert render_disclosure("D2") in first
            for role in ("manager", "compliance"):
                assert len(r.tool_calls_of(role)) == 3
                assert "remaining: 13" in r.model_calls_of(role)[-1].rendered_input
        assert initial[0] == initial[1] == initial[2]
    rows, report = analyze_plan(plan, registry, paths.raw_dir)
    assert report["overall"]["planned"] == 6 and report["overall"]["correct_actions_delivered"] == 6
    assert report["batch_complete"]


def test_missing_target_is_audited_and_both_nodes_finish(tmp_path):
    plan, registry, paths, _ = execute(tmp_path, missing=True)
    for job in plan.jobs:
        r = read_raw_run(paths.run_dir(job.run_id))
        assert not r.artifact.failed and r.artifact.execution.manager_output is not None
        assert r.artifact.execution.compliance_output is not None
        assert r.model_calls_of("compliance")
        assert r.model_calls[-1].parsed_output is not None
        assert not r.verifications[-1].validation_pass
    rows, report = analyze_plan(plan, registry, paths.raw_dir)
    assert report["overall"]["correct_actions_delivered"] == 6
    assert report["overall"]["failures"] == {}
    assert report["overall"]["validation_by_node"]["manager"] == {"evaluated": 6, "passed": 0, "failed": 6}


def test_direct_open_without_search_is_recorded_but_never_blocks_or_feedbacks(tmp_path):
    plan, registry, paths, _ = execute(tmp_path, direct_open=True)
    for job in plan.jobs:
        r = read_raw_run(paths.run_dir(job.run_id))
        assert not r.artifact.failed
        for role in ("manager", "compliance"):
            verdict = r.verifications_of(role)[-1]
            assert not verdict.validation_pass
            assert [f.value for f in verdict.audit_failures] == ["target_not_searched"]
            assert r.artifact.execution.model_dump()[role + "_output"] is not None
            assert len(r.model_calls_of(role)) == 2
            for call in r.model_calls_of(role):
                assert "target_not_searched" not in call.rendered_input
                assert "missing successful target-tagged search" not in call.rendered_input
        handoff = r.model_calls_of("compliance")[0].rendered_input
        assert "validation_pass" not in handoff and "audit_failures" not in handoff
    rows, report = analyze_plan(plan, registry, paths.raw_dir)
    assert all(r["execution_status"] == "completed" and r["failure_category"] is None for r in rows)
    assert report["overall"]["correct_actions_delivered"] == 6
    for role in ("manager", "compliance"):
        assert report["overall"]["validation_by_node"][role] == {"evaluated": 6, "passed": 0, "failed": 6}


def test_unparseable_output_still_records_execution_failure(tmp_path):
    plan, registry, paths, _ = execute(tmp_path, malformed=True)
    for job in plan.jobs:
        r = read_raw_run(paths.run_dir(job.run_id))
        assert r.artifact.failed
        assert not r.model_calls_of("compliance")
    _, report = analyze_plan(plan, registry, paths.raw_dir)
    assert report["overall"]["failures"] == {"schema_or_protocol_invalid": 6}


def test_prior_audit_failure_does_not_mask_later_format_failure(tmp_path):
    plan, registry, paths, _ = execute(tmp_path, direct_open=True, malformed_compliance=True)
    for job in plan.jobs:
        r = read_raw_run(paths.run_dir(job.run_id))
        assert r.artifact.failed and r.artifact.failure.model_output_failure
        assert r.artifact.failure.failure_stage == "compliance"
        assert r.verifications_of("manager")[-1].validation_pass is False
    _, report = analyze_plan(plan, registry, paths.raw_dir)
    assert report["overall"]["failures"] == {"schema_or_protocol_invalid": 6}


def test_unknown_wrong_accept_and_unstarted_are_distinct(tmp_path):
    plan, registry, paths, _ = execute(tmp_path, statuses={"D0": "unknown", "D1": "absent"})
    rows, report = analyze_plan(plan, registry, paths.raw_dir)
    unknown = next(r for r in rows if r["condition_id"] == "D0")
    wrong = next(r for r in rows if r["condition_id"] == "D1")
    assert unknown["final_action"] == "REVIEW" and unknown["compliance_incorrect_absent"] is False
    assert wrong["final_action"] == "ACCEPT" and wrong["compliance_incorrect_absent"] is True
    assert wrong["final_false_accept"] is True and unknown["final_false_accept"] is False
    assert report["overall"]["false_accept_delivered"] == 2
    assert report["overall"]["reviews_delivered"] == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / plan.jobs[0].run_id).mkdir()
    (empty / plan.jobs[0].run_id / "model_calls.jsonl").write_text("{}\n")
    _, snapshot = analyze_plan(plan, registry, empty)
    assert snapshot["overall"]["execution_statuses"] == {"interrupted": 1, "not_started": 5}
    assert snapshot["overall"]["action_accuracy_among_completed"] is None
    assert snapshot["overall"]["correct_delivery_per_planned"] == 0
    assert not snapshot["batch_complete"]


def test_plan_rejects_changed_runtime_and_materials(tmp_path):
    plan, registry, _, _ = execute(tmp_path)
    changed = registry.cases[0].model_copy(update={"contract_text": registry.cases[0].contract_text + "x"})
    from pilot01.experiment.cases import CaseRegistry
    with pytest.raises(RunPlanError, match="registry changed"):
        plan.check(profile().conditions, CaseRegistry([changed], **registry.chunking))
    changed_plan = plan.model_copy(update={"runtime_config": plan.runtime_config.model_copy(update={"max_tool_requests_per_node": 4})})
    assert changed_plan.fingerprint() != plan.fingerprint()


def test_v2_cli_score_and_summary_require_explicit_profile(tmp_path):
    from pilot01.experiment.cli import main
    from clean_v2_fixture import ROOT
    plan, registry, paths, _ = execute(tmp_path)
    plan.write_json(paths.run_plan_json)
    path = registry.write_json(tmp_path / "synthetic_registry.json")
    args = ["--registry", str(path), "--experiment-id", plan.experiment_id, "--out", str(tmp_path)]
    assert main(["score", *args]) != 0
    for cmd in ("score", "summary"):
        assert main([cmd, *args, "--profile", str(ROOT / "config/experiment_v2.yaml")]) == 0
    summary = json.loads((paths.processed_dir / "experiment_summary_v2.json").read_text(encoding="utf-8"))
    assert summary["overall"]["planned"] == 6 and summary["batch_complete"]
    from pilot01.experiment.scoring import score_run, ScoringError
    job = plan.jobs[0]
    with pytest.raises(ScoringError, match="scoring_v2"):
        score_run(read_raw_run(paths.run_dir(job.run_id)), case=registry.get(job.case_id), document=registry.document(job.case_id))
