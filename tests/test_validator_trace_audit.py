"""Replay the real artifact path and check rejection candidates stay auditable."""

import hashlib
import importlib.util
from pathlib import Path

import fixture_registry as fixture
import sample_case
from pilot01.experiment.layout import ExperimentPaths


def load_auditor():
    path = Path(__file__).resolve().parents[1] / "scripts" / "validator_trace_audit.py"
    spec = importlib.util.spec_from_file_location("validator_trace_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_agrees_with_runtime_and_does_not_change_raw_files(tmp_path):
    registry = fixture.build_registry()
    plan = fixture.make_plan(registry=registry, case_ids=(fixture.CASE_A,),
                             condition_ids=("A0V0", "A1V0", "A1V1"))
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    fixture.execute(plan, registry, paths)
    def hashes():
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths.raw_dir.rglob("*") if path.is_file()}
    before = hashes()
    result = load_auditor().audit_runs(registry, paths.raw_dir)
    assert hashes() == before
    assert result["counts"]["runs"] == 6
    assert len(result["nodes"]) == 12
    assert all(row["current_replay_matches_runtime"] is True for row in result["nodes"])
    assert all(run["validator_version"] == "validator_v2_1" for run in result["runs"])


def test_replay_preserves_candidate_diagnosis_when_manager_was_rejected(tmp_path):
    registry = fixture.build_registry()
    plan = fixture.make_plan(registry=registry, case_ids=(fixture.CASE_A,),
                             condition_ids=("A1V1",))
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    def reject(job, registry):
        responses = fixture.default_responses(job, registry)
        responses["manager"] = (*responses["manager"][:-1], sample_case.manager_response_text(
            reason_summary="Assessment uses 0123456789ab:p0001."
        ))
        return responses
    fixture.execute(plan, registry, paths, behaviour=reject)
    result = load_auditor().audit_runs(registry, paths.raw_dir)
    assert len(result["nodes"]) == 2  # Manager for E0/E1; Compliance never reached.
    assert all(row["node"] == "manager" and not row["candidate_applied"]
               for row in result["nodes"])
    assert all(row["current_replay_matches_runtime"] is True for row in result["nodes"])
    assert all("narrative_reference_not_registered" in row["replayed_outcome"]["audit_failures"]
               for row in result["nodes"])
