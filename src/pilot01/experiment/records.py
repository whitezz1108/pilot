"""Read-only experiment browsing and isolated, inert result imports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .record_index import RecordIndex
from .research_records import ResearchRecords, VERSION as RESEARCH_FORMAT, split_record, join_record, write_parts, read_split, SECTIONS, empty_section

FORMAT = "pilot01-records-v1"
MAX_UPLOAD = 64 * 1024 * 1024
MAX_ZIP_UPLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
ZIP_FORMAT = "pilot01-records-zip-v2"
SPLIT_FORMAT = "pilot01-records-split-v2"
LOG_FILES = {"run.json", "model_calls.jsonl", "tool_calls.jsonl", "verification.jsonl"}


def _read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _stamp(path):
    try:
        s = path.stat()
        return (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)
    except FileNotFoundError:
        return None


def _jsonl(path):
    """Bounded to one log entry; ignore a live writer's incomplete tail."""
    try:
        with path.open("rb") as stream:
            for line in stream:
                if line.endswith(b"\n") and line.strip():
                    yield json.loads(line)
    except FileNotFoundError:
        return


def _row(record):
    row = dict(record["summary"], has_logs=bool(record.get("calls")), research=record.get("research"))
    research = record.get("research") or {}
    execution = (record.get("artifact") or {}).get("execution") or {}
    for role in ("manager", "compliance"):
        output = execution.get(role + "_output") or {}
        row[role + "_target_status"] = (output.get("target_clause_status") or {}).get(research.get("target"))
    final = execution.get("compliance_output")
    row["action_correct"] = (final.get("decision") == research["gold_action"]
        if final and research.get("gold_action") and row.get("status") == "completed" else None)
    calls = record.get("calls") or []
    row["first_started_at"] = calls[0].get("requested_at") if calls else None
    row["last_responded_at"] = calls[-1].get("responded_at") if calls else None
    row["manager_action"] = (execution.get("manager_output") or {}).get("decision")
    if not row["manager_action"]:
        row["manager_action"] = next(((c.get("parsed_output") or {}).get("decision")
            for c in reversed(calls) if c.get("role") == "manager" and (c.get("parsed_output") or {}).get("decision")), None)
    return row


def _aggregate(rows):
    counters = dict(Counter(r.get("status", "unknown") for r in rows))
    positive = [r for r in rows if r.get("memo") == "E1" and
                (r.get("research") or {}).get("gold", {}).get((r.get("research") or {}).get("target")) == "present"]
    return dict(total=len(rows), counters=counters,
        actions=dict(Counter(r.get("action") for r in rows if r.get("action"))),
        tokens=sum(r.get("tokens") or 0 for r in rows), reported=sum(r.get("tokens") is not None for r in rows),
        checked=sum(any(type(r.get(role + "_validation_pass")) is bool for role in ("manager", "compliance")) for r in rows),
        check_failed=sum(any(r.get(role + "_validation_pass") is False for role in ("manager", "compliance")) for r in rows),
        with_logs=sum(bool(r.get("has_logs")) for r in rows),
        e1=sum(r.get("memo") == "E1" and bool(r.get("research")) for r in rows), positive=len(positive),
        manager_recovered=sum(r.get("manager_target_status") == "present" for r in positive),
        compliance_recovered=sum(r.get("compliance_target_status") == "present" and r.get("status") == "completed" for r in positive),
        downstream_recovered=sum(r.get("compliance_target_status") == "present" and r.get("manager_target_status") != "present" and r.get("status") == "completed" for r in positive),
        answered=sum(type(r.get("action_correct")) is bool for r in rows),
        correct=sum(r.get("action_correct") is True for r in rows))


def _lines_bytes(data):
    # JSONL is LF framed; U+2028 within model output is not a line boundary.
    return [json.loads(line) for line in data.split(b"\n") if line.strip()]


