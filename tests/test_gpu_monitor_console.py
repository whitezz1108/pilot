"""GPU monitoring is read-only, cached, and exposed by the console."""
from types import SimpleNamespace

from pilot01.experiment.console import make_handler
from pilot01.experiment.gpu_monitor import GpuMonitor


def test_gpu_monitor_uses_ssh_and_caches_success():
    calls = []
    payload = '{"host":"t640","collected_at":"2026-11-01T00:00:00+00:00","gpus":[],"services":[]}'

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monitor = GpuMonitor(mode="ssh", ssh_host="test-t640", cache_seconds=60, runner=runner)
    first = monitor.collect()
    second = monitor.collect()
    assert first is second
    assert first["ok"] and first["host"] == "t640"
    assert calls[0][0][-2:] == ["python3", "-"]
    assert "test-t640" in calls[0][0]
    assert len(calls) == 1


def test_gpu_monitor_failure_is_data_not_an_exception():
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=255, stdout="", stderr="ssh unavailable")

    result = GpuMonitor(mode="ssh", runner=runner).collect(force=True)
    assert not result["ok"]
    assert "ssh unavailable" in result["error"]


def test_gpu_status_endpoint_forwards_force_and_uses_503_for_offline():
    forces = []
    app = SimpleNamespace(
        gpu_status=lambda force=False: forces.append(force) or {"ok": False, "error": "offline"},
        redact=str,
    )
    handler_type = make_handler(app)
    handler = handler_type.__new__(handler_type)
    handler.server = SimpleNamespace(server_port=8766)
    handler.headers = {"Host": "127.0.0.1:8766"}
    handler.path = "/api/gpu-status?force=1"
    sent = []
    handler.send = lambda status, body, kind="application/json; charset=utf-8": sent.append((status, body))
    handler.do_GET()
    assert forces == [True]
    assert sent == [(503, {"ok": False, "error": "offline"})]
