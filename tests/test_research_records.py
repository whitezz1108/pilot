"""Research provenance, lazy payload boundaries and real transport linkage."""
import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pilot01.experiment.costs import estimate_call
from pilot01.experiment.research_records import split_record, join_record
from pilot01.source import ToolRequest, SourceToolError
from test_experiment_records import completed_app
from test_source_retrieval import tools_for
from test_concurrent_execution import make_pool, http_error, envelope, request_for
from pilot01.model.parsing import record_call
from pilot01.source.pagination import PagedSourceTools
from pilot01.experiment.console import make_handler
import sample_case


def test_tool_clock_measures_both_ends_and_monotonic_duration_on_failures():
    times = iter(datetime(2026,10,19,tzinfo=timezone.utc) + timedelta(seconds=i) for i in range(8))
    tools = tools_for(clock=lambda: next(times))
    with patch("pilot01.source.tools.perf_counter", side_effect=[10,10.125,20,20.5]):
        tools.search_contract("agreement")
        with pytest.raises(SourceToolError):
            tools.open_source_span("malformed")
    a,b = tools.log.records
    assert a.responded_at > a.requested_at and b.responded_at > b.requested_at
    assert a.latency_seconds == .125 and b.latency_seconds == .5
    assert a.ok and not b.ok
    assert a.tool_call_id != b.tool_call_id
    assert a.source_version and all("text" not in p for p in a.result_locators)


def test_paged_tools_and_protocol_rejections_have_timing_and_parent_id():
    tools = PagedSourceTools(node="manager", library=sample_case.build_source_library(),
                             target_categories=("target",))
    tools.begin_invocation(contract_id=sample_case.CONTRACT_ID, available=True)
    tools.execute(ToolRequest(tool="search_contract", arguments={"query":"agreement", "target_category":"target"}),
                  call_index=0, invocation=0, run_id="r", parent_model_call_id="m1")
    tools.reject(tool="",arguments={},error="malformed",call_index=1,invocation=0,run_id="r",parent_model_call_id="m2")
    assert [t.parent_model_call_id for t in tools.log.records] == ["m1", "m2"]
    assert all(t.requested_at and t.responded_at and t.latency_seconds is not None for t in tools.log.records)


def test_http_retries_share_the_actual_model_call_id_without_sending_it(tmp_path, monkeypatch):
    attempts = []
    def transport(request, timeout):
        attempts.append(request)
        assert "model_call_id" not in request.data.decode()
        assert not any("call-id" in k.lower() for k in request.headers)
        if len(attempts) == 1:
            raise http_error(429)
        return envelope("{}")
    pool = make_pool(tmp_path, monkeypatch, transport)
    request = request_for()
    response = pool(role="manager",job=SimpleNamespace(run_id="run-0")).generate(request)
    log = record_call(index=0,request=request,prompt_version="test",attempt=0,is_repair=False,response=response)
    rows = [json.loads(line) for line in pool.log_path.read_text().splitlines()]
    assert len(rows) == 2
    assert {r["model_call_id"] for r in rows} == {log.model_call_id}
    assert {r["operation"] for r in rows} == {log.model_call_id}


def priced_call(at="2026-10-18T03:00:00+00:00"):
    return dict(params=dict(model_id="DeepSeek-V4.1-Flash"), requested_at=at,
                usage=dict(prompt_tokens=1_000_000, cached_tokens=200_000,
                           completion_tokens=100_000, reasoning_tokens=80_000))


def test_reference_cost_cache_peak_holiday_and_missing_usage():
    # 800k uncached + 200k cached + 100k output. Reasoning is not added again.
    assert estimate_call(priced_call())["amount"] == pytest.approx(.1806)
    assert estimate_call(priced_call("2026-10-19T03:00:00+00:00"))["amount"] == pytest.approx(.3612)
    assert estimate_call(priced_call("2026-10-01T03:00:00+00:00"))["amount"] == pytest.approx(.1806)
    missing = priced_call();del missing["usage"]["cached_tokens"]
    assert estimate_call(missing)["amount"] is None
    invalid = priced_call();invalid["usage"]["cached_tokens"] = 2_000_000
    assert estimate_call(invalid)["amount"] is None
    unknown = priced_call();unknown["params"]["model_id"] = "another-model"
    assert estimate_call(unknown)["reason"] == "unknown_model_price"


