"""Summarize completed cells and apply the preregistered selection rules."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


NUMERIC_FIELDS = (
    "aggregate_output_tokens_per_second", "joules_per_output_token", "successful_requests_per_hour",
    "ttft_seconds_p50", "ttft_seconds_p95", "latency_seconds_p50", "latency_seconds_p95",
    "power_w_mean", "power_w_peak", "temperature_c_mean", "temperature_c_peak",
    "gpu_utilization_pct_mean", "memory_used_mib_peak", "completion_rate",
)


def median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return statistics.median(values) if values else None


def aggregate(rows: list[dict[str, Any]], group_key: str) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(row[group_key])].append(row)
    output = []
    for value, items in sorted(groups.items()):
        result: dict[str, Any] = {group_key: value, "cells": len(items),
                                  "all_passed": all(item.get("passed") for item in items),
                                  "all_complete": all(item.get("completion_rate") == 1 for item in items)}
        result.update({field: median(items, field) for field in NUMERIC_FIELDS})
        output.append(result)
    return output


def choose_power(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [row for row in groups if row["all_passed"] and row["all_complete"] and row["cells"] >= 3]
    if not eligible:
        return None
    best = max(row["aggregate_output_tokens_per_second"] for row in eligible)
    near = [row for row in eligible if row["aggregate_output_tokens_per_second"] >= best * 0.97]
    lowest_power = min(row["power_w"] for row in near)
    candidates = [row for row in near if row["power_w"] == lowest_power]
    return min(candidates, key=lambda row: row["joules_per_output_token"] if row["joules_per_output_token"] is not None else float("inf"))


def choose_concurrency(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [row for row in groups if row["all_passed"] and row["all_complete"] and row["cells"] >= 3]
    if not eligible:
        return None
    eligible.sort(key=lambda row: row["concurrency"])
    chosen = eligible[0]
    for current in eligible[1:]:
        gain = current["aggregate_output_tokens_per_second"] / chosen["aggregate_output_tokens_per_second"] - 1
        p95_ratio = (current["latency_seconds_p95"] / chosen["latency_seconds_p95"]
                     if current.get("latency_seconds_p95") and chosen.get("latency_seconds_p95") else 1)
        if gain < 0.10 and p95_ratio > 1.25:
            break
        if current["aggregate_output_tokens_per_second"] >= chosen["aggregate_output_tokens_per_second"]:
            chosen = current
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("outputs/native-performance-sweep-20261101"))
    args = parser.parse_args()
    summaries = []
    for path in args.root.rglob("summary.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("schema_version") != "native-sweep-cell-v1":
            continue
        row["cell_path"] = str(path.parent.relative_to(args.root))
        summaries.append(row)
    if not summaries:
        raise SystemExit(f"no native sweep cell summaries found under {args.root}")

    fieldnames = ["cell_path", "phase", "power_w", "concurrency", "repeat", "request_count",
                  "successful_requests", "failed_requests", "completion_rate", "batch_wall_seconds",
                  "completion_tokens", *NUMERIC_FIELDS[:-1], "passed"]
    with (args.root / "measurements.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(summaries, key=lambda row: (row["phase"], row["power_w"], row["concurrency"], row["repeat"])))

    power_groups = aggregate([row for row in summaries if row["phase"] == "power"], "power_w")
    concurrency_groups = aggregate([row for row in summaries if row["phase"] == "concurrency"], "concurrency")
    interaction_groups = aggregate([row for row in summaries if row["phase"] == "interaction"], "power_w")
    power_choice = choose_power(power_groups)
    concurrency_choice = choose_concurrency(concurrency_groups)
    interaction_choice = choose_power(interaction_groups)
    result = {
        "schema_version": "native-sweep-summary-v1", "cells": len(summaries),
        "power": power_groups, "concurrency": concurrency_groups, "interaction": interaction_groups,
        "stage_b_power_choice": None if power_choice is None else power_choice["power_w"],
        "stage_c_concurrency_choice": None if concurrency_choice is None else concurrency_choice["concurrency"],
        "final_power_choice": None if interaction_choice is None else interaction_choice["power_w"],
        "selection_ready": power_choice is not None and concurrency_choice is not None and interaction_choice is not None,
        "notes": ["Choices require three complete passing cells per tested level.",
                  "Energy is integrated from one-second telemetry samples and is therefore approximate at cell boundaries.",
                  "These are engineering probes, not new research results."],
    }
    (args.root / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# Native performance sweep report", "", "Scope: engineering deployment selection; not a research rerun.", ""]
    for title, groups, key in (("Power stage", power_groups, "power_w"),
                               ("Concurrency stage", concurrency_groups, "concurrency"),
                               ("Interaction stage", interaction_groups, "power_w")):
        lines += [f"## {title}", "", f"| {key} | cells | pass | tok/s | J/token | P95 latency (s) |",
                  "|---:|---:|:---:|---:|---:|---:|"]
        for row in groups:
            lines.append(f"| {row[key]} | {row['cells']} | {'yes' if row['all_passed'] else 'no'} | "
                         f"{row['aggregate_output_tokens_per_second'] or 0:.3f} | "
                         f"{row['joules_per_output_token'] or 0:.4f} | {row['latency_seconds_p95'] or 0:.3f} |")
        lines.append("")
    lines += ["## Selection", "",
              f"- Stage B power: {result['stage_b_power_choice']}",
              f"- Stage C concurrency: {result['stage_c_concurrency_choice']}",
              f"- Interaction-confirmed power: {result['final_power_choice']}",
              f"- Ready to freeze: {result['selection_ready']}", "",
              "Raw evidence remains in each cell directory; failed cells are retained and are never silently retried.", ""]
    (args.root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

