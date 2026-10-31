"""No network: exercise the real console-to-runner path with scripted models."""
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from clean_v2_fixture import materials, profile, script
from pilot01.experiment.batch import ScriptedClientFactory
from pilot01.experiment.console import Console, local_credentials, make_handler, read_json, write_json
from pilot01.experiment.runspec import RunPlan


def make_app(tmp_path, *, blocking=None):
    _, registry, _ = materials()
    p = profile()
    pid = registry.document("CASE-0001").paragraphs[0].paragraph_id
    class OfflinePool:
        def __init__(self, settings, jobs, directory):
            self.settings = settings
            self.routing = {j.run_id: 1 for j in jobs}
            self.gates = {}
            self.scripted = ScriptedClientFactory({j.run_id: {
                role: script(pid) for role in ("manager", "compliance")} for j in jobs})
        def write_manifest(self, config_path, plan):
            write_json(Path(config_path).parent / "offline_test_transport.json", {"scripted": True})
        def __call__(self, *, role, job):
            inner = self.scripted(role=role, job=job)
            if not blocking:
                return inner
            class Client:
                def generate(self, request):
                    blocking[0].set()
                    if not blocking[1].wait(timeout=10):
                        raise RuntimeError("test barrier timed out")
                    return inner.generate(request)
            return Client()
    return Console(root=tmp_path, registry=registry, profile=p, case_ids=("CASE-0001",),
        factory_builder=OfflinePool,
        credential_loader=lambda: [{"slot": 1, "ready": True, "host": "offline.example"},
                                   {"slot": 2, "ready": False, "host": "offline.example"}])


def start_data(draft, count=2, concurrency=2):
    return dict(draft_id=draft["draft_id"], run_ids=[j["run_id"] for j in draft["jobs"][:count]],
                concurrency=concurrency, slots=[1])


def test_prepare_is_offline_and_partial_selection_keeps_full_denominator(tmp_path):
    app = make_app(tmp_path)
    draft = app.prepare({})
    assert draft["total"] == 6
    assert not list(app.output.rglob("run.json"))
    app.start(start_data(draft))
    app.thread.join(timeout=15)
    assert not app.busy()
    state = app.state()
    assert state["session"]["status"] == "finished", state
    assert state["counters"] == {"completed": 2}
    assert all(row["node"] == "compliance" for row in state["rows"])
    assert state["session"]["full_plan_count"] == 6
    assert len(list(app.output.glob("raw/*/*/run.json"))) == 2
    paths = app._paths()
    plan = RunPlan.load_json(paths.run_plan_json)
    assert len(plan.jobs) == 6
    report = read_json(paths.processed_dir / "experiment_summary_v2.json")
    assert report["overall"]["execution_statuses"] == {"completed": 2, "not_started": 4}
    detail = app.detail(state["rows"][0]["run_id"])
    assert len(detail["calls"]) == 8 and len(detail["tools"]) == 6
    restored = make_app(tmp_path)
    assert restored.state()["counters"] == {"completed": 2}
    assert restored.state()["session"]["experiment_id"] == draft["draft_id"]


def test_stop_drains_active_then_resume_only_remaining(tmp_path):
    entered, release = threading.Event(), threading.Event()
    app = make_app(tmp_path, blocking=(entered, release))
    draft = app.prepare({})
    app.start(start_data(draft, count=6, concurrency=1))
    assert entered.wait(timeout=5)
    assert app.state()["counters"]["running"] == 1
    with pytest.raises(ValueError, match="正在运行"):
        app.start(start_data(draft))
    app.stop()
    release.set()
    app.thread.join(timeout=15)
    assert app.state()["session"]["status"] == "stopped"
    assert app.state()["counters"] == {"completed": 1, "waiting": 5}
    first_path = next(app.output.glob("raw/*/*/run.json"))
    original = first_path.read_bytes()
    app.resume()
    app.thread.join(timeout=15)
    assert app.state()["counters"] == {"completed": 6}
    assert first_path.read_bytes() == original


