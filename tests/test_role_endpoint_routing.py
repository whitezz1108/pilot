"""Inspect real adapter requests through a fake transport; no GPU or network."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pilot01.model.execution import ExecutionSettings, PooledLiveFactory
from pilot01.runtime import ExperimentProfile
from test_concurrent_execution import credentials
from test_model_client import envelope, request_for


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def dual_endpoint_settings(tmp_path):
    """Synthetic two-endpoint topology for routing; production config is single-slot."""
    path = tmp_path / "dual-endpoints.yaml"
    path.write_text("""max_parallel_runs: 1
http_retries: 0
role_slots: {manager: 1, compliance: 2}
endpoints:
  - {slot: 1, max_in_flight: 1}
  - {slot: 2, max_in_flight: 1}
""", encoding="utf-8")
    return path


def test_engineering_profile_routes_role_requests_and_budgets(tmp_path, monkeypatch):
    credentials(monkeypatch)
    profile = ExperimentProfile(CONFIG / "experiment_engineering_dual_gpu_v1.yaml")
    config_path = dual_endpoint_settings(tmp_path)
    settings = ExecutionSettings.load(config_path)
    seen = []

    def transport(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"),
                     json.loads(request.data), timeout))
        return envelope('{"ok":true}')

    jobs = [SimpleNamespace(run_id=f"run-{i}") for i in range(3)]
    pool = PooledLiveFactory(settings, jobs, tmp_path, transport=transport)
    # Multiple invocations cover tool continuations and format-repair clients.
    for job in jobs:
        for invocation in range(2):
            for role, host, key in (
                ("manager", "first", "unit-first-secret"),
                ("compliance", "second", "unit-second-secret"),
            ):
                request = request_for(params=profile.models.for_role(role), role=role,
                                      run_id=job.run_id, invocation=invocation)
                pool(role=role, job=job).generate(request)
                url, auth, payload, timeout = seen[-1]
                assert url == f"https://{host}.example/v1/chat/completions"
                assert auth == f"Bearer {key}"
                assert payload["model"] == profile.models.for_role(role).model_id
                assert payload["max_tokens"] == 32768
                assert timeout == 360.0

    plan = SimpleNamespace(fingerprint=lambda: "engineering-plan")
    pool.write_manifest(config_path, plan)
    policy = json.loads((tmp_path / "execution_policy.json").read_text())
    assert policy["run_slots"] == {}
    assert policy["run_role_slots"] == {
        j.run_id: {"manager": 1, "compliance": 2} for j in jobs}
    rows = [json.loads(line) for line in pool.log_path.read_text().splitlines()]
    assert len(rows) == 12
    assert all(row["slot"] == {"manager": 1, "compliance": 2}[row["role"]]
               for row in rows)
    audit = "".join(p.read_text() for p in tmp_path.glob("*.json*"))
    assert "unit-first-secret" not in audit and "unit-second-secret" not in audit

    # Same file and endpoints, changed mapping: the frozen policy still rejects it.
    swapped = settings.model_copy(update={"role_slots": {"manager": 2, "compliance": 1}})
    other = PooledLiveFactory(swapped, jobs, tmp_path, transport=transport)
    with pytest.raises(ValueError, match="execution policy changed"):
        other.write_manifest(config_path, plan)


@pytest.mark.parametrize("mapping", [
    {}, {"manager": 1}, {"manager": 1, "compliance": 3},
    {"manager": 1, "compliance": 2, "analyst": 1},
])
def test_invalid_role_mapping_fails_before_client_construction(mapping):
    with pytest.raises(ValueError, match="role_slots"):
        ExecutionSettings(role_slots=mapping, endpoints=[
            {"slot": 1, "max_in_flight": 1}, {"slot": 2, "max_in_flight": 1}])


def test_legacy_execution_settings_omit_role_mapping():
    settings = ExecutionSettings.load(CONFIG / "execution_v1.yaml")
    assert "role_slots" not in settings.model_dump(mode="json")


def test_native_tool_profile_and_endpoint_are_explicit(tmp_path, monkeypatch):
    credentials(monkeypatch)
    profile = ExperimentProfile(CONFIG / "experiment_native_tools_v1.yaml")
    settings = ExecutionSettings.load(CONFIG / "execution_native_tools_v1.yaml")
    assert profile.runtime.tool_protocol == "native-v1"
    assert profile.models.for_role("manager").max_output_tokens == 32768
    assert profile.models.for_role("manager").reasoning_effort == "xhigh"
    assert profile.models.for_role("manager").temperature == 1.0
    assert profile.models.for_role("manager").top_p == 0.95
    assert profile.models.for_role("manager").top_k == 20
    assert profile.models.for_role("manager").min_p == 0.0
    assert profile.models.for_role("manager").presence_penalty == 0.0
    assert profile.models.for_role("manager").repetition_penalty == 1.0
    assert profile.models.models_version == "bf16-mtp-xhigh-native-tools-20261101"
    assert settings.max_parallel_runs == 1
    assert settings.http_retries == 0
    assert settings.role_slots == {"manager": 1, "compliance": 1}
    assert settings.endpoints[0].tool_transport == "native"
    job = SimpleNamespace(run_id="native-run")
    pool = PooledLiveFactory(
        settings,
        [job],
        tmp_path,
        transport=lambda *_: envelope('{"ok":true}'),
    )
    assert pool(role="manager", job=job).tool_transport == "native"


@pytest.mark.parametrize("filename,temperature", [
    ("experiment_native_tools_v1.yaml", 1.0),
    ("experiment_recommended_primary_v1.yaml", 0.6),
])
def test_native_sampling_profiles_are_explicit(filename, temperature):
    profile = ExperimentProfile(CONFIG / filename)
    assert profile.runtime.tool_protocol == "native-v1"
    for role in ("manager", "compliance"):
        params = profile.models.for_role(role)
        assert (params.temperature, params.top_p, params.top_k, params.min_p) == (temperature, 0.95, 20, 0.0)


def test_engineering_failure_is_not_sent_to_other_role_endpoint(tmp_path, monkeypatch):
    credentials(monkeypatch)
    from pilot01.model.client import ModelClientError

    seen = []

    def transport(request, timeout):
        seen.append(request.full_url)
        raise TimeoutError()

    settings = ExecutionSettings.load(CONFIG / "execution_engineering_dual_gpu_v1.yaml")
    job = SimpleNamespace(run_id="run-0")
    pool = PooledLiveFactory(settings, [job], tmp_path, transport=transport)
    with pytest.raises(ModelClientError, match="timed out"):
        pool(role="manager", job=job).generate(request_for())
    assert seen == ["https://first.example/v1/chat/completions"]


def test_workflow_uses_role_endpoints_through_real_factory(tmp_path, monkeypatch):
    import fixture_registry as fixture
    from pilot01.experiment import ExperimentPaths, ExperimentRunner, build_run_plan, read_raw_run

    credentials(monkeypatch)
    models = ExperimentProfile(CONFIG / "experiment_engineering_dual_gpu_v1.yaml").models
    registry = fixture.build_registry()
    plan = build_run_plan(experiment_id="engineering-routing", registry=registry,
        conditions=fixture.conditions(), models=models,
        condition_ids=("A0V0",), case_ids=("CASE-A",), repeat_count=1, seed=22)
    scripts = fixture.script_for_plan(plan, registry)
    responses = {role: iter([answer for job in plan.jobs
                            for answer in scripts[job.run_id][role]])
                 for role in ("manager", "compliance")}
    seen = []

    def transport(request, timeout):
        role = {"https://first.example/v1/chat/completions": "manager",
                "https://second.example/v1/chat/completions": "compliance"}[request.full_url]
        seen.append((role, json.loads(request.data)["max_tokens"], timeout))
        return envelope(next(responses[role]))

    paths = ExperimentPaths(root=tmp_path, experiment_id=plan.experiment_id)
    settings = ExecutionSettings.load(dual_endpoint_settings(tmp_path))
    pool = PooledLiveFactory(settings, plan.jobs, paths.manifest_dir, transport=transport)
    batch = ExperimentRunner(plan=plan, registry=registry, conditions=fixture.conditions(),
        workflow=fixture.workflow(), models=models, client_factory=pool, paths=paths).run()
    assert len(batch.completed) == len(plan.jobs) == 2
    assert {role for role, _, _ in seen} == {"manager", "compliance"}
    assert all(cap == 32768 and timeout == 360.0 for _, cap, timeout in seen)
    for job in plan.jobs:
        raw = read_raw_run(paths.run_dir(job.run_id))
        assert {call.role for call in raw.model_calls} == {"manager", "compliance"}
        assert all(call.params.max_output_tokens == 32768 for call in raw.model_calls)
