"""Run one fail-closed native performance-sweep cell without retries."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest(workload: Path) -> None:
    for line in (workload / "manifest.sha256").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        actual = sha256_file(workload / name)
        if actual != digest:
            raise SystemExit(f"workload hash mismatch for {name}: {actual} != {digest}")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def merge_delta(message: dict[str, Any], delta: dict[str, Any]) -> None:
    for key in ("content", "reasoning_content"):
        if delta.get(key):
            message[key] = message.get(key, "") + delta[key]
    if delta.get("tool_calls"):
        calls = message.setdefault("tool_calls", [])
        for fragment in delta["tool_calls"]:
            index = int(fragment.get("index", len(calls)))
            while len(calls) <= index:
                calls.append({"function": {"name": "", "arguments": ""}})
            target = calls[index]
            for key in ("id", "type"):
                if fragment.get(key):
                    target[key] = fragment[key]
            fn = fragment.get("function") or {}
            for key in ("name", "arguments"):
                if fn.get(key):
                    target.setdefault("function", {}).setdefault(key, "")
                    target["function"][key] += fn[key]


def perform_request(base_url: str, record: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = record["payload"]
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer local-benchmark"},
                                     method="POST")
    first_token: float | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    response_id: str | None = None
    model: str | None = None
    message: dict[str, Any] = {}
    event_count = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            event_count += 1
            response_id = event.get("id", response_id)
            model = event.get("model", model)
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if first_token is None and any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                    first_token = time.monotonic()
                merge_delta(message, delta)
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
    ended = time.monotonic()
    if usage is None:
        raise RuntimeError("stream completed without usage")
    return {
        "request_id": record["request_id"], "sequence": record["sequence"], "ok": True,
        "started_at": started_wall, "ended_at": datetime.now(timezone.utc).isoformat(),
        "latency_seconds": ended - started,
        "ttft_seconds": None if first_token is None else first_token - started,
        "usage": usage, "finish_reason": finish_reason, "response_id": response_id,
        "model": model, "message": message, "stream_events": event_count,
    }


async def execute(records: list[dict[str, Any]], base_url: str, concurrency: int,
                  timeout: float, stop_on_error: bool,
                  external_abort: asyncio.Event | None = None) -> tuple[list[dict[str, Any]], float]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for record in records:
        queue.put_nowait(record)
    results: list[dict[str, Any]] = []
    abort = external_abort or asyncio.Event()

    async def worker() -> None:
        while not queue.empty() and not abort.is_set():
            try:
                record = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                result = await asyncio.to_thread(perform_request, base_url, record, timeout)
            except Exception as exc:  # each error is evidence, never retried
                result = {"request_id": record["request_id"], "sequence": record["sequence"],
                          "ok": False, "error_type": type(exc).__name__, "error": str(exc),
                          "ended_at": datetime.now(timezone.utc).isoformat()}
                if stop_on_error:
                    abort.set()
            results.append(result)
            queue.task_done()

    start = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return sorted(results, key=lambda row: row["sequence"]), time.monotonic() - start


def telemetry_summary(path: Path) -> dict[str, Any]:
    rows = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
    def values(key: str) -> list[float]:
        output = []
        for row in rows:
            try:
                output.append(float(row[key].strip()))
            except (KeyError, TypeError, ValueError):
                pass
        return output
    powers, temps = values("power.draw"), values("temperature.gpu")
    thermal = [str(row.get("clocks_event_reasons.sw_thermal_slowdown", "")).strip().lower() for row in rows]
    ecc = values("ecc.errors.uncorrected.volatile.total")
    return {
        "samples": len(rows), "energy_joules": sum(powers),
        "power_w_mean": sum(powers) / len(powers) if powers else None,
        "power_w_peak": max(powers) if powers else None,
        "temperature_c_mean": sum(temps) / len(temps) if temps else None,
        "temperature_c_peak": max(temps) if temps else None,
        "gpu_utilization_pct_mean": (lambda v: sum(v) / len(v) if v else None)(values("utilization.gpu")),
        "memory_used_mib_peak": (lambda v: max(v) if v else None)(values("memory.used")),
        "thermal_slowdown_observed": any(value in {"active", "yes", "true"} for value in thermal),
        "uncorrectable_ecc_observed": any(value > 0 for value in ecc),
    }


def fetch_metrics(base_url: str) -> dict[str, float]:
    with urllib.request.urlopen(base_url.rstrip("/") + "/metrics", timeout=5) as response:
        text = response.read().decode("utf-8", errors="replace")
    selected: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not any(term in line for term in ("num_running_reqs", "num_queue_reqs", "token_usage", "cache")):
            continue
        match = re.match(r"^([^ ]+)\s+([-+0-9.eE]+)$", line)
        if match:
            selected[match.group(1)] = float(match.group(2))
    return selected


async def monitor_service(base_url: str, path: Path, stop: asyncio.Event) -> None:
    with path.open("w", encoding="utf-8") as handle:
        while not stop.is_set():
            row: dict[str, Any] = {"time": datetime.now(timezone.utc).isoformat()}
            try:
                row["metrics"] = await asyncio.to_thread(fetch_metrics, base_url)
                row["ok"] = True
            except Exception as exc:
                row.update({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass


async def watch_gpu_safety(path: Path, monitor: subprocess.Popen[Any], abort: asyncio.Event,
                           stop: asyncio.Event, evidence: Path) -> None:
    reason: dict[str, Any] | None = None
    while not stop.is_set():
        if monitor.poll() is not None:
            reason = {"reason": "gpu_monitor_exited", "returncode": monitor.returncode}
        elif path.exists():
            rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(rows) > 1:
                try:
                    latest = next(csv.DictReader([rows[0], rows[-1]]))
                    thermal = str(latest.get("clocks_event_reasons.sw_thermal_slowdown", "")).strip().lower()
                    ecc = str(latest.get("ecc.errors.uncorrected.volatile.total", "")).strip()
                    if thermal in {"active", "yes", "true"}:
                        reason = {"reason": "thermal_slowdown", "sample": latest}
                    elif ecc and ecc not in {"0", "0.0"}:
                        reason = {"reason": "uncorrectable_ecc", "sample": latest}
                except (StopIteration, csv.Error):
                    pass
        if reason:
            evidence.write_text(json.dumps({**reason, "detected_at": datetime.now(timezone.utc).isoformat()},
                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            abort.set()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


def service_metrics_summary(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    running: list[float] = []
    queued: list[float] = []
    for row in rows:
        for key, value in (row.get("metrics") or {}).items():
            if "num_running_reqs" in key:
                running.append(float(value))
            if "num_queue_reqs" in key:
                queued.append(float(value))
    return {"service_metric_samples": sum(bool(row.get("ok")) for row in rows),
            "service_metric_errors": sum(not row.get("ok") for row in rows),
            "max_observed_running_requests": max(running) if running else None,
            "max_observed_queued_requests": max(queued) if queued else None}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


async def async_main(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=False)
    verify_manifest(args.workload)
    records = [json.loads(line) for line in (args.workload / "requests.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if args.limit:
        records = records[:args.limit]
    if not records:
        raise SystemExit("workload is empty")
    cell = {"schema_version": "native-sweep-cell-v1", "phase": args.phase, "power_w": args.power,
            "concurrency": args.concurrency, "repeat": args.repeat, "container": args.container,
            "base_url": args.base_url, "request_count": len(records),
            "workload_manifest_sha256": sha256_file(args.workload / "manifest.json")}
    (args.output_dir / "cell-config.json").write_text(json.dumps(cell, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_cmd = [args.python, "deploy/native/benchmark/verify_variant.py", "--container", args.container,
                  "--power", str(args.power), "--concurrency", str(args.concurrency),
                  "--base-url", args.base_url, "--output-dir", str(args.output_dir)]
    subprocess.run(verify_cmd, check=True)
    with (args.output_dir / "gpu-ecc-before.txt").open("w", encoding="utf-8") as handle:
        subprocess.run(["nvidia-smi", "-q", "-i", "2", "-d", "ECC"], stdout=handle, check=True)

    warmup = records[: min(args.warmup, len(records))]
    if warmup:
        warm, _ = await execute(warmup, args.base_url, min(args.concurrency, len(warmup)), args.timeout, True)
        write_jsonl(args.output_dir / "warmup-responses.jsonl", warm)
        if not all(row["ok"] for row in warm):
            raise SystemExit("warmup failed; measured replay not started")

    telemetry = args.output_dir / "gpu-telemetry.csv"
    monitor = subprocess.Popen(["bash", "deploy/native/benchmark/monitor_gpu.sh", "--gpu", "2", "--output", str(telemetry)],
                               stdout=subprocess.DEVNULL, stderr=(args.output_dir / "telemetry-stderr.log").open("w"))
    service_stop = asyncio.Event()
    service_path = args.output_dir / "service-metrics.jsonl"
    service_task = asyncio.create_task(monitor_service(args.base_url, service_path, service_stop))
    safety_abort = asyncio.Event()
    safety_stop = asyncio.Event()
    safety_task = asyncio.create_task(watch_gpu_safety(telemetry, monitor, safety_abort, safety_stop,
                                                        args.output_dir / "safety-stop.json"))
    await asyncio.sleep(1.2)
    try:
        results, batch_wall = await execute(records, args.base_url, args.concurrency, args.timeout, True, safety_abort)
    finally:
        monitor_exited_early = monitor.poll() is not None
        safety_stop.set()
        await safety_task
        service_stop.set()
        await service_task
        if monitor.poll() is None:
            monitor.send_signal(signal.SIGTERM)
        try:
            monitor.wait(timeout=10)
        except subprocess.TimeoutExpired:
            monitor.kill()
    write_jsonl(args.output_dir / "requests.jsonl", records)
    write_jsonl(args.output_dir / "responses.jsonl", [row for row in results if row.get("ok")])
    errors = [row for row in results if not row.get("ok")]
    write_jsonl(args.output_dir / "errors.jsonl", errors)
    subprocess.run(["docker", "logs", args.container], stdout=(args.output_dir / "stdout.log").open("w", encoding="utf-8"),
                   stderr=(args.output_dir / "stderr.log").open("w", encoding="utf-8"), check=False)
    with (args.output_dir / "gpu-ecc-after.txt").open("w", encoding="utf-8") as handle:
        subprocess.run(["nvidia-smi", "-q", "-i", "2", "-d", "ECC"], stdout=handle, check=True)

    good = [row for row in results if row.get("ok")]
    completion_tokens = sum(int(row["usage"].get("completion_tokens") or 0) for row in good)
    prompt_tokens = sum(int(row["usage"].get("prompt_tokens") or 0) for row in good)
    reasoning_tokens = sum(int(((row["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens")) or
                               row["usage"].get("reasoning_tokens") or 0) for row in good)
    telemetry_metrics = telemetry_summary(telemetry)
    service_metrics = service_metrics_summary(service_path)
    latencies = [row["latency_seconds"] for row in good]
    ttfts = [row["ttft_seconds"] for row in good if row.get("ttft_seconds") is not None]
    energy = telemetry_metrics["energy_joules"]
    summary = {
        **cell, "submitted_requests": len(results), "successful_requests": len(good), "failed_requests": len(errors),
        "unsubmitted_after_stop": len(records) - len(results), "completion_rate": len(good) / len(records),
        "batch_wall_seconds": batch_wall, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "aggregate_output_tokens_per_second": completion_tokens / batch_wall if batch_wall else None,
        "successful_requests_per_hour": len(good) * 3600 / batch_wall if batch_wall else None,
        "latency_seconds_p50": percentile(latencies, 0.5), "latency_seconds_p95": percentile(latencies, 0.95),
        "ttft_seconds_p50": percentile(ttfts, 0.5), "ttft_seconds_p95": percentile(ttfts, 0.95),
        "finish_reasons": {reason: sum(str(row.get("finish_reason")) == reason for row in good)
                           for reason in sorted({str(row.get("finish_reason")) for row in good})},
        **telemetry_metrics,
        **service_metrics,
        "gpu_monitor_exited_early": monitor_exited_early,
        "safety_abort_triggered": safety_abort.is_set(),
        "joules_per_output_token": energy / completion_tokens if energy is not None and completion_tokens else None,
        "tokens_per_kwh": completion_tokens / (energy / 3_600_000) if energy else None,
        "passed": (len(good) == len(records) and not errors and not monitor_exited_early
                   and not telemetry_metrics["thermal_slowdown_observed"]
                   and not telemetry_metrics["uncorrectable_ecc_observed"]
                   and not safety_abort.is_set()
                   and service_metrics["max_observed_running_requests"] is not None
                   and service_metrics["max_observed_running_requests"] >= min(args.concurrency, len(records))),
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    if not summary["passed"]:
        raise SystemExit("cell failed; retained evidence and did not retry")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, default=Path("benchmarks/native-sweep-v1"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", required=True, choices=("smoke", "power", "concurrency-safety", "concurrency", "interaction"))
    parser.add_argument("--power", type=int, required=True, choices=(200, 230, 240, 250, 260))
    parser.add_argument("--concurrency", type=int, required=True, choices=(2, 4, 8, 16))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--container", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:30002")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--python", default=os.environ.get("PYTHON", ".venv/bin/python"))
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()

