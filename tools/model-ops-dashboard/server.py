"""Read-only local dashboard for the remote Qwen/SGLang experiment service."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SETTINGS = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))

REMOTE_SCRIPT = r'''
import csv
import datetime
import json
import os
import shutil
import socket
import subprocess
import urllib.request

CONTAINER = "pilot-native-seven-layer-v1"
BASE = "http://127.0.0.1:30002"

def run(args, timeout=8):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()

def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.load(response)

def number(value, integer=False):
    value = value.strip()
    if value in {"", "N/A", "[N/A]"}:
        return None
    try:
        return int(float(value)) if integer else float(value)
    except ValueError:
        return value

gpu_fields = [
    "index", "uuid", "name", "persistence_mode", "temperature.gpu",
    "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
    "power.draw", "power.limit", "clocks.current.graphics", "clocks.current.memory"
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
        "graphics_clock_mhz": number(row[11]), "memory_clock_mhz": number(row[12])
    })

processes = []
try:
    raw_processes = run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                         "--format=csv,noheader,nounits"])
    for row in csv.reader(raw_processes.splitlines()):
        if len(row) >= 4:
            processes.append({"gpu_uuid": row[0].strip(), "pid": number(row[1], True),
                              "process_name": row[2].strip(), "used_memory_mib": number(row[3])})
except Exception:
    pass

container = None
try:
    inspected = json.loads(run(["docker", "inspect", CONTAINER]))[0]
    container = {
        "name": inspected["Name"].lstrip("/"),
        "status": inspected["State"]["Status"],
        "started_at": inspected["State"].get("StartedAt"),
        "image": inspected["Image"],
        "command": inspected["Config"].get("Cmd", []),
        "restart_policy": inspected["HostConfig"].get("RestartPolicy", {}).get("Name", ""),
        "ports": inspected.get("NetworkSettings", {}).get("Ports", {})
    }
except Exception as exc:
    container = {"name": CONTAINER, "status": "unavailable", "error": str(exc)}

service_error = None
server_info = None
models = None
try:
    server_info = get_json("/get_server_info")
    models = get_json("/v1/models")
except Exception as exc:
    service_error = f"{type(exc).__name__}: {exc}"

disk = shutil.disk_usage("/")
with open("/proc/meminfo", encoding="utf-8") as handle:
    meminfo = {}
    for line in handle:
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])

payload = {
    "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "host": socket.gethostname(),
    "service_ok": server_info is not None and models is not None,
    "service_error": service_error,
    "gpus": gpus,
    "processes": processes,
    "container": container,
    "server_info": server_info,
    "models": models,
    "system": {
        "load_average": list(os.getloadavg()),
        "memory_total_mib": round(meminfo.get("MemTotal", 0) / 1024, 1),
        "memory_available_mib": round(meminfo.get("MemAvailable", 0) / 1024, 1),
        "disk_total_gib": round(disk.total / 1024**3, 1),
        "disk_free_gib": round(disk.free / 1024**3, 1)
    }
}
print(json.dumps(payload, ensure_ascii=False))
'''


class Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def local_metadata(self) -> dict:
        manifest_path = (ROOT / SETTINGS["manifest_path"]).resolve()
        repo_path = (ROOT / SETTINGS["repository_path"]).resolve()
        manifest = None
        manifest_sha256 = None
        if manifest_path.is_file():
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
            manifest_sha256 = hashlib.sha256(raw).hexdigest()
        commit = None
        try:
            commit = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=3, check=True
            ).stdout.strip()
        except Exception:
            pass
        return {
            "expected": SETTINGS["expected"],
            "sampling_profiles": SETTINGS["sampling_profiles"],
            "refresh_seconds": SETTINGS["refresh_seconds"],
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "source_commit": commit,
            "ssh_host": SETTINGS["ssh_host"],
            "endpoint": SETTINGS["endpoint"]
        }

    def collect(self, force: bool = False) -> dict:
        with self._lock:
            if not force and self._cached and time.monotonic() - self._cached_at < 2:
                return self._cached
            started = time.monotonic()
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                     SETTINGS["ssh_host"], "python3", "-"],
                    input=REMOTE_SCRIPT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip())
                remote = json.loads(result.stdout)
                payload = {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000),
                           "remote": remote, "local": self.local_metadata()}
            except Exception as exc:
                payload = {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000),
                           "error": f"{type(exc).__name__}: {exc}", "local": self.local_metadata()}
            self._cached = payload
            self._cached_at = time.monotonic()
            return payload


COLLECTOR = Collector()


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelOpsDashboard/1.0"

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            query = urllib.parse.parse_qs(parsed.query)
            payload = COLLECTOR.collect(force=query.get("force") == ["1"])
            self.send_bytes(200 if payload["ok"] else 503,
                            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            "application/json; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self.send_bytes(200, b"ok\n", "text/plain; charset=utf-8")
            return
        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (STATIC / relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            self.send_bytes(404, b"not found\n", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), content_type)

    def log_message(self, format: str, *args) -> None:
        if args and str(args[0]).startswith("GET /api/status"):
            return
        super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local read-only model operations dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Model Operations Dashboard: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
