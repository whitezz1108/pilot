from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/generate_gpu_monitor_report.py"
    spec = importlib.util.spec_from_file_location("gpu_report", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_outlier_is_retained_and_flagged(tmp_path: Path) -> None:
    csv_path = tmp_path / "gpu.csv"
    csv_path.write_text(
        "timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total\n"
        "2026/11/01 00:00:00.000,199,200,70,98,50,67000,1000,1500,Not Active,0\n"
        "2026/11/01 00:00:01.000,349,200,71,98,50,67000,1000,1500,Not Active,0\n"
        "2026/11/01 00:00:02.000,200,200,72,98,50,67000,1000,1500,Not Active,0\n",
        encoding="utf-8",
    )
    data, summary = module().load(csv_path)
    assert len(data) == 3
    assert summary["power"]["max"] == 349
    assert summary["power_outliers"] == 1
    assert summary["thermal_slowdown_samples"] == 0
    assert summary["uncorrectable_ecc_peak"] == 0


def test_svg_contains_axes_and_outlier_marker(tmp_path: Path) -> None:
    csv_path = tmp_path / "gpu.csv"
    csv_path.write_text(
        "timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total\n"
        "2026/11/01 00:00:00,199,200,70,98,50,67000,1000,1500,Not Active,0\n"
        "2026/11/01 00:00:01,349,200,71,98,50,67000,1000,1500,Not Active,0\n",
        encoding="utf-8",
    )
    data, _ = module().load(csv_path)
    svg = module().svg_chart(data, [("power", "raw", "raw"), ("power_limit", "limit", "limit")], "Power", "W", True)
    assert "elapsed time (min)" in svg
    assert "349.00 W" in svg
    assert "stroke-dasharray" in svg

