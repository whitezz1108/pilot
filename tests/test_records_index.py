"""Query cost and evidence fidelity, including a synthetic 1,000-run batch."""
import io
import json
import threading
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from pilot01.experiment.records import Records
from pilot01.experiment.console import make_handler
from types import SimpleNamespace
from test_experiment_console import make_app, start_data
from test_experiment_records import completed_app


def test_thousand_runs_are_indexed_once_then_paged_without_reading_logs(tmp_path):
    app = make_app(tmp_path)
    records = app.records
    batch_id = "synthetic-thousand"
    directory = app.output / "manifests" / batch_id
    directory.mkdir(parents=True)
    ids = [f"run-{i:05d}" for i in range(1000)]
    jobs = [dict(run_id=rid, case_id="CASE-0001", condition_id="A1V0D0" if i % 2 else "A1V0D1",
                 error_condition="E1", source_access=True, verification_required=False) for i, rid in enumerate(ids)]
    (directory / "console_session.json").write_text(json.dumps(dict(run_ids=ids, full_plan_count=1300, status="finished")))
    (directory / "run_plan.json").write_text(json.dumps(dict(jobs=jobs, registry_fingerprint=records.registry_hash)))
    for i, job in enumerate(jobs):
        raw = app.output / "raw" / batch_id / job["run_id"]
        raw.mkdir(parents=True)
        output = dict(decision="ESCALATE", target_clause_status={"Change Of Control": "present"})
        a = dict(**job, experiment_id=batch_id, execution=dict(status="completed", manager_output=output,
                  compliance_output=output), failure=dict(protocol_failure=i % 5 == 0))
        (raw / "run.json").write_text(json.dumps(a))
        (raw / "model_calls.jsonl").write_text(json.dumps(dict(run_id=job["run_id"], role="manager",
             rendered_input="synthetic input " * 50, raw_response="synthetic output",
             usage=dict(total_tokens=10), parsed_output=output)) + "\n")
    ident = "local:" + batch_id
    original_read = Path.read_text
    reads = Counter()
    def tracked(path, *args, **kwargs):
        reads[path.name] += 1
        return original_read(path, *args, **kwargs)
    with patch.object(Path, "read_text", tracked):
        page = records.batch(ident, offset=100, limit=100)
    assert len(page["rows"]) == 100 and page["total"] == 1000
    assert page["rows"][0]["run_id"] == ids[100]
    assert page["aggregate"]["counters"] == dict(completed=800, failed=200)
    assert page["aggregate"]["tokens"] == 10000
    assert page["batch"]["full_plan_count"] == 1300
    assert reads["run_plan.json"] == 1  # No N copies of the entire plan.
    def no_raw(*args, **kwargs):
        pytest.fail("warm queries must not read any raw logs")
    with patch.object(records, "_local_run", no_raw):
        assert records.list()[0]["count"] == 1000
        filtered = records.batch(ident, status="failed", offset=100, limit=100)
        assert filtered["total"] == 200 and len(filtered["rows"]) == 100
        assert filtered["aggregate"] == page["aggregate"]  # Pagination never changes denominators.
        assert records.batch(ident, condition="A1V0D1")["total"] == 500
        assert records.batch(ident, q="run-009")["total"] == 100
        assert records.batch(ident, q="'%_")["total"] == 0
        restarted = Records(app.output, app.registry)
        with patch.object(restarted, "_local_run", no_raw):
            assert restarted.batch(ident, offset=900)["rows"][-1]["run_id"] == ids[-1]
    with records.index.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT max(length(summary)) FROM runs").fetchone()[0] < 3000
        plan = db.execute("EXPLAIN QUERY PLAN SELECT run_id FROM runs WHERE batch_id=? AND status=? ORDER BY ordinal LIMIT 100",
                          (ident, "failed")).fetchall()
        assert any("runs_status" in str(tuple(r)) for r in plan)


def test_only_changed_run_is_reindexed_and_incomplete_tail_is_ignored(tmp_path):
    app = completed_app(tmp_path)
    records = app.records
    ident = records.list()[0]["id"]
    before = records.batch(ident)
    rid = before["rows"][0]["run_id"]
    path = app._paths().run_dir(rid) / "model_calls.jsonl"
    data = json.loads(path.read_bytes().split(b"\n")[0])
    data["usage"] = dict(total_tokens=777)
    line = json.dumps(data).encode()
    with path.open("ab") as f:
        f.write(line)  # Not a committed log entry yet.
    assert records.batch(ident)["aggregate"]["tokens"] == before["aggregate"]["tokens"]
    with path.open("ab") as f:
        f.write(b"\n")
    with patch.object(records, "_local_run", wraps=records._local_run) as reader:
        after = records.batch(ident)
    assert reader.call_count == 1
    assert reader.call_args.args[1] == rid
    assert after["aggregate"]["tokens"] == before["aggregate"]["tokens"] + 777


