"""Generate a dependency-free local GPU telemetry report, with optional PNG."""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


TIMESTAMP_FORMATS = ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
FIELDS = {
    "power": "power.draw", "power_limit": "power.limit", "temperature": "temperature.gpu",
    "gpu_util": "utilization.gpu", "memory_util": "utilization.memory", "memory": "memory.used",
    "sm_clock": "clocks.sm", "memory_clock": "clocks.mem",
    "thermal": "clocks_event_reasons.sw_thermal_slowdown",
    "ecc": "ecc.errors.uncorrected.volatile.total",
}


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"unsupported timestamp: {value!r}")


def number(value: Any) -> float | None:
    try:
        text = str(value).strip()
        return None if not text or text.lower() in {"n/a", "nan", "[not supported]"} else float(text)
    except (TypeError, ValueError):
        return None


def rolling_median(values: list[float | None], width: int = 5) -> list[float | None]:
    half = width // 2
    output = []
    for index in range(len(values)):
        window = [value for value in values[max(0, index-half): min(len(values), index+half+1)] if value is not None]
        output.append(statistics.median(window) if window else None)
    return output


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] if low == high else ordered[low] * (high-position) + ordered[high] * (position-low)


def robust_outliers(values: list[float | None], limits: list[float | None]) -> list[bool]:
    present = [value for value in values if value is not None]
    if not present:
        return [False] * len(values)
    center = statistics.median(present)
    deviations = [abs(value-center) for value in present]
    mad = statistics.median(deviations)
    output = []
    for value, limit in zip(values, limits):
        robust = value is not None and mad > 0 and abs(value-center) / (1.4826*mad) > 8
        cap = value is not None and limit is not None and value > limit * 1.10
        output.append(bool(robust or cap))
    return output


def load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not raw or "timestamp" not in (raw[0] if raw else {}):
        raise ValueError("telemetry CSV is empty or lacks timestamp")
    parsed = []
    missing = {column: 0 for column in FIELDS.values()}
    for row in raw:
        timestamp = parse_timestamp(row["timestamp"])
        item: dict[str, Any] = {"timestamp": timestamp}
        for key, column in FIELDS.items():
            if key == "thermal":
                item[key] = str(row.get(column, "")).strip()
            else:
                item[key] = number(row.get(column))
                if item[key] is None:
                    missing[column] += 1
        parsed.append(item)
    parsed.sort(key=lambda row: row["timestamp"])
    start = parsed[0]["timestamp"]
    for item in parsed:
        item["elapsed"] = (item["timestamp"] - start).total_seconds()
    intervals = [(b["timestamp"]-a["timestamp"]).total_seconds() for a, b in zip(parsed, parsed[1:])]
    powers = [row["power"] for row in parsed]
    limits = [row["power_limit"] for row in parsed]
    medians = rolling_median(powers)
    flags = robust_outliers(powers, limits)
    for row, median, flagged in zip(parsed, medians, flags):
        row["power_median"] = median
        row["power_outlier"] = flagged
    summary = {
        "source": str(path.resolve()), "samples": len(parsed),
        "started_at": parsed[0]["timestamp"].isoformat(), "ended_at": parsed[-1]["timestamp"].isoformat(),
        "duration_seconds": parsed[-1]["elapsed"], "interval_seconds_p50": percentile(intervals, .5),
        "interval_seconds_p95": percentile(intervals, .95), "gaps_over_2_seconds": sum(value > 2 for value in intervals),
        "missing_values": missing, "power_outliers": sum(flags),
    }
    for key in ("power", "power_limit", "temperature", "gpu_util", "memory_util", "memory", "sm_clock", "memory_clock"):
        values = [row[key] for row in parsed if row[key] is not None]
        summary[key] = {"mean": statistics.mean(values), "median": statistics.median(values),
                        "p95": percentile(values, .95), "max": max(values), "min": min(values)} if values else None
    summary["thermal_slowdown_samples"] = sum(row["thermal"].lower() in {"active", "yes", "true"} for row in parsed)
    summary["uncorrectable_ecc_peak"] = max((row["ecc"] or 0) for row in parsed)
    energy = 0.0
    for left, right in zip(parsed, parsed[1:]):
        if left["power"] is not None and right["power"] is not None:
            energy += (left["power"] + right["power"]) / 2 * (right["elapsed"] - left["elapsed"])
    summary["raw_energy_joules_trapezoid"] = energy
    return parsed, summary


