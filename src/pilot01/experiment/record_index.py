"""Rebuildable SQLite query index; raw experiment files remain the evidence.

Only small display summaries belong here. Prompts/responses stay in per-run
files, never in a batch-sized database blob. No execution code reads this DB.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class RecordIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_stamp TEXT NOT NULL,
                    metadata TEXT NOT NULL, aggregate_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    case_id TEXT NOT NULL, condition_id TEXT NOT NULL, memo TEXT NOT NULL,
                    status TEXT NOT NULL, action TEXT, tokens REAL,
                    source_stamp TEXT NOT NULL, detail_path TEXT, summary TEXT NOT NULL,
                    PRIMARY KEY(batch_id, run_id), UNIQUE(batch_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS runs_status ON runs(batch_id, status, ordinal);
                CREATE INDEX IF NOT EXISTS runs_condition ON runs(batch_id, condition_id, ordinal);
                CREATE INDEX IF NOT EXISTS runs_memo ON runs(batch_id, memo, ordinal);
                CREATE TABLE IF NOT EXISTS memo_versions (
                    version_id TEXT PRIMARY KEY, memo_id TEXT NOT NULL,
                    case_id TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_runs (
                    batch_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    memo_version_id TEXT REFERENCES memo_versions(version_id),
                    source_version TEXT, PRIMARY KEY(batch_id,run_id)
                );
                CREATE TABLE IF NOT EXISTS model_call_refs (
                    batch_id TEXT NOT NULL, run_id TEXT NOT NULL, model_call_id TEXT NOT NULL,
                    role TEXT NOT NULL, invocation INTEGER NOT NULL, call_index INTEGER NOT NULL,
                    id_origin TEXT NOT NULL, requested_at TEXT, responded_at TEXT,
                    PRIMARY KEY(batch_id,run_id,model_call_id),
                    FOREIGN KEY(batch_id,run_id) REFERENCES research_runs(batch_id,run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS model_calls_role ON model_call_refs(batch_id,run_id,role,invocation,call_index);
                CREATE TABLE IF NOT EXISTS tool_call_refs (
                    batch_id TEXT NOT NULL, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    parent_model_call_id TEXT, tool TEXT NOT NULL, latency_seconds REAL,
                    PRIMARY KEY(batch_id,run_id,tool_call_id),
                    FOREIGN KEY(batch_id,run_id,parent_model_call_id) REFERENCES model_call_refs(batch_id,run_id,model_call_id)
                );
                CREATE TABLE IF NOT EXISTS source_relations (
                    batch_id TEXT NOT NULL, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    paragraph_id TEXT NOT NULL, relation TEXT NOT NULL, role TEXT, model_call_id TEXT,
                    PRIMARY KEY(batch_id,run_id,ordinal),
                    FOREIGN KEY(batch_id,run_id) REFERENCES research_runs(batch_id,run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS source_relations_kind ON source_relations(batch_id,run_id,relation);
                PRAGMA user_version=2;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def put_research(db, ident, rid, parts):
        m = parts["materials"]
        memo = m.get("memo")
        if memo:
            db.execute("INSERT OR IGNORE INTO memo_versions VALUES(?,?,?,?)", (
                m["memo_version_id"], memo.get("memo_id", ""), memo.get("case_id", ""), json.dumps(memo, ensure_ascii=False)))
        db.execute("DELETE FROM tool_call_refs WHERE batch_id=? AND run_id=?", (ident,rid))
        db.execute("DELETE FROM research_runs WHERE batch_id=? AND run_id=?", (ident,rid))
        db.execute("INSERT INTO research_runs VALUES(?,?,?,?)", (ident,rid,m.get("memo_version_id"),m.get("source_version")))
        db.executemany("INSERT INTO model_call_refs VALUES(?,?,?,?,?,?,?,?,?)", [
            (ident,rid,c["model_call_id"],c.get("role", ""),c.get("invocation",0),c.get("call_index",0),
             c["id_origin"],c.get("requested_at"),c.get("responded_at")) for c in parts["model_calls"]])
        db.executemany("INSERT INTO tool_call_refs VALUES(?,?,?,?,?,?)", [
            (ident,rid,t["tool_call_id"],t.get("parent_model_call_id"),t.get("tool", ""),t.get("latency_seconds")) for t in parts["tools"]])
        db.executemany("INSERT INTO source_relations VALUES(?,?,?,?,?,?,?)", [
            (ident,rid,i,r["paragraph_id"],r["relation"],r.get("role"),r.get("model_call_id"))
            for i,r in enumerate(m["relations"])])

    @staticmethod
    def put_run(db, ident, ordinal, stamp, row, detail_path=None):
        db.execute("""INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(batch_id,run_id) DO UPDATE SET
                ordinal=excluded.ordinal, case_id=excluded.case_id,
                condition_id=excluded.condition_id, memo=excluded.memo,
                status=excluded.status, action=excluded.action, tokens=excluded.tokens,
                source_stamp=excluded.source_stamp, detail_path=excluded.detail_path,
                summary=excluded.summary""",
            (ident, row["run_id"], ordinal, row.get("case_id") or "",
             row.get("condition") or "", row.get("memo") or "",
             row.get("status") or "unknown", row.get("action"), row.get("tokens"),
             stamp, detail_path, json.dumps(row, ensure_ascii=False)))

    @staticmethod
    def page(db, ident, *, offset=0, limit=100, status="", q="", condition=""):
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 200:
            raise ValueError("分页参数不正确；每页需要1至200项")
        clauses, args = ["batch_id=?"], [ident]
        for field, value in (("status", status), ("condition_id", condition)):
            if value:
                clauses.append(field + "=?")
                args.append(value)
        if q:
            if len(q) > 500:
                raise ValueError("搜索内容过长")
            clauses.append("(instr(lower(run_id),lower(?))>0 OR instr(lower(case_id),lower(?))>0)")
            args.extend([q, q])
        where = " AND ".join(clauses)
        total = db.execute("SELECT count(*) FROM runs WHERE " + where, args).fetchone()[0]
        rows = db.execute("SELECT summary FROM runs WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?",
                          [*args, limit, offset]).fetchall()
        return total, [json.loads(row[0]) for row in rows]