def _safe_name(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", value)) and value not in (".", "..")


def _number(value):
    try:
        result = float(value)
        return result if result >= 0 and result < 1e15 else None
    except (ValueError, TypeError):
        return None


def summarize(record):
    """Derive display fields from actual artifacts; a CSV stays a CSV summary."""
    if not record.get("artifact"):
        return dict(record["summary"])
    a, calls, tools, checks = (record[k] for k in ("artifact", "calls", "tools", "verification"))
    execution, failure = a.get("execution") or {}, a.get("failure") or {}
    failed = bool(failure.get("protocol_failure"))
    status = "failed" if failed else "completed" if execution.get("status") == "completed" else "interrupted"
    final = execution.get("compliance_output") or {}
    row = dict(run_id=a["run_id"], case_id=a.get("case_id", ""),
               condition=a.get("condition_id", ""), memo=a.get("error_condition", ""),
               repeat=(a.get("repeat_index") or 0) + 1, status=status, action=final.get("decision"),
               calls=len(calls), tools=len(tools), tokens=sum((c.get("usage") or {}).get("total_tokens") or 0 for c in calls),
               missing_usage=sum(c.get("usage") is None for c in calls), error=failure.get("error"))
    for role in ("manager", "compliance"):
        verdicts = [v for v in checks if v.get("node") == role]
        row[role + "_validation_pass"] = verdicts[-1].get("validation_pass") if verdicts else None
        row[role + "_audit_failures"] = verdicts[-1].get("audit_failures", verdicts[-1].get("failures", [])) if verdicts else []
    return row


def _validate_record(record):
    if not isinstance(record, dict) or not isinstance(record.get("summary"), dict):
        raise ValueError("记录缺少summary汇总")
    rid = record["summary"].get("run_id")
    if not isinstance(rid, str) or not rid or len(rid) > 500:
        raise ValueError("run_id缺失或过长")
    a = record.get("artifact")
    if a is not None:
        if not isinstance(a, dict) or a.get("run_id") != rid or not isinstance(a.get("execution"), dict):
            raise ValueError("run.json结构或run_id不一致")
        if a.get("failure") is not None and not isinstance(a["failure"], dict):
            raise ValueError("failure必须是对象")
        for role in ("manager", "compliance"):
            output = a["execution"].get(role + "_output")
            if output is not None and (not isinstance(output, dict) or not isinstance(output.get("target_clause_status"), dict)):
                raise ValueError("节点输出结构不正确")
    for field in ("calls", "tools", "verification"):
        values = record.setdefault(field, [])
        if not isinstance(values, list) or len(values) > 10000 or not all(isinstance(v, dict) for v in values):
            raise ValueError(f"{field}必须是记录数组")
    for call in record["calls"]:
        if call.get("run_id") is not None and call["run_id"] != rid:
            raise ValueError("模型日志run_id与结果不一致")
        for field in ("rendered_input", "raw_response"):
            if call.get(field) is not None and not isinstance(call[field], str):
                raise ValueError(f"{field}必须是文本")
        if call.get("parsed_output") is not None and not isinstance(call["parsed_output"], dict):
            raise ValueError("parsed_output必须是对象")
        usage = call.get("usage")
        if usage is not None and (not isinstance(usage, dict) or
                                  usage.get("total_tokens") is not None and _number(usage["total_tokens"]) is None):
            raise ValueError("usage格式错误")
        if usage and usage.get("total_tokens") is not None:
            usage["total_tokens"] = _number(usage["total_tokens"])
    for tool in record["tools"]:
        if tool.get("run_id") is not None and tool["run_id"] != rid:
            raise ValueError("工具日志run_id与结果不一致")
        if tool.get("arguments") is not None and not isinstance(tool["arguments"], dict):
            raise ValueError("工具arguments必须是对象")
    for verdict in record["verification"]:
        for field in ("failures", "audit_failures"):
            if verdict.get(field) is not None and (not isinstance(verdict[field], list) or
                                                  not all(isinstance(v, str) for v in verdict[field])):
                raise ValueError("检查未通过项必须是文本数组")
    research = record.get("research")
    if research is not None and (not isinstance(research, dict) or not isinstance(research.get("gold"), dict)):
        raise ValueError("研究侧标注格式错误")
    # Unknown metadata remains inert text; never evaluate uploaded HTML or code.
    record["summary"] = summarize(record)
    return record


class Records:
    def __init__(self, output, registry, *, redact=lambda text: text, active_experiment=lambda: None):
        self.output = Path(output)
        self.imports = self.output / "imports"
        self.registry = registry
        self.registry_hash = "sha256:" + hashlib.sha256(json.dumps(registry.to_payload(), sort_keys=True).encode()).hexdigest()
        self.redact = redact
        self.active_experiment = active_experiment
        self.lock = threading.RLock()
        self.annotations = _read(Path(__file__).with_name("record_annotations.json"), {})
        self.index = RecordIndex(self.output / "index" / "records.sqlite3")
        self.contexts = OrderedDict()
        self.research = ResearchRecords(self)

    def _catalog(self):
        catalog = {}
        for path in (self.output / "manifests").glob("*/console_session.json"):
            if not _safe_name(path.parent.name) or not path.resolve().is_relative_to(self.output.resolve()):
                continue
            catalog["local:" + path.parent.name] = ("local", path)
        for path in self.imports.glob("*/bundle.json"):
            if path.resolve().is_relative_to(self.output.resolve()):
                catalog["import:" + path.parent.name] = ("import", path)
        for path in self.imports.glob("*/manifest.json"):
            if path.resolve().is_relative_to(self.output.resolve()) and not path.parent.name.startswith("."):
                catalog["import:" + path.parent.name] = ("import", path)
        return catalog

    def _lookup(self, ident):
        item = self._catalog().get(ident)
        if item is None:
            raise ValueError("找不到该批次，请刷新实验记录列表")
        return item

    def _local_context(self, path):
        signature = (_stamp(path), _stamp(path.parent / "run_plan.json"))
        cached = self.contexts.get(str(path))
        if cached and cached[0] == signature:
            self.contexts.move_to_end(str(path))
            return cached[1]
        session = _read(path)
        ids = session.get("run_ids", [])
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not all(_safe_name(i) for i in ids):
            raise ValueError("本地批次run清单无效")
        plan = _read(path.parent / "run_plan.json", {})
        context = (session, plan, ids, set(ids), {j["run_id"]: j for j in plan.get("jobs", [])})
        self.contexts[str(path)] = (signature, context)
        if len(self.contexts) > 8:
            self.contexts.popitem(last=False)
        return context

    def _local_run(self, path, rid, *, logs=True, context=None, compact=False):
        session, plan, ids, selected, jobs = context or self._local_context(path)
        if rid not in selected:
            raise ValueError("该run不在所选批次中")
        directory = self.output / "raw" / path.parent.name / rid
        if not directory.resolve().is_relative_to(self.output.resolve()):
            raise ValueError("来源目录不合法")
        record = dict(summary=dict(run_id=rid), artifact=_read(directory / "run.json"), calls=[], tools=[], verification=[])
        if logs:
            for key, name in (("calls", "model_calls.jsonl"), ("tools", "tool_calls.jsonl"), ("verification", "verification.jsonl")):
                p = directory / name
                if p.exists():
                    if compact and key == "calls":
                        record[key] = [dict(usage=c.get("usage"), role=c.get("role"),
                            requested_at=c.get("requested_at"), responded_at=c.get("responded_at"),
                            parsed_output=dict(decision=(c.get("parsed_output") or {}).get("decision"))) for c in _jsonl(p)]
                    elif compact and key == "tools":
                        record[key] = [{} for _ in _jsonl(p)]
                    else:
                        record[key] = list(_jsonl(p))
        if record["artifact"]:
            record["summary"] = summarize(record)
            a = record["artifact"]
            if plan.get("registry_fingerprint") == self.registry_hash:
                case = self.registry.get(a["case_id"])
                record["research"] = dict(gold={k: v.value for k, v in case.gold_target_clause_status.items()},
                    target=case.target_category, gold_action=case.gold_action.value,
                    provenance="与冻结plan的registry_fingerprint一致，仅研究侧对照")
            annotations = self.annotations.get("runs", {}).get(rid)
            if not compact and annotations and annotations.get("artifact_sha256") == hashlib.sha256((directory / "run.json").read_bytes()).hexdigest():
                record["annotations"] = annotations
        else:
            job = jobs.get(rid, {})
            record["summary"].update(case_id=job.get("case_id", ""), condition=job.get("condition_id", ""),
                memo=job.get("error_condition", ""), status=("running" if self.active_experiment() == path.parent.name
                    else "interrupted") if directory.exists() else "waiting",
                calls=len(record["calls"]), tools=len(record["tools"]), tokens=None, action=None, error=None)
        if not compact:
            # Do not attach every historical translation to every new run.
            summaries = {c.get("summary") for event in ((record.get("artifact") or {}).get("execution") or {}).get("events", [])
                         for c in ((event.get("payload") or {}).get("output") or {}).get("claims", [])}
            record["translations"] = {k:v for k,v in self.annotations.get("claims", {}).items() if k in summaries}
        return record

    def _metadata(self, path, context):
        session, plan, ids, selected, _ = context
        jobs = [job for job in plan.get("jobs", []) if job.get("run_id") in selected]
        values = {key: set() for key in ("a", "v", "e", "d")}
        for job in jobs:
            if type(job.get("source_access")) is bool:
                values["a"].add("A1" if job["source_access"] else "A0")
            if type(job.get("verification_required")) is bool:
                level = "V1" if job["verification_required"] else "V0"
                values["v"].add("V2" if job.get("recovery_required") is True else level)
            if job.get("error_condition") in ("E0", "E1"):
                values["e"].add(job["error_condition"])
            match = re.search(r"D[012]$", job.get("condition_id", ""))
            if match:
                values["d"].add(match.group())
        return dict(experiment_id=path.parent.name, created_at=session.get("created_at"),
                    started_at=session.get("created_at"), initiated_by=session.get("initiated_by"),
                    identity_source=session.get("identity_source", "not_recorded"),
                    condition_values={k: sorted(v) for k, v in values.items()},
                    condition_ids=sorted({j.get("condition_id") for j in jobs if j.get("condition_id")}),
                    factor_selection=session.get("factor_selection"), repeat_count=plan.get("repeat_count"),
                    actual_contracts=len({j["case_id"] for j in jobs}), experiment_version=plan.get("experiment_version"),
                    finished_at=session.get("finished_at"), full_plan_count=session.get("full_plan_count"),
                    selected_count=len(ids), source="系统原始日志", runtime_source="冻结批次")

    def _clean(self, value):
        return json.loads(self.redact(json.dumps(value, ensure_ascii=False)))

    def _split_record(self, directory, ordinal, record, parts=None):
        # File names do not depend on imported (potentially arbitrary) run IDs.
        relative = f"records/{ordinal:06d}.json"
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        parts = parts or split_record(record)
        parts = self._clean(parts)
        paths = write_parts(directory, f"{ordinal:06d}", parts)
        data = json.dumps(dict(format=RESEARCH_FORMAT, parts=paths, run_id=record["summary"]["run_id"])).encode("utf-8")
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(target)
        return dict(run_id=record["summary"]["run_id"], path=relative)

    def _read_import_record(self, path):
        value = _read(path)
        if value.get("format") == RESEARCH_FORMAT:
            return join_record(read_split(path.parents[1], value["parts"]))
        return value

    def _save_manifest(self, directory, metadata, entries):
        target = directory / "manifest.json"
        temp = directory / "manifest.tmp"
        temp.write_text(self.redact(json.dumps(dict(format=SPLIT_FORMAT, batch=metadata, entries=entries),
                                              ensure_ascii=False)), encoding="utf-8")
        temp.replace(target)  # Publish only after every run was validated and written.
        return target

    def _import_manifest(self, path):
        if path.name == "bundle.json":
            manifest = path.with_name("manifest.json")
            if not manifest.exists():
                bundle = _read(path)
                entries = [self._split_record(path.parent, i, _validate_record(record))
                           for i, record in enumerate(bundle["runs"])]
                self._save_manifest(path.parent, bundle["batch"], entries)
            path = manifest
        return path

    def _entry_path(self, directory, relative):
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("记录文件路径不合法或缺失")
        return path

    def _local_stamps(self, path, selected):
        """Use directory-entry metadata instead of thousands of Path.resolve/stat calls.

        On Windows scandir supplies file timestamps/sizes from the directory
        listing. Symlinks still go through the ordinary containment checks.
        """
        root = self.output / "raw" / path.parent.name
        if not root.resolve().is_relative_to(self.output.resolve()):
            raise ValueError("来源目录不合法")
        result = {}
        try:
            with os.scandir(root) as directories:
                for entry in directories:
                    if entry.name not in selected or not entry.is_dir():
                        continue
                    if entry.is_symlink() and not Path(entry.path).resolve().is_relative_to(self.output.resolve()):
                        raise ValueError("来源目录不合法")
                    stamps = {}
                    with os.scandir(entry.path) as files:
                        for file in files:
                            if file.name in LOG_FILES:
                                if file.is_symlink():
                                    if not Path(file.path).resolve().is_relative_to(self.output.resolve()):
                                        raise ValueError("来源文件不合法")
                                    stamps[file.name] = _stamp(Path(file.path))
                                else:
                                    s = file.stat()
                                    stamps[file.name] = (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)
                    result[entry.name] = [True, *[stamps.get(name) for name in sorted(LOG_FILES)]]
        except FileNotFoundError:
            pass
        return result

    def _sync(self, ident, *, scan=True):
        """Reconcile changed files only, with one shared context per batch.

        A history listing reuses finished batch summaries. A batch refresh checks
        file signatures (not file contents); active batches always check changes.
        Index updates commit as one transaction, so readers never see half a page.
        """
        kind, path = self._lookup(ident)
        if kind == "import":
            path = self._import_manifest(path)
        source_stamp = json.dumps([_stamp(path), _stamp(path.parent / "run_plan.json") if kind == "local" else None,
                                   self.registry_hash, self.active_experiment() == path.parent.name])
        with self.index.connect() as db:
            previous = db.execute("SELECT * FROM batches WHERE id=?", (ident,)).fetchone()
            same_source = previous and previous["source_stamp"] == source_stamp
            if same_source and not scan and self.active_experiment() != path.parent.name:
                return
            existing = {r["run_id"]: r for r in db.execute(
                "SELECT run_id,source_stamp,ordinal FROM runs WHERE batch_id=?", (ident,))}
            if kind == "local":
                context = self._local_context(path)
                metadata = self._metadata(path, context)
                entries = [dict(run_id=rid) for rid in context[2]]
                plan_stamp = _stamp(path.parent / "run_plan.json")
                file_stamps = self._local_stamps(path, context[3])
            else:
                manifest = _read(path)
                metadata, entries = manifest["batch"], manifest["entries"]
                context = None
            changed = not same_source
            if not previous:
                db.execute("INSERT INTO batches VALUES(?,?,?,?,?)", (ident, kind, "", "{}", "{}"))
            # Changed membership/order is rare; rebuild these derived rows safely.
            if [(e["run_id"], i) for i, e in enumerate(entries)] != [(r["run_id"], r["ordinal"]) for r in sorted(existing.values(), key=lambda r:r["ordinal"])]:
                db.execute("DELETE FROM runs WHERE batch_id=?", (ident,))
                existing = {}
                changed = True
            for i, entry in enumerate(entries):
                rid = entry["run_id"]
                detail_path = None
                if kind == "local":
                    signature = file_stamps.get(rid, [False, *[None for _ in LOG_FILES]])
                    unfinished = signature[0] and signature[1 + sorted(LOG_FILES).index("run.json")] is None
                    stamp = json.dumps([signature, plan_stamp, self.registry_hash,
                                        unfinished and self.active_experiment() == path.parent.name])
                else:
                    detail = self._entry_path(path.parent, entry["path"])
                    detail_path = str(detail.relative_to(self.output))
                    stamp = json.dumps(_stamp(detail))
                if rid in existing and existing[rid]["source_stamp"] == stamp:
                    continue
                if kind == "local":
                    record = self._local_run(path, rid, context=context, compact=True)
                else:
                    record = self._read_import_record(detail)
                row = self._clean(_row(record))
                self.index.put_run(db, ident, i, stamp, row, detail_path)
                changed = True
            if changed:
                rows = [json.loads(r[0]) for r in db.execute("SELECT summary FROM runs WHERE batch_id=? ORDER BY ordinal", (ident,))]
                if not metadata.get("condition_values"):
                    values = {k: set() for k in ("a", "v", "e", "d")}
                    for row in rows:
                        for key, prefix in (("a", "A"), ("v", "V"), ("d", "D")):
                            match = re.search(prefix + (r"[012]" if key == "d" else r"[01]"), str(row.get("condition") or ""))
                            if match:
                                values[key].add(match.group())
                        if row.get("memo") in ("E0", "E1"):
                            values["e"].add(row["memo"])
                    metadata["condition_values"] = {k: sorted(v) for k, v in values.items()}
                db.execute("UPDATE batches SET source_stamp=?,metadata=?,aggregate_json=? WHERE id=?",
                           (source_stamp, json.dumps(self._clean(metadata), ensure_ascii=False),
                            json.dumps(_aggregate(rows)), ident))

    def list(self):
        with self.lock:
            items = []
            for ident, (kind, path) in self._catalog().items():
                try:
                    self._sync(ident, scan=False)
                    with self.index.connect() as db:
                        row = db.execute("SELECT * FROM batches WHERE id=?", (ident,)).fetchone()
                    a = json.loads(row["aggregate_json"])
                    items.append(dict(id=ident, **json.loads(row["metadata"]), count=a["total"], counters=a["counters"], kind=kind))
                except (ValueError, OSError, KeyError, TypeError):
                    items.append(dict(id=ident, experiment_id=path.parent.name, count=0, kind=kind, error="记录无法读取"))
            return self._clean(sorted(items, key=lambda x: x.get("created_at") or "", reverse=True))

    def batch(self, ident, *, offset=0, limit=100, status="", q="", condition=""):
        with self.lock:
            self._sync(ident)
            with self.index.connect() as db:
                b = db.execute("SELECT metadata,aggregate_json FROM batches WHERE id=?", (ident,)).fetchone()
                total, rows = self.index.page(db, ident, offset=offset, limit=limit, status=status, q=q, condition=condition)
            return self._clean(dict(id=ident, batch=json.loads(b[0]), aggregate=json.loads(b[1]), rows=rows,
                        total=total, offset=offset, limit=limit))

    def indexed_rows(self, ident):
        """Internal compact state snapshot, without any prompt/response payloads."""
        with self.lock:
            self._sync(ident)
            with self.index.connect() as db:
                return self._clean([json.loads(r[0]) for r in db.execute("SELECT summary FROM runs WHERE batch_id=? ORDER BY ordinal", (ident,))])

    def run(self, ident, rid):
        with self.lock:
            kind, path = self._lookup(ident)
            if kind == "local":
                record = self._local_run(path, rid)
            else:
                # Bootstrap old bundles once. Afterwards a PK lookup opens ONE file.
                self._sync(ident, scan=False)
                with self.index.connect() as db:
                    row = db.execute("SELECT detail_path FROM runs WHERE batch_id=? AND run_id=?", (ident, rid)).fetchone()
                if row is None:
                    raise ValueError("该run不在所选记录中")
                record = self._read_import_record(self._entry_path(self.output, row[0]))
            return self._clean(record)

    def _export_context(self, ident):
        with self.lock:
            kind, path = self._lookup(ident)
            if kind == "local":
                context = self._local_context(path)
                batch, ids = self._metadata(path, context), context[2]
            else:
                path = self._import_manifest(path)
                manifest = _read(path)
                batch = manifest["batch"]
                context = {e["run_id"]: e["path"] for e in manifest["entries"]}
                ids = list(context)
            batch = self._clean(batch)
        return kind, path, batch, ids, context

    def _export_run(self, ident, kind, path, rid, context):
        record = (self._local_run(path, rid, context=context) if kind == "local" else
                  self._read_import_record(self._entry_path(path.parent, context[rid])))
        return self._clean(record)

    def export(self, ident):
        """Legacy JSON compatibility. The UI uses the bounded ZIP export."""
        kind, path, batch, ids, context = self._export_context(ident)
        return json.dumps(dict(format=FORMAT, batch=batch,
            runs=[self._export_run(ident, kind, path, rid, context) for rid in ids]), ensure_ascii=False).encode("utf-8")

    def export_zip(self, ident):
        """Disk-backed archive; memory is bounded to one run, not the batch."""
        kind, path, batch, ids, context = self._export_context(ident)
        output = tempfile.TemporaryFile()
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                entries = []
                for i, rid in enumerate(ids):
                    with self.lock:
                        paths = self.research.ensure(ident, rid)
                        exported_paths = {}
                        for section, name in paths.items():
                            if name is None:
                                exported_paths[section] = None
                                continue
                            source = self._entry_path(self.research.root, name)
                            batch_name = batch.get("experiment_id", "batch")
                            batch_name = batch_name if _safe_name(batch_name) else "batch"
                            run_name = rid.removeprefix(batch_name + "__")
                            run_name = run_name if _safe_name(run_name) else f"run-{i:06d}"
                            archive_name = f"{section}/{batch_name}/{i:06d}--{run_name}{source.suffix}"
                            exported_paths[section] = archive_name
                            # Redact on every export, including cached derived files.
                            archive.writestr(archive_name, self.redact(source.read_text(encoding="utf-8")).encode("utf-8"))
                    entries.append(dict(run_id=rid, parts=exported_paths))
                archive.writestr("manifest.json", json.dumps(dict(format=RESEARCH_FORMAT, batch=batch, entries=entries), ensure_ascii=False).encode("utf-8"))
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise

    def import_bytes(self, filename, data):
        return self.import_file(filename, io.BytesIO(data), len(data))

    def import_file(self, filename, stream, size):
        ext = Path(filename).suffix.lower()
        maximum = MAX_ZIP_UPLOAD if ext == ".zip" else MAX_UPLOAD
        if not 0 < size <= maximum:
            raise ValueError("ZIP不能超过512MB；CSV、JSON不能超过64MB")
        if ext == ".zip":
            return self._import_zip(filename, stream)
        data = stream.read(MAX_UPLOAD + 1)
        if ext == ".csv":
            bundle = self._csv(data)
        elif ext == ".json":
            bundle = json.loads(data.decode("utf-8-sig"))
            if isinstance(bundle, dict) and "run_id" in bundle and "execution" in bundle:
                bundle = dict(format=FORMAT, batch={}, runs=[dict(summary=dict(run_id=bundle["run_id"]), artifact=bundle)])
        else:
            raise ValueError("支持CSV、完整记录包JSON、单run.json或原始日志ZIP；不接受HTML")
        if not isinstance(bundle, dict) or bundle.get("format") != FORMAT or not isinstance(bundle.get("batch"), dict):
            raise ValueError("不是实验记录包，请从实验记录页导出完整JSON记录包")
        runs = bundle.get("runs")
        if not isinstance(runs, list) or not 1 <= len(runs) <= 10000:
            raise ValueError("记录包需要1至10000条run")
        return self._store_import(filename, stream, bundle["batch"], iter(runs))

    def _store_import(self, filename, stream, metadata, records):
        upload_id = uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        metadata = dict(metadata)
        metadata.update(source="上传：" + Path(filename).name, imported_at=created)
        if not metadata.get("started_at") and not metadata.get("created_at"):
            metadata["time_source"] = "imported_at"
        metadata.setdefault("created_at", created)
        metadata.setdefault("experiment_id", Path(filename).name)
        with self.lock:
            self.imports.mkdir(parents=True, exist_ok=True)
            # TemporaryDirectory owns only its freshly created child. Invalid
            # uploads never publish a half-imported manifest to other readers.
            with tempfile.TemporaryDirectory(prefix=".pending-", dir=self.imports) as temp:
                directory = Path(temp) / upload_id
                directory.mkdir()
                entries, seen = [], set()
                for i, record in enumerate(records):
                    if i >= 10000:
                        raise ValueError("记录包最多包含10000条run")
                    parts = record.pop("_research_parts", None)
                    record = _validate_record(record)
                    rid = record["summary"]["run_id"]
                    if rid in seen:
                        raise ValueError("上传内容存在重复run_id")
                    seen.add(rid)
                    entries.append(self._split_record(directory, i, record, parts))
                if not entries:
                    raise ValueError("记录包需要1至10000条run")
                metadata["selected_count"] = len(entries)
                self._save_manifest(directory, metadata, entries)
                stream.seek(0)
                with (directory / ("source" + Path(filename).suffix.lower())).open("wb") as target:
                    shutil.copyfileobj(stream, target, length=1024 * 1024)
                directory.rename(self.imports / upload_id)
        return dict(id="import:" + upload_id, count=len(entries), message="已导入；没有启动实验或调用模型")

    def _import_zip(self, filename, stream):
        try:
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if len(infos) > 100000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                    raise ValueError("ZIP展开后不能超过2GB或100000个文件")
                names = {}
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
                        raise ValueError("ZIP包含不安全路径")
                    normalized = str(path)
                    if normalized in names:
                        raise ValueError("ZIP中有重复文件")
                    if info.file_size > 128 * 1024 * 1024:
                        raise ValueError("ZIP中单个记录文件不能超过128MB")
                    names[normalized] = info
                if "manifest.json" in names:
                    if names["manifest.json"].file_size > 4 * 1024 * 1024:
                        raise ValueError("ZIP清单过大")
                    manifest = json.loads(archive.read(names["manifest.json"]))
                    if manifest.get("format") not in (ZIP_FORMAT, RESEARCH_FORMAT) or not isinstance(manifest.get("batch"), dict):
                        raise ValueError("ZIP记录清单格式错误")
                    entries = manifest.get("entries")
                    if not isinstance(entries, list) or not 1 <= len(entries) <= 10000:
                        raise ValueError("ZIP需要1至10000条run")
                    def records():
                        used = set()
                        for entry in entries:
                            if manifest["format"] == RESEARCH_FORMAT:
                                paths = entry.get("parts")
                                if not isinstance(paths, dict) or set(paths) != set(SECTIONS):
                                    raise ValueError("分类记录清单缺少数据类型")
                                parts = {}
                                for section, relative in paths.items():
                                    if relative is None and section != "overview":
                                        parts[section] = empty_section(section)
                                        continue
                                    if (not isinstance(relative, str) or relative not in names or relative in used or
                                            not relative.startswith(section + "/")):
                                        raise ValueError("分类记录路径缺失、重复或类型不符")
                                    used.add(relative)
                                    data = archive.read(names[relative])
                                    parts[section] = json.loads(data) if section in ("overview", "materials") else _lines_bytes(data)
                                if sum(names[p].file_size for p in paths.values() if p is not None) > 128 * 1024 * 1024:
                                    raise ValueError("ZIP中单轮记录不能超过128MB")
                                record = join_record(parts)
                                if (record.get("summary") or {}).get("run_id") != entry.get("run_id"):
                                    raise ValueError("ZIP清单与run_id不一致")
                                record["_research_parts"] = parts
                                yield record
                                continue
                            relative = entry.get("path")
                            if relative not in names or relative in used or not relative.startswith("runs/") or not relative.endswith("/record.json"):
                                raise ValueError("ZIP记录路径缺失或重复")
                            used.add(relative)
                            record = json.loads(archive.read(names[relative]))
                            if (record.get("summary") or {}).get("run_id") != entry.get("run_id"):
                                raise ValueError("ZIP清单与run_id不一致")
                            yield record
                    return self._store_import(filename, stream, manifest["batch"], records())
                groups = {}
                for name, info in names.items():
                    path = PurePosixPath(name)
                    if info.is_dir() or path.name not in LOG_FILES:
                        continue
                    rid = path.parent.name
                    if not _safe_name(rid):
                        raise ValueError("ZIP中的run目录名称不合法")
                    files = groups.setdefault(rid, {})
                    if path.name in files:
                        raise ValueError("ZIP中有重复run文件")
                    files[path.name] = info
                metadata, batches = {}, set()
                def records():
                    for rid, files in groups.items():
                        if sum(f.file_size for f in files.values()) > 128 * 1024 * 1024:
                            raise ValueError("ZIP中单轮记录不能超过128MB")
                        artifact = json.loads(archive.read(files["run.json"])) if "run.json" in files else None
                        if artifact:
                            batches.add(artifact.get("experiment_id", ""))
                            if len(batches) > 1:
                                raise ValueError("ZIP中包含多个实验批次，请每次上传一个批次")
                        yield dict(summary=dict(run_id=rid, case_id=artifact.get("case_id", "") if artifact else rid,
                            status="interrupted"), artifact=artifact,
                            calls=_lines_bytes(archive.read(files["model_calls.jsonl"])) if "model_calls.jsonl" in files else [],
                            tools=_lines_bytes(archive.read(files["tool_calls.jsonl"])) if "tool_calls.jsonl" in files else [],
                            verification=_lines_bytes(archive.read(files["verification.jsonl"])) if "verification.jsonl" in files else [])
                # Metadata is filled as the iterator advances, before manifest publication.
                # _store_import copies it early, so use the first artifact for identity.
                first = next((f["run.json"] for f in groups.values() if "run.json" in f), None)
                if first:
                    metadata["experiment_id"] = json.loads(archive.read(first)).get("experiment_id", "ZIP原始记录")
                return self._store_import(filename, stream, metadata, records())
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError("无法读取ZIP，请使用未加密的实验记录压缩包") from exc
        except (KeyError, TypeError, AttributeError, IndexError) as exc:
            raise ValueError("ZIP中的分类记录结构不完整或字段类型错误") from exc

    def _csv(self, data):
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or not {"run_id", "case_id", "status"} <= set(reader.fieldnames):
            raise ValueError("CSV至少需要run_id、case_id、status列")
        records = []
        for line in reader:
            if None in line:
                raise ValueError("CSV列数与表头不一致")
            row = {k: v for k, v in line.items() if k is not None}
            for key in ("calls", "tools", "tokens", "elapsed", "repeat"):
                row[key] = _number(row.get(key))
            for role in ("manager", "compliance"):
                value = str(row.get(role + "_validation_pass", "")).lower()
                row[role + "_validation_pass"] = True if value == "true" else False if value == "false" else None
                value = row.get(role + "_audit_failures", "[]") or "[]"
                try:
                    parsed = json.loads(value.replace("'", '"'))
                    row[role + "_audit_failures"] = parsed if isinstance(parsed, list) else [value]
                except ValueError:
                    row[role + "_audit_failures"] = [value]
            records.append(dict(summary=row, artifact=None, calls=[], tools=[], verification=[]))
        return dict(format=FORMAT, batch=dict(source="CSV汇总"), runs=records)