def svg_chart(data: list[dict[str, Any]], series: list[tuple[str, str, str]], title: str, unit: str,
              outliers: bool = False) -> str:
    width, height = 1100, 280
    left, right, top, bottom = 72, 24, 30, 46
    values = [row[key] for row in data for key, _, _ in series if row.get(key) is not None]
    x_max = max(row["elapsed"] for row in data) or 1
    y_min, y_max = min(values), max(values)
    pad = (y_max-y_min)*0.08 or max(abs(y_max)*.08, 1)
    y_min, y_max = y_min-pad, y_max+pad
    def xy(x: float, y: float) -> tuple[float, float]:
        return left + x/x_max*(width-left-right), top + (y_max-y)/(y_max-y_min)*(height-top-bottom)
    colors = {"raw": "#2563eb", "trend": "#f97316", "limit": "#64748b", "secondary": "#7c3aed"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
             '<style>text{font-family:system-ui,sans-serif;font-size:12px;fill:#475569}.title{font-size:15px;font-weight:600;fill:#0f172a}.grid{stroke:#e2e8f0;stroke-width:1}.axis{stroke:#94a3b8}.out{fill:#dc2626;stroke:#fff;stroke-width:1.5}</style>',
             f'<text class="title" x="{left}" y="18">{html.escape(title)}</text>']
    for index in range(5):
        fraction = index/4
        y = top + fraction*(height-top-bottom)
        value = y_max - fraction*(y_max-y_min)
        parts += [f'<line class="grid" x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}"/>',
                  f'<text x="{left-8}" y="{y+4:.2f}" text-anchor="end">{value:.1f}</text>']
    for index in range(6):
        fraction = index/5
        x = left + fraction*(width-left-right)
        parts += [f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}"/>',
                  f'<text x="{x:.2f}" y="{height-bottom+20}" text-anchor="middle">{x_max*fraction/60:.1f}</text>']
    parts += [f'<text x="{(left+width-right)/2}" y="{height-8}" text-anchor="middle">elapsed time (min)</text>',
              f'<text transform="translate(16 {(top+height-bottom)/2}) rotate(-90)" text-anchor="middle">{html.escape(unit)}</text>']
    for key, label, kind in series:
        points = [xy(row["elapsed"], row[key]) for row in data if row.get(key) is not None]
        if not points:
            continue
        dash = ' stroke-dasharray="7 5"' if kind == "limit" else ""
        parts.append(f'<polyline fill="none" stroke="{colors[kind]}" stroke-width="{2.4 if kind == "trend" else 1.4}"{dash} points="' + " ".join(f"{x:.2f},{y:.2f}" for x, y in points) + f'"><title>{html.escape(label)}</title></polyline>')
    if outliers:
        for row in data:
            if row.get("power_outlier") and row.get("power") is not None:
                x, y = xy(row["elapsed"], row["power"])
                parts.append(f'<circle class="out" cx="{x:.2f}" cy="{y:.2f}" r="4"><title>{row["timestamp"].isoformat()} | {row["power"]:.2f} W</title></circle>')
    legend_x = width-right-320
    for index, (_, label, kind) in enumerate(series):
        x = legend_x + index*105
        parts += [f'<line x1="{x}" y1="17" x2="{x+20}" y2="17" stroke="{colors[kind]}" stroke-width="2"/>',
                  f'<text x="{x+25}" y="21">{html.escape(label)}</text>']
    parts.append("</svg>")
    return "".join(parts)


