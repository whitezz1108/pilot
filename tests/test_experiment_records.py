"""History browsing and portable imports cannot start or mutate experiments."""
import io
import json
import zipfile
import threading

import pytest

from test_experiment_console import make_app, start_data
from pilot01.experiment.records import Records


def completed_app(tmp_path):
    app = make_app(tmp_path)
    draft = app.prepare({})
    app.start(start_data(draft))
    app.thread.join(timeout=15)
    assert not app.busy()
    return app


def test_history_export_import_roundtrip_preserves_inputs_and_active_state(tmp_path):
    app = completed_app(tmp_path)
    state_before = app.state()
    raw_before = {p: p.read_bytes() for p in app.output.glob("raw/*/*/*") if p.is_file()}
    ident = app.records.list()[0]["id"]
    batch = app.records.batch(ident)
    assert len(batch["rows"]) == 2
    rid = batch["rows"][0]["run_id"]
    record = app.records.run(ident, rid)
    assert len(record["calls"]) == 8 and len(record["tools"]) == 6
    assert record["research"]["gold"]["Change Of Control"] == "present"
    imported = app.records.import_bytes("完整记录.json", app.records.export(ident))
    restored = app.records.run(imported["id"], rid)
    assert restored["calls"] == record["calls"]
    assert restored["artifact"] == record["artifact"]
    assert restored["research"] == record["research"]
    assert app.state()["session"] == state_before["session"]
    assert app.state()["counters"] == state_before["counters"]
    assert all(p.read_bytes() == data for p, data in raw_before.items())
    assert len(Records(app.output, app.registry).list()) == 2


def test_csv_is_summary_only_and_remains_inert(tmp_path):
    app = make_app(tmp_path)
    data = ('run_id,case_id,condition,memo,status,action,tokens,manager_validation_pass\n'
            'run-1,<script>alert(1)</script>,D0,E1,completed,ESCALATE,123,False\n').encode()
    imported = app.records.import_bytes("../../summary.csv", data)
    record = app.records.run(imported["id"], "run-1")
    assert record["artifact"] is None and record["calls"] == []
    assert record["summary"]["tokens"] == 123
    assert record["summary"]["manager_validation_pass"] is False
    assert "research" not in record
    assert app.thread is None and app.session is None
    assert not list(app.output.glob("raw/*"))


def test_zip_raw_logs_import_and_path_traversal_rejected(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    rid = app.records.batch(ident)["rows"][0]["run_id"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in app._paths().run_dir(rid).iterdir():
            if path.is_file():
                archive.writestr(f"raw/batch/{rid}/{path.name}", path.read_bytes())
    result = app.records.import_bytes("raw.zip", stream.getvalue())
    assert len(app.records.run(result["id"], rid)["calls"]) == 8
    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.txt", "no")
    with pytest.raises(ValueError, match="不安全路径"):
        app.records.import_bytes("unsafe.zip", unsafe.getvalue())
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("filename,data", [
    ("page.html", b"<script>throw 1</script>"),
    ("bad.json", b"{}"),
    ("bad.csv", b"foo,bar\na,b\n"),
    ("bad.zip", b"not a zip"),
])
def test_invalid_upload_has_no_effect(tmp_path, filename, data):
    app = make_app(tmp_path)
    with pytest.raises(ValueError):
        app.records.import_bytes(filename, data)
    assert not app.records.list()
    assert app.thread is None


def test_no_cross_batch_run_access_and_no_material_fingerprint_guess(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    with pytest.raises(ValueError):
        app.records.run(ident, "../outside")
    with pytest.raises(ValueError):
        app.records.batch("local:../../outside")
    plan_path = app._paths().run_plan_json
    plan = json.loads(plan_path.read_text())
    plan["registry_fingerprint"] = "sha256:" + "0" * 64
    plan_path.write_text(json.dumps(plan))
    rid = app.records.batch(ident)["rows"][0]["run_id"]
    assert "research" not in app.records.run(ident, rid)


def test_browsing_live_batch_does_not_call_it_interrupted(tmp_path):
    entered, release = threading.Event(), threading.Event()
    app = make_app(tmp_path, blocking=(entered, release))
    draft = app.prepare({})
    app.start(start_data(draft, concurrency=1))
    try:
        assert entered.wait(5)
        ident = app.records.list()[0]["id"]
        rows = app.records.batch(ident)["rows"]
        assert any(row["status"] == "running" for row in rows)
        assert app.busy() and app.session["experiment_id"] == draft["draft_id"]
    finally:
        app.stop()
        release.set()
        app.thread.join(15)


def test_malformed_nested_json_is_rejected_as_a_file_error(tmp_path):
    app = completed_app(tmp_path)
    exported = json.loads(app.records.export(app.records.list()[0]["id"]))
    exported["runs"][0]["calls"][0]["usage"] = "invalid"
    with pytest.raises(ValueError, match="usage"):
        app.records.import_bytes("invalid.json", json.dumps(exported).encode())