@pytest.mark.parametrize("change", [
    {"concurrency": 0}, {"concurrency": 129}, {"run_ids": []},
    {"run_ids": ["../outside"]}, {"slots": [2]}, {"draft_id": "stale"},
])
def test_invalid_launch_never_starts_a_thread(tmp_path, change):
    app = make_app(tmp_path)
    draft = app.prepare({})
    data = start_data(draft)
    data.update(change)
    with pytest.raises(ValueError):
        app.start(data)
    assert app.thread is None
    assert not list(app.output.rglob("run.json"))


def test_known_key_is_not_returned_by_config(tmp_path, monkeypatch):
    path = tmp_path / "api.env"
    path.write_text('PILOT01_BASE_URL=https://example.org/v1\nPILOT01_API_KEY=unit-private-value\nPILOT01_MODEL_ID=model\nPILOT01_BASE_URL_2=https://example.org/v1\nPILOT01_API_KEY_2=\n')
    for name in ("PILOT01_API_KEY", "PILOT01_BASE_URL", "PILOT01_API_KEY_2", "PILOT01_BASE_URL_2"):
        monkeypatch.setenv(name, "stale")
    result = local_credentials(path)
    assert result[0]["ready"] and not result[1]["ready"]
    assert "unit-private-value" not in json.dumps(result)
    assert "PILOT01_API_KEY_2" not in os.environ


@pytest.mark.parametrize("origin,token,host", [
    ("http://evil.example", "valid", "127.0.0.1:8766"),
    ("http://127.0.0.1:8766", "bad", "127.0.0.1:8766"),
    (None, "valid", "evil.example:8766"),
])
def test_http_start_rejects_cross_site_and_bad_token(origin, token, host):
    app = SimpleNamespace(token="valid", start=lambda _: pytest.fail("unauthorized start"))
    handler = make_handler(app).__new__(make_handler(app))
    handler.server = SimpleNamespace(server_port=8766)
    handler.headers = {"Host": host, "Origin": origin, "X-Console-Token": token}
    handler.path = "/api/start"
    handler.rfile = io.BytesIO(b'{}')
    results = []
    handler.send = lambda status, body: results.append(status)
    handler.do_POST()
    assert results == [403]


def test_detail_rejects_path_traversal(tmp_path):
    app = make_app(tmp_path)
    with pytest.raises(ValueError):
        app.detail("../../config/api.local.env")

def test_terminal_stopped_batch_cannot_resume_and_new_round_preserves_results(tmp_path):
    app = make_app(tmp_path)
    first = app.prepare({})
    app.start(start_data(first))
    app.thread.join(timeout=15)
    old_paths = {p: p.read_bytes() for p in app.output.glob('raw/*/*/run.json')}
    app.session['status'] = 'stopped'  # Historical stop after all jobs were dispatched.
    assert app.state()['session']['status'] == 'finished'
    assert not app.state()['can_resume']
    with pytest.raises(ValueError, match='均已结束'):
        app.resume()
    second = app.prepare({})
    assert second['draft_id'] != first['draft_id']
    assert all(p.read_bytes() == contents for p, contents in old_paths.items())
    app.start(start_data(second))
    app.thread.join(timeout=15)
    assert app.state()['counters'] == {'completed': 2}
    assert all(p.read_bytes() == contents for p, contents in old_paths.items())
    assert len(list(app.output.glob('raw/*/*/run.json'))) == 4


def test_stopped_incomplete_batch_can_be_left_for_new_round(tmp_path):
    entered, release = threading.Event(), threading.Event()
    app = make_app(tmp_path, blocking=(entered, release))
    first = app.prepare({})
    app.start(start_data(first, count=6, concurrency=1))
    assert entered.wait(timeout=5)
    app.stop()
    release.set()
    app.thread.join(timeout=15)
    assert app.state()['can_resume']
    previous = app._paths().manifest_dir / 'console_session.json'
    original = previous.read_bytes()
    second = app.prepare({})
    app.start(start_data(second))
    app.thread.join(timeout=15)
    assert app.state()['session']['experiment_id'] == second['draft_id']
    assert previous.read_bytes() == original
