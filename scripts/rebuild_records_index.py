"""Offline query-index backfill. Never loads credentials or starts experiments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from pilot01.experiment.cases import CaseRegistry
from pilot01.experiment.records import Records

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/experiment_console")
    parser.add_argument("--registry", type=Path, default=ROOT / "data/pilot01/clean80_v1/case_registry_v2.json")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild derived DB rows only; preserve all source files")
    parser.add_argument("--sections", action="store_true", help="Build type-first research files and provenance indices too")
    parser.add_argument("--batch", help="Only backfill this catalog batch ID")
    args = parser.parse_args()
    start = perf_counter()
    records = Records(args.output_root, CaseRegistry.load_json(args.registry))
    if args.rebuild:
        with records.index.connect() as db:
            db.execute("DELETE FROM tool_call_refs")
            db.execute("DELETE FROM research_runs")
            db.execute("DELETE FROM memo_versions")
            db.execute("DELETE FROM batches")  # Foreign-key cascade only deletes query-index rows.
    batches = records.list()
    for b in batches:
        if not b.get("error"):
            if args.batch and b["id"] != args.batch:
                continue
            records.batch(b["id"], limit=1)  # Check signatures even for idle historical batches.
            if args.sections:
                for row in records.indexed_rows(b["id"]):
                    records.research.ensure(b["id"], row["run_id"])
    errors = [b["id"] for b in batches if b.get("error")]
    print(json.dumps(dict(index=str(records.index.path), batches=len(batches),
                         runs=sum(b.get("count", 0) for b in batches), errors=errors,
                         seconds=round(perf_counter() - start, 3), paid_calls_started=0), ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