def test_legacy_import_splits_once_and_single_run_reads_one_file(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    bundle = app.records.export(ident)
    legacy = app.records.imports / "old-bundle"
    legacy.mkdir(parents=True)
    (legacy / "bundle.json").write_bytes(bundle)
    imported = "import:old-bundle"
    rid = app.records.batch(imported)["rows"][0]["run_id"]
    assert (legacy / "bundle.json").read_bytes() == bundle
    assert (legacy / "manifest.json").exists()
    original = Path.read_text
    reads = []
    def track(p, *args, **kwargs):
        reads.append(p)
        return original(p, *args, **kwargs)
    with patch.object(Path, "read_text", track):
        result = app.records.run(imported, rid)
    assert result["summary"]["run_id"] == rid
    assert len(reads) == 3
    assert all("old-bundle" in p.parts for p in reads)
    assert not any(p.name == "bundle.json" for p in reads)


def test_split_zip_roundtrip_keeps_research_annotations_and_inputs(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    rows = app.records.batch(ident)["rows"]
    before = {r["run_id"]: app.records.run(ident, r["run_id"]) for r in rows}
    with patch.object(app.records, "export", side_effect=AssertionError("must not construct giant JSON")):
        with app.records.export_zip(ident) as stream:
            with zipfile.ZipFile(stream) as archive:
                assert "manifest.json" in archive.namelist()
                assert not any(n.endswith("/record.json") for n in archive.namelist())
                assert len([n for n in archive.namelist() if n.startswith("model_inputs/")]) == 2
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
            imported = app.records.import_file("complete.zip", stream, size)
    assert not list(app.records.imports.glob("*/bundle.json"))
    for rid, record in before.items():
        assert app.records.run(imported["id"], rid) == record


def test_invalid_zip_import_is_atomic_and_cannot_leave_queryable_partial_batch(tmp_path):
    app = make_app(tmp_path)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(dict(format="pilot01-records-zip-v2", batch={},
            entries=[dict(run_id="r1", path="runs/0/record.json"), dict(run_id="r2", path="runs/1/record.json")])) )
        archive.writestr("runs/0/record.json", json.dumps(dict(summary=dict(run_id="r1"))))
        archive.writestr("runs/1/record.json", "bad json")
    with pytest.raises(ValueError):
        app.records.import_bytes("bad.zip", stream.getvalue())
    assert app.records.list() == []
    assert list(app.records.imports.iterdir()) == []


def test_state_deltas_restart_fallback_and_partial_http_tail(tmp_path):
    app = completed_app(tmp_path)
    full = app.state(compact=True)
    assert not full["delta"] and "run_ids" not in full["session"]
    same = app.state(full["version"], compact=True)
    assert same["delta"] and same["rows"] == []
    assert same["counters"] == full["counters"]
    assert not app.state("previous-process:1")["delta"]
    p = app._paths().manifest_dir / "http_attempts_test.jsonl"
    p.write_bytes(b'{"status":429,"attempt":1}\n{"status":503')
    assert app.state()["http_retries"] == 1
    with p.open("ab") as f:
        f.write(b',"attempt":2}\n')
    state = app.state()
    assert state["http_retries"] == 2 and state["errors_5xx"] == 1 and state["rate_limits"] == 1
    assert app.state()["http_retries"] == 2


def test_parallel_readers_get_consistent_pages(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    results, errors = [], []
    def read():
        try:
            for _ in range(3):
                page = app.records.batch(ident, limit=1)
                results.append((page["total"], page["aggregate"]["total"], len(page["rows"])))
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors and len(results) == 12
    assert set(results) == {(2, 2, 1)}


def test_cached_summaries_are_redacted_again_at_read_time(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    with app.records.index.connect() as db:
        row = db.execute("SELECT run_id,summary FROM runs WHERE batch_id=? LIMIT 1", (ident,)).fetchone()
        summary = json.loads(row["summary"])
        summary["error"] = "private-unit-sentinel"
        db.execute("UPDATE runs SET summary=? WHERE batch_id=? AND run_id=?",
                   (json.dumps(summary), ident, row["run_id"]))
    app.records.redact = lambda s: s.replace("private-unit-sentinel", "[REDACTED]")
    assert "private-unit-sentinel" not in json.dumps(app.records.batch(ident))


def test_http_pagination_and_default_zip_download(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    handler = make_handler(app).__new__(make_handler(app))
    handler.server = SimpleNamespace(server_port=8779)
    handler.headers = {"Host": "127.0.0.1:8779"}
    handler.path = "/api/records/batch?id=" + ident + "&offset=1&limit=1"
    sent = []
    handler.send = lambda status, body: sent.append((status, body))
    handler.do_GET()
    assert sent[0][0] == 200 and len(sent[0][1]["rows"]) == 1
    assert sent[0][1]["total"] == 2
    assert sent[0][1]["aggregate"]["total"] == 2
    handler.path = "/api/records/export?id=" + ident
    headers = {}
    handler.send_response = lambda status: sent.append((status, None))
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    handler.do_GET()
    assert headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"].endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(handler.wfile.getvalue())) as archive:
        assert len(archive.namelist()) == 17  # Offline fixture has no HTTP audit; no empty files.


@pytest.mark.parametrize("args", [dict(limit=0), dict(limit=201), dict(offset=-1)])
def test_pagination_rejects_invalid_bounds(tmp_path, args):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    with pytest.raises(ValueError, match="分页"):
        app.records.batch(ident, **args)
