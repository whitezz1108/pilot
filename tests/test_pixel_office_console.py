"""The pixel office is a read-only view over the experiment console state."""
import json
import threading
from types import SimpleNamespace

import pytest

from pilot01.experiment.console import Console, make_handler


@pytest.mark.parametrize(("path", "kind", "signature"), [
    ("/office", "text/html; charset=utf-8", b"refreshOffice"),
    ("/pixel-office.css", "text/css; charset=utf-8", b".pixelOffice"),
    ("/pixel-office.js", "application/javascript; charset=utf-8", b"Agent "),
    ("/assets/pixel-office-room.png", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("/assets/pixel-office-room-v2.png", "image/png", b"\x89PNG\r\n\x1a\n"),
])
def test_pixel_office_assets_are_served_from_fixed_paths(path, kind, signature):
    app = SimpleNamespace(redact=str, token="test-token")
    handler_type = make_handler(app)
    handler = handler_type.__new__(handler_type)
    handler.server = SimpleNamespace(server_port=8766)
    handler.headers = {"Host": "127.0.0.1:8766"}
    handler.path = path
    sent = []
    handler.send = lambda status, body, content_type="application/json; charset=utf-8": sent.append(
        (status, body, content_type)
    )

    handler.do_GET()

    assert sent[0][0] == 200
    assert sent[0][2] == kind
    assert signature in sent[0][1]


def test_activity_records_a_real_manager_to_compliance_handoff(tmp_path):
    app = Console.__new__(Console)
    app.lock = threading.RLock()
    app.activity = {}
    app._paths = lambda: SimpleNamespace(manifest_dir=tmp_path)
    job = SimpleNamespace(run_id="run-1")

    app._activity(job, "manager", "requesting")
    manager_started = app.activity[job.run_id]["role_started_at"]
    app._activity(job, "manager", "returned")
    assert app.activity[job.run_id]["role_started_at"] == manager_started
    assert app.activity[job.run_id]["handoff_at"] is None

    app._activity(job, "compliance", "requesting")
    handoff = app.activity[job.run_id]
    assert handoff["previous_role"] == "manager"
    assert handoff["handoff_at"] == handoff["role_started_at"]

    app._activity(job, "compliance", "returned")
    latest = app.activity[job.run_id]
    assert latest["handoff_at"] == handoff["handoff_at"]
    events = [json.loads(line) for line in (tmp_path / "console_activity.jsonl").read_text().splitlines()]
    assert events[-1]["handoff_at"] == handoff["handoff_at"]
