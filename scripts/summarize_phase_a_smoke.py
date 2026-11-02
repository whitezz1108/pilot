"""Summarize a dual-role Stage-A workflow smoke without changing its evidence."""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any


def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def max_running(path: Path) -> float:
    pattern = re.compile(r"^sglang:num_running_reqs\{.*\}\s+([0-9.eE+-]+)$")
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values.append(float(match.group(1)))
    return max(values, default=0.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    attempt = args.root / "attempts" / args.experiment
    raw = args.root / "tasks" / "raw" / args.experiment
    runs = []
    all_calls: list[dict[str, Any]] = []
    all_tools: list[dict[str, Any]] = []
    for directory in sorted(path for path in raw.iterdir() if path.is_dir()):
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        calls = jsonl(directory / "model_calls.jsonl")
        tools = jsonl(directory / "tool_calls.jsonl")
        raw_compact = [json.dumps(call.get("raw_response"), ensure_ascii=False, separators=(",", ":")) for call in calls]
        runs.append({
            "run_id": record["run_id"], "status": record["execution"]["status"],
            "failure": record["failure"], "model_calls": len(calls), "tool_calls": len(tools),
            "manager_answer": record["execution"].get("manager_output") is not None,
            "compliance_answer": record["execution"].get("compliance_output") is not None,
            "parse_errors": sum(bool(call.get("parse_error")) for call in calls),
            "model_errors": sum(bool(call.get("error")) for call in calls),
            "format_repairs": sum(bool(call.get("is_repair")) for call in calls),
            "tool_request_null": sum('"tool_request":null' in value for value in raw_compact),
            "tool_errors": sum(not tool.get("ok", False) for tool in tools),
            "missing_tool_call_ids": sum(not tool.get("tool_call_id") for tool in tools),
            "usage_missing": sum(not bool(call.get("usage")) for call in calls),
        })
        all_calls.extend(calls)
        all_tools.extend(tools)
    wire = jsonl(attempt / "audit" / "wire.jsonl")
    active = jsonl(attempt / "audit" / "active-http.jsonl")
    tool_ids = [tool.get("tool_call_id") for tool in all_tools if tool.get("tool_call_id")]
    tool_text = "\n".join(json.dumps(tool, ensure_ascii=False) for tool in all_tools)
    aggregate = {
        "planned_runs": 8,
        "runs_present": len(runs),
        "completed_runs": sum(run["status"] == "completed" for run in runs),
        "failed_runs": sum(run["status"] == "failed" for run in runs),
        "model_calls": len(all_calls),
        "completed_http_calls": len(active),
        "tool_calls": len(all_tools),
        "parse_errors": sum(run["parse_errors"] for run in runs),
        "model_errors": sum(run["model_errors"] for run in runs),
        "format_repairs": sum(run["format_repairs"] for run in runs),
        "tool_request_null": sum(run["tool_request_null"] for run in runs),
        "tool_errors": sum(run["tool_errors"] for run in runs),
        "missing_tool_call_ids": sum(run["missing_tool_call_ids"] for run in runs),
        "duplicate_tool_call_ids": len(tool_ids) - len(set(tool_ids)),
        "usage_missing": sum(run["usage_missing"] for run in runs),
        "cursor_or_query_mismatch_mentions": len(re.findall(r"cursor.{0,30}mismatch|query.{0,30}mismatch", tool_text, re.I)),
        "endpoint_requests": dict(collections.Counter(row.get("url") for row in wire)),
        "manager_max_running_requests_observed": max_running(attempt / "telemetry" / "manager-metrics.prom"),
        "compliance_max_running_requests_observed": max_running(attempt / "telemetry" / "compliance-metrics.prom"),
    }
    aggregate["passed"] = (
        aggregate["completed_runs"] == aggregate["planned_runs"]
        and not aggregate["failed_runs"]
        and not any(aggregate[key] for key in (
            "parse_errors", "model_errors", "format_repairs", "tool_request_null", "tool_errors",
            "missing_tool_call_ids", "duplicate_tool_call_ids", "usage_missing", "cursor_or_query_mismatch_mentions",
        ))
        and aggregate["manager_max_running_requests_observed"] >= 2
        and aggregate["compliance_max_running_requests_observed"] >= 2
    )
    result = {"schema_version": "phase-a-dual-gpu-smoke-v1", "scope": "engineering smoke; not research evidence",
              "experiment": args.experiment, "aggregate": aggregate, "runs": runs}
    (attempt / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Stage-A dual-GPU workflow smoke", "", "Engineering smoke only; not research evidence.", "",
             f"- Result: **{'PASS' if aggregate['passed'] else 'FAIL'}**",
             f"- Runs: {aggregate['completed_runs']}/{aggregate['planned_runs']} completed; {aggregate['failed_runs']} failed",
             f"- Calls: {aggregate['model_calls']} model; {aggregate['tool_calls']} tools",
             f"- Observed server concurrency: Manager {aggregate['manager_max_running_requests_observed']:g}; Compliance {aggregate['compliance_max_running_requests_observed']:g}",
             f"- Protocol counters: null={aggregate['tool_request_null']}, parse={aggregate['parse_errors']}, repair={aggregate['format_repairs']}, tool errors={aggregate['tool_errors']}, missing IDs={aggregate['missing_tool_call_ids']}, duplicate IDs={aggregate['duplicate_tool_call_ids']}, cursor/query mismatch={aggregate['cursor_or_query_mismatch_mentions']}", "",
             "| run | status | model calls | tool calls | failure |", "|---|:---:|---:|---:|---|"]
    for run in runs:
        failure = run["failure"].get("error") or ""
        lines.append(f"| {run['run_id']} | {run['status']} | {run['model_calls']} | {run['tool_calls']} | {failure} |")
    (attempt / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False))


if __name__ == "__main__":
    main()
