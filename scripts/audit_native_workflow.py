"""Run the normal experiment CLI while recording exact live HTTP payloads.

This wrapper changes no workflow behavior. It only appends the actual request
payload and active HTTP duration to a new evidence directory. The destination
must already exist and be empty of audit files.
"""
from __future__ import annotations

import argparse
import json
import runpy
import sys
import threading
import time
from pathlib import Path

import pilot01.model.execution as execution
import pilot01.model.openai_compat as adapter


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--allowed-base-url", action="append", default=[])
    parser.add_argument("--allowed-seed", action="append", type=int, default=[])
    parser.add_argument("--allow-missing-seed", action="store_true")
    known, remaining = parser.parse_known_args()
    known.audit_dir.mkdir(parents=True, exist_ok=True)
    wire_path = known.audit_dir / "wire.jsonl"
    active_path = known.audit_dir / "active-http.jsonl"
    if wire_path.exists() or active_path.exists():
        raise SystemExit("refusing to append to an existing workflow audit")

    original = adapter._urlopen_transport
    lock = threading.Lock()

    def audited(request, timeout):
        payload = json.loads(request.data)
        if timeout != 360:
            raise RuntimeError(f"unexpected timeout: {timeout}")
        if payload.get("model") != "Qwen3.8-27B" or payload.get("max_tokens") != 32768:
            raise RuntimeError("unexpected model or max_tokens")
        allowed_seeds = set(known.allowed_seed or (101, 303))
        seed_ok = payload.get("seed") in allowed_seeds or (known.allow_missing_seed and "seed" not in payload)
        if payload.get("reasoning_effort") != "xhigh" or not seed_ok:
            raise RuntimeError("unexpected reasoning effort or seed")
        allowed_urls = tuple(known.allowed_base_url or ("http://127.0.0.1:30002/",))
        if not request.full_url.startswith(allowed_urls):
            raise RuntimeError(f"unexpected endpoint: {request.full_url}")
        request.add_header("x-sglext-return-input-ids", "1")
        request.add_header("x-sglext-return-output-ids", "1")
        row = {"time": time.time(), "url": request.full_url, "timeout": timeout,
               "model_call_id": getattr(request, "pilot01_model_call_id", None), "payload": payload}
        with lock, wire_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        started = time.monotonic()
        try:
            return original(request, timeout)
        finally:
            active = {"time": time.time(), "model_call_id": getattr(request, "pilot01_model_call_id", None),
                      "seconds": time.monotonic() - started}
            with lock, active_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(active, ensure_ascii=False, separators=(",", ":")) + "\n")

    adapter._urlopen_transport = audited
    execution._urlopen_transport = audited
    sys.argv = [sys.argv[0], *remaining]
    runpy.run_module("pilot01.experiment", run_name="__main__")


if __name__ == "__main__":
    main()