def test_split_files_are_lossless_memo_versions_are_distinct_and_queries_lazy(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    rid = app.records.batch(ident)["rows"][0]["run_id"]
    original = app.records.run(ident,rid)
    parts = split_record(original, document=app.registry.document("CASE-0001"))
    assert join_record(parts) == original
    assert parts["materials"]["source_registry_verified"]
    assert all(p["start_char"] is not None for p in parts["materials"]["locators"])
    assert "events" not in parts["overview"]["record"]["artifact"]["execution"]
    assert all("rendered_input" not in c and "raw_response" not in c for c in parts["model_calls"])
    alternate = deepcopy(original)
    for event in alternate["artifact"]["execution"]["events"]:
        out = (event.get("payload") or {}).get("output") or {}
        if event.get("node") == "analyst" and out.get("claims"):
            out["claims"][0]["summary"] = "Different treatment, same memo ID"
    m1, m2 = parts["materials"], split_record(alternate)["materials"]
    assert m1["memo"]["memo_id"] == m2["memo"]["memo_id"]
    assert m1["memo_version_id"] != m2["memo_version_id"]
    app.records.research.section(ident,rid)  # Build once.
    # Cached overview/call metadata must not open any original full run again.
    with patch.object(app.records,"run",side_effect=AssertionError("eager raw read")):
        overview = app.records.research.section(ident,rid)
        calls = app.records.research.section(ident,rid,"model_calls")
        first = calls["rows"][0]["model_call_id"]
        request = app.records.research.section(ident,rid,"model_inputs",call_id=first)
    assert "calls" not in overview["record"] and "reconstruction" not in overview
    assert len(request["rows"]) == 1
    assert request["rows"][0]["rendered_input"] == original["calls"][0]["rendered_input"]
    assert len({c["model_call_id"] for c in calls["rows"]}) == len(original["calls"])
    with app.records.index.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM model_call_refs").fetchone()[0] == len(original["calls"])


def test_derived_historical_ids_do_not_claim_recorded_linkage(tmp_path):
    app = completed_app(tmp_path)
    ident = app.records.list()[0]["id"]
    rid = app.records.batch(ident)["rows"][0]["run_id"]
    record = app.records.run(ident,rid)
    for c in record["calls"]:
        c.pop("model_call_id", None)
    for t in record["tools"]:
        for key in ("parent_model_call_id", "tool_call_id", "latency_seconds", "requested_at", "responded_at"):
            t.pop(key,None)
    parts = split_record(record)
    assert all(c["id_origin"] == "derived_composite" for c in parts["model_calls"])
    assert all(t["timing_status"] == "historical_missing" and t["link_origin"] == "derived_composite" for t in parts["tools"])
    assert all(t["parent_model_call_id"] for t in parts["tools"])
    assert join_record(parts) == record


def test_console_detail_endpoint_is_also_an_overview_not_a_combined_record(tmp_path):
    app = completed_app(tmp_path)
    rid = app.state()["rows"][0]["run_id"]
    handler = make_handler(app).__new__(make_handler(app))
    handler.server = SimpleNamespace(server_port=8779)
    handler.headers = {"Host":"127.0.0.1:8779"}
    handler.path = "/api/detail?run_id=" + rid
    sent=[]
    handler.send=lambda status,body: sent.append((status,body))
    handler.do_GET()
    assert sent[0][0] == 200
    result=sent[0][1]
    assert result["record"]["summary"]["run_id"] == rid
    assert "calls" not in result["record"] and "events" not in result["record"]["artifact"]["execution"]


def test_imported_calls_without_run_id_still_get_run_scoped_historical_ids():
    def record(rid):
        return dict(summary=dict(run_id=rid),calls=[dict(role="manager",invocation=0,call_index=0)],
                    tools=[],verification=[],artifact=None)
    a,b=(split_record(record(rid)) for rid in ("first-run","second-run"))
    assert a["model_calls"][0]["model_call_id"] != b["model_calls"][0]["model_call_id"]
