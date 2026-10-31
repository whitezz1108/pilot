"""Offline execution tests: real run isolation, request caps and retry evidence."""
import io
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import fixture_registry as fixture
from test_model_client import request_for, envelope
from pilot01.experiment import ExperimentPaths, ScriptedClientFactory, read_raw_run
from pilot01.model.client import ModelClientError
from pilot01.model.execution import ExecutionSettings, PooledLiveFactory, AdaptiveGate


def credentials(monkeypatch):
    monkeypatch.setenv("PILOT01_BASE_URL", "https://first.example/v1")
    monkeypatch.setenv("PILOT01_API_KEY", "unit-first-secret")
    monkeypatch.setenv("PILOT01_BASE_URL_2", "https://second.example/v1")
    monkeypatch.setenv("PILOT01_API_KEY_2", "unit-second-secret")


def test_parallel_real_runs_overlap_keep_isolation_and_resume(tmp_path):
    registry = fixture.build_registry()
    plan = fixture.make_plan(registry=registry)
    scripted = ScriptedClientFactory(fixture.script_for_plan(plan, registry))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    seen = []
    class Client:
        def __init__(self, inner): self.inner = inner
        def generate(self, request):
            with lock:
                first = len(seen) < 2
                seen.append(request.run_id)
            if first: barrier.wait(timeout=10)
            return self.inner.generate(request)
    def factory(*, role, job):
        return Client(scripted(role=role, job=job))
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    batch = fixture.execute(plan, registry, paths, factory=factory, max_workers=2)
    assert [r.run_id for r in batch.results] == list(plan.run_ids)
    assert len(batch.completed) == len(plan.jobs)
    assert seen[0] != seen[1]
    for result in batch.results:
        raw = read_raw_run(result.directory)
        assert all(c.run_id == result.run_id for c in raw.model_calls)
        roles = [c.role for c in raw.model_calls]
        assert roles.index("manager") < roles.index("compliance")
    resumed = fixture.execute(plan, registry, paths, max_workers=2, resume=True)
    assert len(resumed.skipped) == len(plan.jobs)
    assert len(list(paths.batch_index_json.parent.glob("schedule_*.jsonl"))) == 2


def test_parallel_fail_fast_drains_without_starting_remaining(tmp_path):
    registry = fixture.build_registry()
    plan = fixture.make_plan(registry=registry)
    def bad(job, registry):
        return {"manager": ("bad", "bad"), "compliance": ("unused",)}
    batch = fixture.execute(plan, registry,
        ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id),
        behaviour=bad, max_workers=2, fail_fast=True)
    assert len(batch.results) == 2
    assert len(batch.failed) == 2


def make_pool(tmp_path, monkeypatch, transport, **settings):
    credentials(monkeypatch)
    return PooledLiveFactory(ExecutionSettings(**settings),
        [SimpleNamespace(run_id=f"run-{i}") for i in range(8)],
        tmp_path, transport=transport, sleeper=lambda _: None)


def http_error(status, retry_after="0"):
    return urllib.error.HTTPError("https://first.example/v1", status, "test",
        {"Retry-After": retry_after}, io.BytesIO(b'{}'))


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_retry_is_bounded_and_audited(tmp_path, monkeypatch, status):
    attempts = []
    def transport(request, timeout):
        attempts.append(request)
        if len(attempts) < 3: raise http_error(status)
        return envelope("{}")
    pool = make_pool(tmp_path, monkeypatch, transport)
    pool(role="manager", job=SimpleNamespace(run_id="run-0")).generate(request_for())
    rows = [json.loads(s) for s in pool.log_path.read_text().splitlines()]
    assert [r["status"] for r in rows] == [status, status, 200]
    assert [r["retry"] for r in rows] == [True, True, False]
    assert len({r["operation"] for r in rows}) == 1
    assert all(r.data == attempts[0].data for r in attempts)
    assert "unit-first-secret" not in pool.log_path.read_text()
    if status in (429, 503): assert pool.gates[1].limit < 6


@pytest.mark.parametrize("error", [http_error(401), http_error(429), TimeoutError(),
                                      urllib.error.URLError("broken")])
def test_exhaustion_and_no_retry_for_ambiguous_failures(tmp_path, monkeypatch, error):
    def transport(request, timeout): raise error
    pool = make_pool(tmp_path, monkeypatch, transport)
    with pytest.raises(ModelClientError):
        pool(role="manager", job=SimpleNamespace(run_id="run-0")).generate(request_for())
    rows = pool.log_path.read_text().splitlines()
    assert len(rows) == (3 if isinstance(error, urllib.error.HTTPError) and error.code == 429 else 1)


def test_invalid_answer_is_not_retried_by_transport(tmp_path, monkeypatch):
    body = json.loads(envelope("not JSON"))
    body["choices"][0]["finish_reason"] = "length"
    pool = make_pool(tmp_path, monkeypatch, lambda r, t: json.dumps(body).encode())
    result = pool(role="manager", job=SimpleNamespace(run_id="run-0")).generate(request_for())
    assert result.finish_reason == "length"
    assert len(pool.log_path.read_text().splitlines()) == 1


