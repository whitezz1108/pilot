"""Summarize the isolated native-v2 protocol reruns without dumping prompts."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_run(run_dir: Path, audit_dir: Path) -> dict:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    models = read_jsonl(run_dir / "model_calls.jsonl")
    tools = read_jsonl(run_dir / "tool_calls.jsonl")
    wire = read_jsonl(audit_dir / "wire.jsonl")
    wire_by_id = {row.get("model_call_id"): row["payload"] for row in wire}

    per_role_models: dict[str, int] = Counter(row["role"] for row in models)
    per_role_tools: dict[str, int] = Counter(row["node"] for row in tools)
    tool_names: dict[str, Counter] = defaultdict(Counter)
    for row in tools:
        tool_names[row["node"]][row["tool"]] += 1

    max_native_calls = 0
    final_requests = []
    parallel_flags = []
    for row in models:
        response = row.get("provider_response") or {}
        choices = response.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        max_native_calls = max(max_native_calls, len(message.get("tool_calls") or []))
        payload = wire_by_id.get(row["model_call_id"], {})
        if "parallel_tool_calls" in payload:
            parallel_flags.append(payload["parallel_tool_calls"])
        if row.get("finish_reason") == "stop":
            final_requests.append(
                {
                    "role": row["role"],
                    "call_index": row["call_index"],
                    "tools_key_present": "tools" in payload,
                    "tool_count": len(payload.get("tools") or []),
                    "tool_choice": payload.get("tool_choice"),
                }
            )

    exposed: dict[str, set[str]] = defaultdict(set)
    open_checks = []
    for row in tools:
        node = row["node"]
        if row["tool"] == "search_contract":
            exposed[node].update(row.get("result_ids") or [])
        elif row["tool"] == "open_source_span":
            paragraph_id = (row.get("arguments") or {}).get("paragraph_id")
            open_checks.append(
                {"node": node, "paragraph_id": paragraph_id, "previously_returned": paragraph_id in exposed[node]}
            )

    text = "\n".join(
        (run_dir / name).read_text(encoding="utf-8")
        for name in ("run.json", "model_calls.jsonl", "tool_calls.jsonl", "events.jsonl")
    ) + "\n" + (audit_dir / "wire.jsonl").read_text(encoding="utf-8")
    execution = run.get("execution") or {}
    return {
        "run_id": run["run_id"],
        "case_id": run["case_id"],
        "status": (execution.get("status") or "completed") if not run.get("failure", {}).get("failure_type") else "failed",
        "manager_output_present": bool(execution.get("manager_output")),
        "compliance_output_present": bool(execution.get("compliance_output")),
        "failure": run.get("failure"),
        "model_calls_by_role": dict(per_role_models),
        "tool_calls_by_role": dict(per_role_tools),
        "tool_names_by_role": {key: dict(value) for key, value in tool_names.items()},
        "all_tool_calls_ok": all(row.get("ok") is True and not row.get("error") for row in tools),
        "tool_error_count": sum(bool(row.get("error")) or row.get("ok") is not True for row in tools),
        "model_or_parse_error_count": sum(bool(row.get("error")) or bool(row.get("parse_error")) for row in models),
        "max_native_tool_calls_in_one_response": max_native_calls,
        "parallel_tool_calls_values": sorted(set(parallel_flags), key=str),
        "final_requests": final_requests,
        "invalid_check_only_present": "invalid_check_only" in text,
        "cursor_error_present": "cursor unknown or expired" in text.lower(),
        "open_calls": len(open_checks),
        "all_open_ids_previously_returned": all(row["previously_returned"] for row in open_checks),
        "open_checks": open_checks,
    }


def telemetry_summary(path: Path) -> dict:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    def nums(name: str) -> list[float]:
        return [float(row[name].strip()) for row in rows]
    thermal = [row["clocks_event_reasons.sw_thermal_slowdown"].strip() for row in rows]
    return {
        "samples": len(rows),
        "max_power_w": max(nums("power.draw")),
        "max_temperature_c": max(nums("temperature.gpu")),
        "max_memory_mib": max(nums("memory.used")),
        "thermal_slowdown_seen": any(value != "Not Active" for value in thermal),
        "max_uncorrected_ecc": max(nums("ecc.errors.uncorrected.volatile.total")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    raw_root = args.root / "tasks" / "raw"
    results = []
    for run_dir in sorted(raw_root.glob("*/*")):
        experiment_id = run_dir.parent.name
        results.append(summarize_run(run_dir, args.root / "audits" / experiment_id))
    output = {"runs": results, "telemetry_gpu2": telemetry_summary(args.root / "telemetry" / "gpu2.csv")}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
