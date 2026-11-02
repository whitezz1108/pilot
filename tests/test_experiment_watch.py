"""The monitor must not turn an incomplete observation into a confirmed failure."""
import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "experiment_watch", Path(__file__).parents[1] / "scripts/watch_experiment_batch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def test_watcher_archives_terminal_failure_once_and_does_not_invent_missing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "process_observation", lambda: ["123 console"])

    def unavailable(*args, **kwargs):
        raise TimeoutError("transient observation timeout")

    monkeypatch.setattr(watch.urllib.request, "urlopen", unavailable)
    base = tmp_path / "experiment"
    out = tmp_path / "watch"
    batch = "test-batch"
    manifest = base / "manifests" / batch
    manifest.mkdir(parents=True)
    watch.write_json(manifest / "run_plan.json", {"jobs": []})
    partial = base / "raw" / batch / "partial"
    partial.mkdir(parents=True)
    (partial / "model_calls.jsonl").write_text('{"role":"manager"}\n')
    failed = base / "raw" / batch / "failed"
    failed.mkdir(parents=True)
    artifact = {"run_id": "failed", "execution": {"status": "failed"},
                "failure": {"error": "native-v2 received 2 calls"}}
    watch.write_json(failed / "run.json", artifact)
    native = [{"function": {"name": "search_contract"}}] * 2
    (failed / "model_calls.jsonl").write_text(json.dumps({"role": "manager", "provider_response": {
        "choices": [{"message": {"tool_calls": native}, "finish_reason": "tool_calls"}]}}) + "\n")

    assert watch.inspect_once(base, out, ["partial", "failed", "not_started"], batch, "http://offline.invalid") is False
    state = watch.read_json(out / "latest.json")
    assert state["counts"] == {"unconfirmed": 2, "failed": 1}
    assert state["all_original_runs_terminal"] is False
    evidence = out / "failures/failed"
    assert watch.read_json(evidence / "raw/run.json") == artifact
    facts = watch.read_json(evidence / "facts.json")
    assert facts["observed_signals"] == ["multiple_native_calls_in_one_response"]
    assert facts["attribution_status"] == "pending_evidence_review"
    hashes = watch.read_json(evidence / "evidence_manifest.json")
    assert hashes["run_json_source_sha256"] == hashes["run_json_copy_sha256"]
    original_marker = (failed / "run.json").read_bytes()
    assert watch.inspect_once(base, out, ["partial", "failed", "not_started"], batch, "http://offline.invalid") is False
    assert watch.read_json(out / "latest.json")["newly_archived_failures"] == []
    assert (failed / "run.json").read_bytes() == original_marker


def test_completed_marker_without_downstream_delivery_is_exposed(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "process_observation", lambda: [])

    def unavailable(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(watch.urllib.request, "urlopen", unavailable)
    base = tmp_path / "experiment"
    directory = base / "raw/test/completed"
    directory.mkdir(parents=True)
    watch.write_json(directory / "run.json", {"execution": {
        "status": "completed", "manager_output": {"decision": "REVIEW"}, "compliance_output": None}})
    out = tmp_path / "watch"
    assert watch.inspect_once(base, out, ["completed"], "test", "http://offline.invalid") is True
    assert watch.read_json(out / "latest.json")["completed_but_missing_delivery"] == ["completed"]
