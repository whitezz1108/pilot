from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metric_alignment_uses_stable_identity() -> None:
    build = load_script("build_native_replay.py")
    wire = [
        {"model_call_id": "b", "payload": {}},
        {"model_call_id": "a", "payload": {}},
    ]
    metrics = [
        {"model_call_id": "a", "completion_tokens": 1},
        {"model_call_id": "b", "completion_tokens": 2},
    ]
    aligned = build.align_metrics(wire, metrics)
    assert [row["model_call_id"] for row in aligned] == ["b", "a"]


def test_metric_alignment_prefers_internal_model_call_id() -> None:
    build = load_script("build_native_replay.py")
    wire = [{"model_call_id": "internal", "payload": {}}]
    metrics = [{"model_call_id": "internal", "request_id": "provider"}]
    assert build.align_metrics(wire, metrics) == metrics


def test_power_choice_is_lowest_within_three_percent() -> None:
    summarize = load_script("summarize_native_sweep.py")
    groups = [
        {"power_w": 230, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 95.0, "joules_per_output_token": 2.2},
        {"power_w": 240, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 98.0, "joules_per_output_token": 2.0},
        {"power_w": 250, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 100.0, "joules_per_output_token": 2.1},
        {"power_w": 260, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 100.5, "joules_per_output_token": 2.3},
    ]
    assert summarize.choose_power(groups)["power_w"] == 240


def test_concurrency_rejects_small_gain_with_large_p95_cost() -> None:
    summarize = load_script("summarize_native_sweep.py")
    groups = [
        {"concurrency": 4, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 150.0, "latency_seconds_p95": 60.0},
        {"concurrency": 8, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 220.0, "latency_seconds_p95": 110.0},
        {"concurrency": 16, "cells": 3, "all_passed": True, "all_complete": True,
         "aggregate_output_tokens_per_second": 230.0, "latency_seconds_p95": 260.0},
    ]
    assert summarize.choose_concurrency(groups)["concurrency"] == 8


def test_config_contains_preregistered_levels() -> None:
    import yaml

    config = yaml.safe_load((ROOT / "config/benchmark/native_sweep_v1.yaml").read_text(encoding="utf-8"))
    assert config["design"]["power"]["powers_w"] == [230, 240, 250, 260]
    assert config["design"]["concurrency"]["levels"] == [2, 4, 8, 16]
    assert config["workload"]["request_count"] == 68
    assert "seed" in config["workload"]["required_payload_fields"]


def test_telemetry_detects_thermal_slowdown(tmp_path: Path) -> None:
    replay = load_script("run_native_replay.py")
    telemetry = tmp_path / "gpu.csv"
    telemetry.write_text(
        "timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total\n"
        "2026/11/01 00:00:00,240,240,70,99,40,70000,1200,1500,Not Active,0\n"
        "2026/11/01 00:00:01,241,240,71,99,40,70010,1200,1500,Active,0\n",
        encoding="utf-8",
    )
    summary = replay.telemetry_summary(telemetry)
    assert summary["energy_joules"] == 481
    assert summary["thermal_slowdown_observed"] is True


def test_builder_freezes_payload_and_hashes(tmp_path: Path) -> None:
    wire = tmp_path / "wire.jsonl"
    metrics = tmp_path / "metrics.json"
    output = tmp_path / "frozen"
    payload = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "probe"}],
               "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
               "max_tokens": 10, "seed": 7}
    wire.write_text(json.dumps({"model_call_id": "r1", "payload": payload}) + "\n", encoding="utf-8")
    metrics.write_text(json.dumps({"call_metrics": [{"model_call_id": "r1", "prompt_tokens": 3,
                                                       "completion_tokens": 2, "reasoning_tokens": 1}]}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_native_replay.py"), "--wire-jsonl", str(wire),
         "--metrics", str(metrics), "--output-dir", str(output), "--expected-requests", "1",
         "--expected-prompt-tokens", "3", "--expected-completion-tokens", "2",
         "--expected-reasoning-tokens", "1"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    frozen = json.loads((output / "requests.jsonl").read_text(encoding="utf-8"))
    assert frozen["payload"]["stream"] is True
    assert frozen["payload"]["seed"] == 7
    assert (output / "manifest.sha256").read_text(encoding="ascii").count("\n") == 3


