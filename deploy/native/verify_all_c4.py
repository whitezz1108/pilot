"""Send four simultaneous native-tool requests to each of the three GPU endpoints."""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import urllib.request
from pathlib import Path


ENDPOINTS = {
    "gpu0": "http://127.0.0.1:30000",
    "gpu1": "http://127.0.0.1:30001",
    "gpu2": "http://127.0.0.1:30002",
}
barrier = threading.Barrier(len(ENDPOINTS) * 4)


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def probe(gpu: str, base: str, value: int) -> dict:
    payload = {
        "model": "Qwen3.8-27B",
        "messages": [{
            "role": "user",
            "content": f"Call the probe tool exactly once with value {value}. Do not answer in text.",
        }],
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "max_tokens": 2048,
        "parallel_tool_calls": False,
        "tools": [{
            "type": "function",
            "function": {
                "name": "probe",
                "description": "Return the supplied integer value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }],
        "tool_choice": "required",
    }
    barrier.wait(timeout=30)
    started = time.time()
    request = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=360) as response:
        result = json.load(response)
    ended = time.time()
    choice = result["choices"][0]
    calls = choice.get("message", {}).get("tool_calls") or []
    if choice.get("finish_reason") != "tool_calls" or len(calls) != 1:
        raise RuntimeError(f"{gpu}/{value}: expected exactly one native tool call")
    function = calls[0].get("function", {})
    arguments = json.loads(function.get("arguments", "{}"))
    if function.get("name") != "probe" or arguments != {"value": value}:
        raise RuntimeError(f"{gpu}/{value}: unexpected tool call {function!r}")
    return {
        "gpu": gpu,
        "value": value,
        "started": started,
        "ended": ended,
        "seconds": ended - started,
        "finish_reason": choice["finish_reason"],
        "tool_name": function["name"],
        "arguments": arguments,
    }


def main() -> None:
    evidence = Path("/home/evan/ai/benchmarks/latest_native_all_c4.txt").read_text().strip()
    output_dir = Path(evidence)
    server_values = {}
    for gpu, base in ENDPOINTS.items():
        info = get(base + "/get_server_info")
        server_values[gpu] = {
            "max_running_requests": info.get("max_running_requests"),
            "tool_call_parser": info.get("tool_call_parser"),
        }
        if info.get("max_running_requests") != 4 or info.get("tool_call_parser") != "qwen3_coder":
            raise SystemExit(f"{gpu} server configuration is not c4 native-tool mode")

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for gpu, base in ENDPOINTS.items():
            for local_index in range(4):
                value = int(gpu[-1]) * 100 + local_index
                tasks.append(pool.submit(probe, gpu, base, value))
        results = [task.result() for task in tasks]

    by_gpu = {}
    for gpu in ENDPOINTS:
        rows = sorted((row for row in results if row["gpu"] == gpu), key=lambda row: row["value"])
        by_gpu[gpu] = {
            "request_count": len(rows),
            "launch_span_seconds": max(row["started"] for row in rows) - min(row["started"] for row in rows),
            "all_succeeded": len(rows) == 4,
            "requests": rows,
        }
    output = {"server_values": server_values, "four_request_batches": by_gpu}
    (output_dir / "c4-live-verification.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