def test_second_slot_routing_and_keys_stay_out_of_manifest(tmp_path, monkeypatch):
    calls = []
    def transport(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization")))
        return envelope("{}")
    pool = make_pool(tmp_path, monkeypatch, transport, endpoints=[
        {"slot": 1, "max_in_flight": 2}, {"slot": 2, "max_in_flight": 2}])
    for role in ("manager", "compliance"):
        pool(role=role, job=SimpleNamespace(run_id="run-1")).generate(request_for())
    assert all(url.startswith("https://second.example/") for url, key in calls)
    assert all(key == "Bearer unit-second-secret" for url, key in calls)
    config = tmp_path / "execution.yaml"
    config.write_text("{}")
    pool.write_manifest(config, SimpleNamespace(fingerprint=lambda: "plan-1"))
    text = "".join(p.read_text() for p in tmp_path.glob("*.json*"))
    assert "unit-first-secret" not in text and "unit-second-secret" not in text
    config.write_text("changed")
    with pytest.raises(ValueError, match="execution policy changed"):
        pool.write_manifest(config, SimpleNamespace(fingerprint=lambda: "plan-1"))


def test_gate_limits_concurrent_requests_and_recovers():
    gate = AdaptiveGate(2, recovery=2)
    gate.acquire(); gate.release(throttled=True)
    assert gate.limit == 1
    gate.acquire(); gate.release(success=True)
    gate.acquire(); gate.release(success=True)
    assert gate.limit == 2
    entered = threading.Barrier(3)
    release = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()
    def run():
        nonlocal active, peak
        gate.acquire()
        with lock: active += 1; peak = max(peak, active)
        entered.wait(timeout=5)
        release.wait(timeout=5)
        with lock: active -= 1
        gate.release(success=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run), pool.submit(run)
        entered.wait(timeout=5)
        release.set()
        a.result(); b.result()
    assert peak == 2 and gate.active == 0


def test_v2_parallel_flow_keeps_validator_and_budget(tmp_path):
    from clean_v2_fixture import materials, profile, script
    from pilot01.experiment import ExperimentRunner
    from pilot01.experiment.runspec import build_run_plan
    _, registry, _ = materials()
    p = profile()
    plan = build_run_plan(experiment_id="parallel-v2", registry=registry,
        conditions=p.conditions, models=p.models, runtime_config=p.runtime,
        profile_fingerprint=p.fingerprint(), repeat_count=1, seed=17)
    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    responses = {j.run_id: {role: script(pid) for role in ("manager", "compliance")}
                 for j in plan.jobs}
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    batch = ExperimentRunner(plan=plan, registry=registry, conditions=p.conditions,
        models=p.models, workflow=p.workflow, profile=p, paths=paths,
        client_factory=ScriptedClientFactory(responses)).run(max_workers=6)
    assert len(batch.completed) == 6
    for job in plan.jobs:
        raw = read_raw_run(paths.run_dir(job.run_id))
        assert raw.artifact.validator_version == "validator_v3"
        assert raw.artifact.runtime_config["max_tool_requests_per_node"] == 16
        assert all(c.params.max_output_tokens == 32768 for c in raw.model_calls)


def test_missing_second_credentials_fail_before_transport(tmp_path, monkeypatch):
    credentials(monkeypatch)
    monkeypatch.delenv("PILOT01_API_KEY_2")
    with pytest.raises(ModelClientError, match="PILOT01_API_KEY_2"):
        PooledLiveFactory(ExecutionSettings(endpoints=[{"slot": 2, "max_in_flight": 2}]),
            [SimpleNamespace(run_id="r")], tmp_path,
            transport=lambda *_: pytest.fail("must not call transport"))


def test_two_keys_share_account_gate_and_throttling(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, monkeypatch, lambda *_: envelope("{}"),
        endpoints=[{"slot": 1, "max_in_flight": 32, "quota_group": "same-account"},
                   {"slot": 2, "max_in_flight": 32, "quota_group": "same-account"}])
    assert pool.gates[1] is pool.gates[2]
    pool.gates[1].acquire()
    pool.gates[1].release(throttled=True)
    assert pool.gates[2].limit == 16


def test_independent_accounts_have_separate_gates(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, monkeypatch, lambda *_: envelope("{}"),
        endpoints=[{"slot": 1, "max_in_flight": 32, "quota_group": "account-a"},
                   {"slot": 2, "max_in_flight": 32, "quota_group": "account-b"}])
    assert pool.gates[1] is not pool.gates[2]
    pool.gates[1].acquire()
    pool.gates[1].release(throttled=True)
    assert pool.gates[2].limit == 32


def test_conflicting_same_account_caps_rejected():
    with pytest.raises(ValueError, match="same max_in_flight"):
        ExecutionSettings(endpoints=[
            {"slot": 1, "max_in_flight": 6, "quota_group": "same"},
            {"slot": 2, "max_in_flight": 32, "quota_group": "same"}])


def test_nonstreaming_keepalive_blank_lines_are_valid_json(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, monkeypatch,
        lambda *_: b'\n\r\n   \n' + envelope("{}") + b'\n\n')
    result = pool(role="manager", job=SimpleNamespace(run_id="run-0")).generate(request_for())
    assert result.text == "{}"


def test_64_selected_runs_keep_full_plan_and_isolated_logs(tmp_path):
    registry = fixture.build_registry()
    plan = fixture.make_plan(registry=registry, repeat_count=10)
    selected = plan.run_ids[1:65]
    assert len(selected) == 64
    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    batch = fixture.execute(plan, registry, paths, max_workers=64, selected_run_ids=selected)
    assert len(batch.completed) == 64
    assert tuple(r.run_id for r in batch.results) == selected
    assert not paths.run_dir(plan.run_ids[0]).exists()
    for result in batch.results:
        raw = read_raw_run(result.directory)
        assert all(c.run_id == result.run_id for c in raw.model_calls)
    schedule = next(paths.manifest_dir.glob("schedule_*.jsonl"))
    rows = [json.loads(line) for line in schedule.read_text().splitlines()]
    submitted = [r for r in rows if r["event"] == "submitted"]
    assert [r["plan_index"] for r in submitted] == list(range(1, 65))
