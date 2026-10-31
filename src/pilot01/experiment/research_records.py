"""Type-first research files and bounded, lazy section queries.

Original evidence is never rewritten. A split package can reconstruct the old
record exactly, but normal browsing never serializes that combined object.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .costs import PRICE_VERSION, estimate_call, total_cost

VERSION = "pilot01-research-records-v3"
DERIVATION_VERSION = "2026-10-27.1"
SECTIONS = ("overview", "materials", "model_calls", "model_inputs", "model_responses",
            "tools", "verification", "workflow", "http")
JSON_SECTIONS = {"overview", "materials"}


def empty_section(section):
    if section == "materials":
        return dict(memo=None, memo_origin="not_recorded", memo_version_id=None, contract_id=None,
                    source_version=None, source_registry_verified=False, locators=[], relations=[])
    return []


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def call_key(call, run_id=None):
    return (call.get("run_id") or run_id, call.get("role", call.get("node")),
            call.get("invocation", 0), call.get("call_index", 0))


def model_id(call, run_id=None):
    return call.get("model_call_id") or "legacy-" + fingerprint(call_key(call, run_id))[:32]


def _actual_memo(events):
    for event in events:
        payload = event.get("payload") or {}
        if event.get("node") == "manager":
            memo = (payload.get("input") or {}).get("analyst_memo")
            if isinstance(memo, dict):
                return memo, "manager_input_event"
        if event.get("node") == "analyst":
            output = payload.get("output") or {}
            if isinstance(output, dict) and "memo_id" in output and "claims" in output:
                return output, "analyst_output_event"
    return None, "not_recorded"


def split_record(record, *, document=None, http=(), score=None):
    """Separate cohesive research entities; no per-number or per-token files."""
    artifact = record.get("artifact") or {}
    execution = artifact.get("execution") or {}
    events = execution.get("events") or []
    memo, memo_origin = _actual_memo(events)
    calls = record.get("calls") or []
    rid = record["summary"]["run_id"]
    if len({model_id(c, rid) for c in calls}) != len(calls):
        raise ValueError("单轮模型调用标识重复，无法建立唯一关联")
    by_key = {call_key(c, rid): model_id(c, rid) for c in calls}
    metadata, inputs, responses = [], [], []
    for c in calls:
        cid = model_id(c, rid)
        metadata.append(dict({k:v for k,v in c.items() if k not in ("rendered_input", "raw_response", "parsed_output", "reasoning_content", "provider_response")},
                             model_call_id=cid, id_origin="recorded" if c.get("model_call_id") else "derived_composite",
                             cost_estimate=estimate_call(c)))
        inputs.append(dict(model_call_id=cid, rendered_input=c.get("rendered_input")))
        responses.append(dict(model_call_id=cid, raw_response=c.get("raw_response"), parsed_output=c.get("parsed_output"),
                              **{k:c[k] for k in ("reasoning_content", "provider_response") if k in c}))
    tools = []
    relations = []
    for claim in (memo or {}).get("claims", []):
        for pid in claim.get("source_ids") or []:
            relations.append(dict(paragraph_id=pid, relation="memo_cited", claim_id=claim.get("claim_id")))
    for i, t in enumerate(record.get("tools") or []):
        parent = t.get("parent_model_call_id") or by_key.get(call_key(t, rid))
        tools.append(dict(t, parent_model_call_id=parent,
                          tool_call_id=t.get("tool_call_id") or "legacy-tool-" + fingerprint([call_key(t, rid), i])[:32],
                          link_origin="recorded" if t.get("parent_model_call_id") else "derived_composite",
                          timing_status="recorded" if t.get("latency_seconds") is not None else "historical_missing"))
        if t.get("ok"):
            for pid in t.get("result_ids") or []:
                relations.append(dict(paragraph_id=pid, relation="opened" if t.get("tool") == "open_source_span" else "search_returned",
                                      role=t.get("node"), tool_call_id=tools[-1]["tool_call_id"], model_call_id=parent))
    for role in ("manager", "compliance"):
        provenance = (execution.get(role + "_output") or {}).get("evidence_provenance") or {}
        for field, relation in (("relied_paragraph_ids", "declared_relied"), ("inherited_source_ids", "declared_inherited"),
                                ("candidate_source_ids", "declared_candidate"), ("opened_paragraph_ids", "declared_opened")):
            for pid in provenance.get(field) or []:
                relations.append(dict(paragraph_id=pid, relation=relation, role=role))
    # Locate only against exactly matching immutable text. No current-registry
    # fallback when a historical contract hash differs.
    expected_hash = artifact.get("contract_text_hash") or execution.get("contract_text_hash")
    valid_doc = document is not None and document.text_hash == expected_hash
    paragraphs = {p.paragraph_id:p for p in document.paragraphs} if valid_doc else {}
    locators = []
    for pid in sorted({r["paragraph_id"] for r in relations}):
        p = paragraphs.get(pid)
        locators.append(dict(paragraph_id=pid, contract_id=artifact.get("contract_id"), source_version=expected_hash,
                             start_char=p.start_char if p else None, end_char=p.end_char if p else None,
                             locator_status="verified" if p else "source_version_unavailable"))
    materials = dict(memo=memo, memo_origin=memo_origin,
                     memo_version_id="memo-sha256:" + fingerprint(memo) if memo else None,
                     contract_id=artifact.get("contract_id"), source_version=expected_hash,
                     source_registry_verified=valid_doc, locators=locators, relations=relations)
    head = {k:v for k,v in record.items() if k not in ("calls", "tools", "verification", "artifact")}
    if record.get("artifact") is not None:
        a = {k:v for k,v in artifact.items() if k != "execution"}
        a["execution"] = {k:v for k,v in execution.items() if k != "events"}
        head["artifact"] = a
    elif "artifact" in record:
        head["artifact"] = None
    # Exact field-presence maps are small and let legacy imports round-trip
    # without pretending derived IDs were recorded during historical runs.
    schema = dict(top_keys=list(record), call_keys=[list(c) for c in calls],
                  tool_keys=[list(t) for t in record.get("tools") or []], events_present="events" in execution)
    timeline = dict(started_at=execution.get("started_at"), completed_at=execution.get("completed_at"),
                    model_seconds=sum(c.get("latency_seconds") or 0 for c in calls),
                    tools_timed=sum(t.get("latency_seconds") is not None for t in tools), tools_total=len(tools))
    if timeline["started_at"] and timeline["completed_at"]:
        from datetime import datetime
        timeline["elapsed_seconds"] = (datetime.fromisoformat(timeline["completed_at"].replace("Z", "+00:00")) -
                                        datetime.fromisoformat(timeline["started_at"].replace("Z", "+00:00"))).total_seconds()
    return dict(overview=dict(format=VERSION, record=head, reconstruction=schema, timeline=timeline,
                              cost_estimate=total_cost(calls), score=score), materials=materials,
                model_calls=metadata, model_inputs=inputs, model_responses=responses,
                tools=tools, verification=record.get("verification") or [], workflow=events, http=list(http))


def join_record(parts):
    overview = parts["overview"]
    head = dict(overview["record"])
    schema = overview["reconstruction"]
    inputs = {v["model_call_id"]:v for v in parts["model_inputs"]}
    outputs = {v["model_call_id"]:v for v in parts["model_responses"]}
    calls = []
    for c, keys in zip(parts["model_calls"], schema["call_keys"], strict=True):
        cid = c["model_call_id"]
        values = dict(c, **{k:v for k,v in inputs[cid].items() if k != "model_call_id"},
                      **{k:v for k,v in outputs[cid].items() if k != "model_call_id"})
        calls.append({k:values[k] for k in keys})
    head["calls"] = calls
    head["tools"] = [{k:t[k] for k in keys} for t,keys in zip(parts["tools"], schema["tool_keys"], strict=True)]
    head["verification"] = parts["verification"]
    if head.get("artifact") is not None and schema["events_present"]:
        head["artifact"] = dict(head["artifact"], execution=dict(head["artifact"]["execution"], events=parts["workflow"]))
    return {k:head[k] for k in schema["top_keys"]}


def write_parts(directory, token, parts):
    """Type / batch-or-import / run, rather than a mixed record.json per run."""
    paths = {}
    for section in SECTIONS:
        if parts[section] == [] or (section == "materials" and not parts[section].get("memo") and
                                   not parts[section].get("source_version") and not parts[section].get("relations")):
            paths[section] = None  # An absent entity does not need an empty file.
            continue
        suffix = "json" if section in JSON_SECTIONS else "jsonl"
        relative = f"{section}/{token}.{suffix}"
        path = Path(directory) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(parts[section], ensure_ascii=False) if suffix == "json" else
                   "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in parts[section]))
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        paths[section] = relative
    return paths


def read_part(path):
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def read_split(directory, paths):
    root = Path(directory).resolve()
    result = {}
    for section in SECTIONS:
        if paths[section] is None:
            if section == "overview":
                raise ValueError("分类记录缺少概览")
            result[section] = empty_section(section)
            continue
        path = (root / paths[section]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("分类记录路径不合法或缺失")
        result[section] = read_part(path)
    return result


class ResearchRecords:
    def __init__(self, records):
        self.records = records
        self.root = records.output / "research"
        self._shared = {}
        self._documents = {}

    def _batch_shared(self, path):
        from .records import _stamp, _jsonl
        files = sorted(path.parent.glob("http_attempts*.jsonl"))
        scores = self.records.output / "processed" / path.parent.name / "run_scores_v2.jsonl"
        signature = (tuple((str(p), _stamp(p)) for p in files), _stamp(scores))
        key = str(path)
        if key not in self._shared or self._shared[key][0] != signature:
            http = defaultdict(list)
            for p in files:
                for row in _jsonl(p):
                    http[row.get("run_id")].append(row)
            score = {r["run_id"]:r for r in _jsonl(scores)}
            self._shared[key] = (signature, http, score)
            if len(self._shared) > 4:
                del self._shared[next(iter(self._shared))]
        return self._shared[key]

    def ensure(self, ident, rid):
        from .records import _stamp, LOG_FILES
        kind, path = self.records._lookup(ident)
        if kind == "local":
            context = self.records._local_context(path)
            if rid not in context[3]:
                raise ValueError("该run不在所选批次中")
            source = self.records.output / "raw" / path.parent.name / rid
            shared, http, scores = self._batch_shared(path)
            signature = [_stamp(source / f) for f in sorted(LOG_FILES | {"events.jsonl"})]
            signature += [fingerprint(http.get(rid, [])), fingerprint(scores.get(rid)),
                          self.records.registry_hash, _stamp(path.parent / "run_plan.json"),
                          _stamp(path), self.records.active_experiment() == path.parent.name]
        else:
            self.records._sync(ident, scan=False)
            with self.records.index.connect() as db:
                row = db.execute("SELECT detail_path FROM runs WHERE batch_id=? AND run_id=?", (ident,rid)).fetchone()
            if row is None:
                raise ValueError("该run不在所选批次中")
            source = self.records._entry_path(self.records.output, row[0])
            signature = [_stamp(source)]
            http, scores = {}, {}
        signature = fingerprint([VERSION, DERIVATION_VERSION, PRICE_VERSION, signature])
        # Local IDs were validated as safe path components by the catalog.
        # Arbitrary uploaded IDs use a digest and stay mapped in the manifest.
        # Short physical keys also work under Windows' legacy path limit.
        # The export manifest retains the full human-readable batch/run IDs.
        batch_key = "b-" + fingerprint(ident)[:16]
        token = batch_key + "/r-" + fingerprint(rid)[:24]
        marker = self.root / "manifests" / (token + ".json")
        if marker.exists():
            meta = json.loads(marker.read_text(encoding="utf-8"))
            if meta["signature"] == signature:
                with self.records.index.connect() as db:
                    indexed = db.execute("SELECT 1 FROM research_runs WHERE batch_id=? AND run_id=?", (ident,rid)).fetchone()
                    if not indexed:
                        parts = {s:read_part(self.records._entry_path(self.root, meta["paths"][s])) if meta["paths"][s] else empty_section(s)
                                 for s in ("materials", "model_calls", "tools")}
                        self.records.index.put_research(db, ident, rid, parts)
                return meta["paths"]
        record = self.records.run(ident, rid)
        artifact = record.get("artifact") or {}
        document = None
        try:
            case = self.records.registry.get(artifact.get("case_id"))
            if case.contract_text_hash == (artifact.get("contract_text_hash") or (artifact.get("execution") or {}).get("contract_text_hash")):
                key = (case.contract_id, case.contract_text_hash)
                if key not in self._documents:
                    self._documents[key] = self.records.registry.document(case.case_id)
                document = self._documents[key]
        except (KeyError, ValueError, RuntimeError):
            pass
        parts = split_record(record, document=document, http=http.get(rid, []), score=scores.get(rid))
        if kind == "import" and source.name.endswith(".json"):
            # New imports already retain exact materials/http from the archive.
            imported = json.loads(source.read_text(encoding="utf-8"))
            if imported.get("format") == VERSION and imported.get("parts"):
                saved = read_split(source.parents[1], imported["parts"])
                parts["materials"] = saved["materials"]
                parts["http"] = saved["http"]
        paths = write_parts(self.root, token, parts)
        payload_offsets = {}
        for section in ("model_inputs", "model_responses"):
            offsets = {}
            if paths[section] is None:
                payload_offsets[section] = offsets
                continue
            with (self.root / paths[section]).open("rb") as source:
                while True:
                    offset = source.tell()
                    line = source.readline()
                    if not line:
                        break
                    row = json.loads(line)
                    offsets[row["model_call_id"]] = [offset, len(line)]
            payload_offsets[section] = offsets
        with self.records.index.connect() as db:
            self.records.index.put_research(db, ident, rid, parts)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp = marker.with_suffix(".tmp")
        temp.write_text(json.dumps(dict(signature=signature, paths=paths, payload_offsets=payload_offsets)), encoding="utf-8")
        temp.replace(marker)
        return paths

    def section(self, ident, rid, section="overview", *, offset=0, limit=100, call_id=None):
        if section not in SECTIONS:
            raise ValueError("未知实验数据类型")
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分页参数无效")
        with self.records.lock:
            paths = self.ensure(ident, rid)
            if paths[section] is None:
                return self.records._clean(empty_section(section) if section in JSON_SECTIONS else
                    dict(section=section, rows=[], total=0, offset=offset, limit=limit))
            path = self.records._entry_path(self.root, paths[section])
            if section in JSON_SECTIONS:
                value = read_part(path)
                if section == "overview":
                    value = {k:v for k,v in value.items() if k != "reconstruction"}
                    value["sections"] = list(SECTIONS)
                return self.records._clean(value)
            if call_id is not None and section in ("model_inputs", "model_responses"):
                marker = self.root / "manifests" / paths["overview"].removeprefix("overview/")
                meta = json.loads(marker.read_text(encoding="utf-8"))
                span = meta["payload_offsets"][section].get(call_id)
                rows = []
                if span and offset == 0:
                    with path.open("rb") as source:
                        source.seek(span[0])
                        rows = [json.loads(source.read(span[1]))]
                return self.records._clean(dict(section=section, rows=rows, total=int(bool(span)), offset=offset, limit=limit))
            # Read one entity at a time: never return every rendered input just
            # because the caller opened the model-call list.
            rows, total = [], 0
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if call_id is not None and row.get("model_call_id") != call_id:
                        continue
                    if offset <= total < offset + limit:
                        rows.append(row)
                    total += 1
            return self.records._clean(dict(section=section, rows=rows, total=total, offset=offset, limit=limit))
