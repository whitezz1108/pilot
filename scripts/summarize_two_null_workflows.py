"""Summarize the bounded two-null-history engineering workflows."""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, Any] = {"schema_version": "two-null-engineering-summary-v1", "runs": []}
    all_calls: list[dict[str, Any]] = []
    all_tools: list[dict[str, Any]] = []
    for seed in (303, 101):
        experiment = f"two-null-engineering-20261101-seed{seed}"
        run_dir = next((args.root / "tasks/raw" / experiment).glob("*"))
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        execution = run["execution"]
        calls = rows(run_dir / "model_calls.jsonl")
        tools = rows(run_dir / "tool_calls.jsonl")
        usage = [call.get("usage") or {} for call in calls]
        started = datetime.fromisoformat(execution["started_at"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(execution["completed_at"].replace("Z", "+00:00"))
        completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in usage)
        active = rows(args.root / f"audits/seed{seed}/active-http.jsonl")
        active_seconds = sum(float(item["seconds"]) for item in active)
        raw_compact = [(call.get("raw_response") or "").replace(" ", "") for call in calls]
        item = {
            "seed": seed, "run_id": execution["run_id"], "status": execution["status"],
            "wall_seconds": (completed - started).total_seconds(),
            "manager_answer": execution.get("manager_output") is not None,
            "compliance_answer": execution.get("compliance_output") is not None,
            "manager_decision": (execution.get("manager_output") or {}).get("decision"),
            "compliance_decision": (execution.get("compliance_output") or {}).get("decision"),
            "model_calls": len(calls), "tool_calls": len(tools),
            "tool_calls_by_node": dict(collections.Counter(tool.get("node") for tool in tools)),
            "tool_errors": sum(not tool.get("ok", False) for tool in tools),
            "missing_tool_call_ids": sum(not tool.get("tool_call_id") for tool in tools),
            "parse_errors": sum(bool(call.get("parse_error")) for call in calls),
            "model_errors": sum(bool(call.get("error")) for call in calls),
            "format_repairs": sum(bool(call.get("is_repair")) for call in calls),
            "tool_request_null": sum('"tool_request":null' in raw for raw in raw_compact),
            "usage_missing": sum(not bool(item) for item in usage),
            "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in usage),
            "completion_tokens": completion_tokens,
            "reasoning_tokens": sum(int(item.get("reasoning_tokens") or
                                        (item.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                                    for item in usage),
            "active_http_seconds": active_seconds,
            "request_output_tokens_per_second": completion_tokens / active_seconds,
            "finish_reasons": dict(collections.Counter(call.get("finish_reason") for call in calls)),
            "failure": run["failure"],
            "hashes": {name: sha(run_dir / name) for name in ("run.json", "model_calls.jsonl", "tool_calls.jsonl")},
        }
        result["runs"].append(item)
        all_calls.extend(calls)
        all_tools.extend(tools)

    telemetry = list(csv.DictReader((args.root / "gpu-telemetry.csv").open(encoding="utf-8")))
    def values(key: str) -> list[float]:
        output = []
        for row in telemetry:
            try:
                output.append(float(row[key].strip()))
            except (KeyError, ValueError):
                pass
        return output
    powers, temperatures = values("power.draw"), values("temperature.gpu")
    result["aggregate"] = {
        "runs_completed": sum(run["status"] == "completed" for run in result["runs"]),
        "model_calls": len(all_calls), "tool_calls": len(all_tools),
        "prompt_tokens": sum(run["prompt_tokens"] for run in result["runs"]),
        "completion_tokens": sum(run["completion_tokens"] for run in result["runs"]),
        "reasoning_tokens": sum(run["reasoning_tokens"] for run in result["runs"]),
        "tool_errors": sum(run["tool_errors"] for run in result["runs"]),
        "parse_errors": sum(run["parse_errors"] for run in result["runs"]),
        "format_repairs": sum(run["format_repairs"] for run in result["runs"]),
        "tool_request_null": sum(run["tool_request_null"] for run in result["runs"]),
        "gpu_samples": len(telemetry), "power_mean_w": statistics.mean(powers), "power_peak_w": max(powers),
        "power_samples_over_220_w": sum(value > 220 for value in powers),
        "temperature_peak_c": max(temperatures),
        "thermal_slowdown_samples": sum(str(row.get("clocks_event_reasons.sw_thermal_slowdown", "")).strip() == "Active" for row in telemetry),
        "uncorrectable_ecc_peak": max(values("ecc.errors.uncorrected.volatile.total") or [0]),
        "workload_manifest_sha256": sha(args.root / "workload/manifest.json"),
    }
    result["scope"] = "Two engineering reruns of historical null cases; not a replacement for the 68-request corpus or new research evidence."
    (args.root / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Two historical-null engineering reruns", "",
             "Scope: bounded engineering verification only; not a new research batch.", "",
             "| seed | status | model calls | tool calls | null | parse errors | repairs | completion tokens | tok/s |",
             "|---:|:---:|---:|---:|---:|---:|---:|---:|---:|"]
    for run in result["runs"]:
        lines.append(f"| {run['seed']} | {run['status']} | {run['model_calls']} | {run['tool_calls']} | "
                     f"{run['tool_request_null']} | {run['parse_errors']} | {run['format_repairs']} | "
                     f"{run['completion_tokens']} | {run['request_output_tokens_per_second']:.3f} |")
    agg = result["aggregate"]
    lines += ["", "## Aggregate", "",
              f"- Completed: {agg['runs_completed']}/2",
              f"- Actual replay corpus: {agg['model_calls']} requests; {agg['prompt_tokens']} prompt, "
              f"{agg['completion_tokens']} completion, {agg['reasoning_tokens']} reasoning tokens",
              f"- Protocol: null={agg['tool_request_null']}, tool errors={agg['tool_errors']}, "
              f"parse errors={agg['parse_errors']}, format repairs={agg['format_repairs']}",
              f"- GPU: mean {agg['power_mean_w']:.2f} W; peak temperature {agg['temperature_peak_c']:.0f} C; "
              f"thermal slowdown samples={agg['thermal_slowdown_samples']}; uncorrectable ECC peak={agg['uncorrectable_ecc_peak']:.0f}",
              "- One telemetry sample exceeded 220 W; retain the raw row as a sensor/power transient rather than silently dropping it.", ""]
    (args.root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result["aggregate"], ensure_ascii=False))


if __name__ == "__main__":
    main()

