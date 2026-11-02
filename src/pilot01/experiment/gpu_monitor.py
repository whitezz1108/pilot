"""Read-only GPU and SGLang telemetry for the experiment console."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time


REMOTE_SCRIPT = r'''
import csv
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.request

SERVICES = [
    {"gpu_index": 0, "container": "pilot-native-gpu0-v1", "port": 30000},
    {"gpu_index": 1, "container": "pilot-native-gpu1-v1", "port": 30001},
    {"gpu_index": 2, "container": "pilot-native-seven-layer-v1", "port": 30002},
]

def run(args, timeout=8):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()

def get_text(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")

def get_json(url):
    return json.loads(get_text(url))

def number(value, integer=False):
    value = str(value).strip()
    if value in {"", "N/A", "[N/A]", "[Not Supported]"}:
        return None
    try:
        return int(float(value)) if integer else float(value)
    except ValueError:
        return None

def metric(text, name):
    values = []
    for line in text.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            try:
                values.append(float(line.rsplit(None, 1)[1]))
            except (ValueError, IndexError):
                pass
    return max(values) if values else None

gpu_fields = [
    "index", "uuid", "name", "persistence_mode", "temperature.gpu",
    "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
    "power.draw", "power.limit", "clocks.current.graphics", "clocks.current.memory",
    "clocks_event_reasons.sw_thermal_slowdown", "ecc.errors.uncorrected.volatile.total",
]
raw_gpus = run(["nvidia-smi", "--query-gpu=" + ",".join(gpu_fields),
                "--format=csv,noheader,nounits"])
gpus = []
for row in csv.reader(raw_gpus.splitlines()):
    row = [item.strip() for item in row]
    gpus.append({
        "index": number(row[0], True), "uuid": row[1], "name": row[2],
        "persistence_mode": row[3], "temperature_c": number(row[4]),
        "gpu_util_pct": number(row[5]), "memory_util_pct": number(row[6]),
        "memory_used_mib": number(row[7]), "memory_total_mib": number(row[8]),
        "power_draw_w": number(row[9]), "power_limit_w": number(row[10]),
        "graphics_clock_mhz": number(row[11]), "memory_clock_mhz": number(row[12]),
        "thermal_slowdown": row[13], "uncorrectable_ecc": number(row[14], True),
    })

processes = []
try:
    raw = run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
               "--format=csv,noheader,nounits"])
    for row in csv.reader(raw.splitlines()):
        if len(row) >= 4:
            processes.append({"gpu_uuid": row[0].strip(), "pid": number(row[1], True),
                              "process_name": row[2].strip(), "used_memory_mib": number(row[3])})
except Exception:
    pass

services = []
for spec in SERVICES:
    base = "http://127.0.0.1:%s" % spec["port"]
    item = dict(spec, endpoint=base, ok=False)
    try:
        info = get_json(base + "/get_server_info")
        models = get_json(base + "/v1/models")
        try:
            metrics = get_text(base + "/metrics")
        except Exception:
            metrics = ""
        inspected = json.loads(run(["docker", "inspect", spec["container"]]))[0]
        item.update({
            "ok": True,
            "model": (models.get("data") or [{}])[0].get("id"),
            "container_status": inspected.get("State", {}).get("Status"),
            "max_running_requests": info.get("max_running_requests"),
            "context_length": info.get("context_length"),
            "dtype": info.get("dtype"),
            "tool_call_parser": info.get("tool_call_parser"),
            "metrics_available": bool(metrics),
            "running_requests": metric(metrics, "sglang:num_running_reqs"),
            "queued_requests": metric(metrics, "sglang:num_queue_reqs"),
            "token_usage": metric(metrics, "sglang:token_usage"),
            "generation_throughput": metric(metrics, "sglang:gen_throughput"),
        })
    except Exception as exc:
        item["error"] = "%s: %s" % (type(exc).__name__, exc)
    services.append(item)

disk = shutil.disk_usage("/")
meminfo = {}
with open("/proc/meminfo", encoding="utf-8") as handle:
    for line in handle:
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])

print(json.dumps({
    "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "host": socket.gethostname(),
    "gpus": gpus,
    "processes": processes,
    "services": services,
    "system": {
        "load_average": list(os.getloadavg()),
        "memory_total_mib": round(meminfo.get("MemTotal", 0) / 1024, 1),
        "memory_available_mib": round(meminfo.get("MemAvailable", 0) / 1024, 1),
        "disk_total_gib": round(disk.total / 1024**3, 1),
        "disk_free_gib": round(disk.free / 1024**3, 1),
    },
}, ensure_ascii=False))
'''


class GpuMonitor:
    """Collect cached telemetry either locally on T640 or through an SSH alias."""

    def __init__(self, *, mode: str | None = None, ssh_host: str | None = None,
                 cache_seconds: float = 2.0, runner=subprocess.run):
        self.mode = mode or os.environ.get("PILOT01_GPU_MONITOR_MODE", "ssh")
        self.ssh_host = ssh_host or os.environ.get("PILOT01_GPU_SSH_HOST", "t640")
        if self.mode not in {"ssh", "local", "disabled"}:
            raise ValueError("PILOT01_GPU_MONITOR_MODE must be ssh, local, or disabled")
        self.cache_seconds = cache_seconds
        self.runner = runner
        self.lock = threading.Lock()
        self.cached: dict | None = None
        self.cached_at = 0.0

    def collect(self, force: bool = False) -> dict:
        with self.lock:
            if not force and self.cached and time.monotonic() - self.cached_at < self.cache_seconds:
                return self.cached
            started = time.monotonic()
            if self.mode == "disabled":
                result = {"ok": False, "mode": "disabled", "error": "GPU监控已禁用"}
            else:
                command = (["python3", "-"] if self.mode == "local" else
                           ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            self.ssh_host, "python3", "-"])
                try:
                    completed = self.runner(command, input=REMOTE_SCRIPT, capture_output=True,
                                            text=True, timeout=30, check=False)
                    if completed.returncode:
                        raise RuntimeError((completed.stderr or completed.stdout).strip())
                    payload = json.loads(completed.stdout)
                    result = {"ok": True, "mode": self.mode,
                              "target": self.ssh_host if self.mode == "ssh" else payload.get("host"),
                              "latency_ms": round((time.monotonic() - started) * 1000), **payload}
                except Exception as exc:
                    result = {"ok": False, "mode": self.mode,
                              "target": self.ssh_host if self.mode == "ssh" else "local",
                              "latency_ms": round((time.monotonic() - started) * 1000),
                              "error": f"{type(exc).__name__}: {exc}"}
            self.cached = result
            self.cached_at = time.monotonic()
            return result