def write_png(data: list[dict[str, Any]], path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    x = [row["elapsed"]/60 for row in data]
    fig, axes = plt.subplots(5, 1, figsize=(14, 14), sharex=True, constrained_layout=True)
    panels = [
        (("power", "raw"), ("power_median", "5-point median"), ("power_limit", "limit"), "Power (W)"),
        (("temperature", "temperature"), None, None, "Temperature (C)"),
        (("memory", "memory used"), None, None, "Memory (MiB)"),
        (("gpu_util", "GPU"), ("memory_util", "memory"), None, "Utilization (%)"),
        (("sm_clock", "SM"), ("memory_clock", "memory"), None, "Clock (MHz)"),
    ]
    for axis, panel in zip(axes, panels):
        for entry in panel[:3]:
            if entry:
                key, label = entry
                axis.plot(x, [row.get(key) for row in data], linewidth=1.2, label=label)
        axis.set_ylabel(panel[3]); axis.grid(alpha=.22); axis.legend(loc="upper right")
    axes[-1].set_xlabel("Elapsed time (min)")
    outlier_x = [row["elapsed"]/60 for row in data if row.get("power_outlier")]
    outlier_y = [row["power"] for row in data if row.get("power_outlier")]
    axes[0].scatter(outlier_x, outlier_y, color="#dc2626", s=25, label="flagged outlier", zorder=5)
    fig.suptitle("GPU experiment telemetry")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="nvidia-smi telemetry CSV")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--png", action="store_true", help="also render PNG when matplotlib is installed")
    parser.add_argument("--open", action="store_true", help="open the generated HTML in the default browser")
    args = parser.parse_args()
    data, summary = load(args.input)
    args.output.mkdir(parents=True, exist_ok=True)
    charts = {
        "power.svg": svg_chart(data, [("power", "raw", "raw"), ("power_median", "median", "trend"), ("power_limit", "limit", "limit")], "Power draw and cap", "W", True),
        "temperature.svg": svg_chart(data, [("temperature", "temperature", "raw")], "GPU temperature", "C"),
        "memory.svg": svg_chart(data, [("memory", "memory used", "raw")], "GPU memory", "MiB"),
        "utilization.svg": svg_chart(data, [("gpu_util", "GPU", "raw"), ("memory_util", "memory", "secondary")], "Utilization", "%"),
        "clocks.svg": svg_chart(data, [("sm_clock", "SM", "raw"), ("memory_clock", "memory", "secondary")], "Clock frequencies", "MHz"),
    }
    for name, content in charts.items():
        (args.output / name).write_text(content, encoding="utf-8")
    png_written = args.png and write_png(data, args.output / "gpu-monitor.png")
    summary["png_written"] = png_written
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    power = summary["power"] or {}
    quality = "attention" if summary["power_outliers"] or summary["thermal_slowdown_samples"] or summary["uncorrectable_ecc_peak"] else "clean"
    report = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GPU实验监测报告</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f8fafc;color:#0f172a}}main{{max-width:1180px;margin:auto;padding:28px}}h1{{font-size:24px}}.meta{{color:#64748b}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:18px 0}}.card{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px}}.value{{font-size:24px;font-weight:600}}.warn{{color:#b45309}}.ok{{color:#15803d}}section{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin:14px 0}}img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border-bottom:1px solid #e2e8f0;text-align:left}}code{{word-break:break-all}}@media(max-width:600px){{main{{padding:14px}}}}
</style></head><body><main><h1>GPU实验监测报告</h1><p class="meta">{html.escape(summary['started_at'])} 至 {html.escape(summary['ended_at'])} · {summary['samples']} samples · {summary['duration_seconds']/60:.1f} min</p>
<div class="grid"><div class="card"><div>平均功率</div><div class="value">{power.get('mean',0):.2f} W</div></div><div class="card"><div>峰值温度</div><div class="value">{summary['temperature']['max']:.0f} C</div></div><div class="card"><div>峰值显存</div><div class="value">{summary['memory']['max']:.0f} MiB</div></div><div class="card"><div>数据质量</div><div class="value {'warn' if quality=='attention' else 'ok'}">{'需复核' if quality=='attention' else '正常'}</div></div></div>
<section><img src="power.svg" alt="功率原始值、滚动中位数、功率上限和异常点"></section><section><img src="temperature.svg" alt="GPU温度"></section><section><img src="memory.svg" alt="GPU显存"></section><section><img src="utilization.svg" alt="GPU和显存利用率"></section><section><img src="clocks.svg" alt="GPU时钟"></section>
<section><h2>采样质量与安全事件</h2><table><tr><th>项目</th><th>结果</th></tr><tr><td>采样间隔 P50 / P95</td><td>{summary['interval_seconds_p50']:.3f} / {summary['interval_seconds_p95']:.3f} s</td></tr><tr><td>超过2秒的间隔</td><td>{summary['gaps_over_2_seconds']}</td></tr><tr><td>功率异常点</td><td>{summary['power_outliers']}</td></tr><tr><td>热降频样本</td><td>{summary['thermal_slowdown_samples']}</td></tr><tr><td>不可纠正ECC峰值</td><td>{summary['uncorrectable_ecc_peak']:.0f}</td></tr><tr><td>原始梯形积分能量</td><td>{summary['raw_energy_joules_trapezoid']/1000:.2f} kJ</td></tr></table><p class="meta">异常点不会从统计或图中删除。滚动中位数只用于观察趋势，不能替代原始遥测。</p></section>
<section><h2>来源</h2><code>{html.escape(summary['source'])}</code></section></main></body></html>"""
    report_path = args.output / "gpu-monitor-report.html"
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"report": str(report_path.resolve()), "summary": str((args.output/'summary.json').resolve()),
                      "png": str((args.output/'gpu-monitor.png').resolve()) if png_written else None}, ensure_ascii=False))
    if args.open:
        webbrowser.open(report_path.resolve().as_uri())


if __name__ == "__main__":
    main()

