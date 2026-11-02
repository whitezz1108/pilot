"""Fail closed when a benchmark server differs from the requested variant."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

IMAGE = "sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae"
PREFERRED = {"max_new_tokens": 32768, "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}


def fetch(base: str, path: str) -> object:
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.load(response)


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--power", type=int, required=True, choices=(200, 230, 240, 250, 260))
    parser.add_argument("--concurrency", type=int, required=True, choices=(2, 4, 8, 16))
    parser.add_argument("--gpu", type=int, default=2, choices=(0, 2))
    parser.add_argument("--base-url", default="http://127.0.0.1:30002")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ready-timeout", type=int, default=1200)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + args.ready_timeout
    while True:
        try:
            info = fetch(args.base_url, "/get_server_info")
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise SystemExit("server did not become ready before timeout")
            time.sleep(5)
    if not isinstance(info, dict):
        raise SystemExit("/get_server_info did not return an object")
    expected = {
        "model_path": "/models/Qwen3.8-27B",
        "served_model_name": "Qwen3.8-27B",
        "dtype": "bfloat16",
        "quantization": None,
        "context_length": 49152,
        "tp_size": 1,
        "mem_fraction_static": 0.8,
        "max_running_requests": args.concurrency,
        "reasoning_parser": "qwen3",
        "tool_call_parser": "qwen3_coder",
        "speculative_num_steps": 3,
        "speculative_eagle_topk": 1,
        "speculative_num_draft_tokens": 4,
        "disable_radix_cache": True,
        "enable_metrics": True,
        "sampling_defaults": "model",
        "preferred_sampling_params": PREFERRED,
    }
    mismatches = {key: {"expected": value, "actual": info.get(key)} for key, value in expected.items()
                  if key not in info or info.get(key) != value}
    if info.get("speculative_algorithm") not in ("NEXTN", "EAGLE"):
        mismatches["speculative_algorithm"] = {"expected": "NEXTN/EAGLE", "actual": info.get("speculative_algorithm")}

    inspect = json.loads(command("docker", "inspect", args.container))[0]
    if inspect.get("Image") != IMAGE:
        mismatches["image"] = {"expected": IMAGE, "actual": inspect.get("Image")}
    labels = inspect.get("Config", {}).get("Labels") or {}
    expected_labels = {
        "pilot01.native-benchmark": "true",
        "pilot01.native-benchmark.power-w": str(args.power),
        "pilot01.native-benchmark.concurrency": str(args.concurrency),
        "pilot01.native-benchmark.gpu": str(args.gpu),
    }
    for key, value in expected_labels.items():
        if labels.get(key) != value:
            mismatches[f"label:{key}"] = {"expected": value, "actual": labels.get(key)}
    power = float(command("nvidia-smi", "-i", str(args.gpu), "--query-gpu=power.limit", "--format=csv,noheader,nounits"))
    if abs(power - args.power) > 0.1:
        mismatches["power_limit_w"] = {"expected": args.power, "actual": power}
    models = fetch(args.base_url, "/v1/models")
    served = models.get("data", []) if isinstance(models, dict) else []
    if not any(item.get("id") == "Qwen3.8-27B" and item.get("max_model_len") == 49152 for item in served):
        mismatches["models"] = {"expected": "Qwen3.8-27B/max_model_len=49152", "actual": served}

    (args.output_dir / "server-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "models.json").write_text(json.dumps(models, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "docker-inspect.json").write_text(json.dumps(inspect, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {"verified": not mismatches, "container": args.container, "base_url": args.base_url,
              "gpu": args.gpu, "power_w": power, "concurrency": args.concurrency, "mismatches": mismatches}
    (args.output_dir / "variant-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mismatches:
        raise SystemExit("benchmark variant mismatch: " + json.dumps(mismatches, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

