"""Verify a clean native-v2 client commit against all three local GPU endpoints.

The Pilot01 runner/client has a Git identity; the SGLang GPU services do not run
Pilot01 source code.  This verifier records both identities separately and
checks that every endpoint exposes the same native-tool-compatible service
configuration before sending one bounded structured-tool probe per endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS = {
    "gpu0": "http://127.0.0.1:30000",
    "gpu1": "http://127.0.0.1:30001",
    "gpu2": "http://127.0.0.1:30002",
}
COMPARE_KEYS = (
    "model_path",
    "served_model_name",
    "dtype",
    "quantization",
    "context_length",
    "tp_size",
    "mem_fraction_static",
    "max_running_requests",
    "reasoning_parser",
    "tool_call_parser",
    "speculative_algorithm",
    "speculative_num_steps",
    "speculative_eagle_topk",
    "speculative_num_draft_tokens",
    "disable_radix_cache",
    "preferred_sampling_params",
)


def command(*args: str) -> str:
    return subprocess.run(
        args, cwd=ROOT, check=True, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def get_json(url: str, timeout: int = 10) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def post_json(url: str, payload: dict[str, Any], timeout: int = 360) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def probe(base_url: str, value: int) -> dict[str, Any]:
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
    response = post_json(base_url + "/v1/chat/completions", payload)
    choice = response["choices"][0]
    calls = choice.get("message", {}).get("tool_calls") or []
    if choice.get("finish_reason") != "tool_calls" or len(calls) != 1:
        raise RuntimeError("endpoint did not return exactly one native tool call")
    function = calls[0].get("function", {})
    arguments = json.loads(function.get("arguments", "{}"))
    if function.get("name") != "probe" or arguments != {"value": value}:
        raise RuntimeError(f"unexpected native tool call: {function!r}")
    return {
        "finish_reason": choice["finish_reason"],
        "tool_name": function["name"],
        "arguments": arguments,
        "provider_call_id_present": bool(calls[0].get("id")),
        "model": response.get("model"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()

    commit = command("git", "rev-parse", "HEAD")
    if args.expected_commit and commit != args.expected_commit:
        raise SystemExit(f"unexpected client commit: {commit} != {args.expected_commit}")
    status = command("git", "status", "--porcelain", "--untracked-files=no")
    if status:
        raise SystemExit("tracked client worktree is not clean")
    command(str(ROOT / ".venv/bin/python"), "scripts/config_manifest.py", "--check")

    manifest_path = ROOT / "config/native-seven-layer-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    strict_bundle = manifest["bundles"]["strict-native-v2"]
    infos = {name: get_json(base + "/get_server_info") for name, base in ENDPOINTS.items()}
    models = {name: get_json(base + "/v1/models") for name, base in ENDPOINTS.items()}
    baseline = {key: infos["gpu2"].get(key) for key in COMPARE_KEYS}
    mismatches = {
        name: {
            key: {"gpu2": baseline[key], name: info.get(key)}
            for key in COMPARE_KEYS if info.get(key) != baseline[key]
        }
        for name, info in infos.items() if name != "gpu2"
    }
    mismatches = {name: values for name, values in mismatches.items() if values}
    if mismatches:
        raise SystemExit("GPU endpoint configuration mismatch: " + json.dumps(mismatches))
    required = {
        "dtype": "bfloat16",
        "quantization": None,
        "context_length": 49152,
        "reasoning_parser": "qwen3",
        "tool_call_parser": "qwen3_coder",
        "disable_radix_cache": True,
    }
    unexpected = {key: {"expected": value, "actual": baseline.get(key)}
                  for key, value in required.items() if baseline.get(key) != value}
    if unexpected:
        raise SystemExit("unexpected native service configuration: " + json.dumps(unexpected))

    probes = {name: probe(base, index) for index, (name, base) in enumerate(ENDPOINTS.items())}
    gpu_csv = command(
        "nvidia-smi",
        "--query-gpu=index,uuid,power.limit,temperature.gpu,memory.used,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total",
        "--format=csv,noheader,nounits",
    )
    output = {
        "schema_version": "pilot01-three-gpu-native-v2-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "git_commit": commit,
            "tracked_worktree_clean": True,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "bundle_version": manifest["bundle_version"],
            "strict_native_v2": strict_bundle,
        },
        "service_identity_boundary": (
            "GPU services run the frozen SGLang image/model configuration; "
            "the Pilot01 Git commit identifies the verified runner/client checkout."
        ),
        "endpoints": {
            name: {
                "base_url": ENDPOINTS[name],
                "server": {key: infos[name].get(key) for key in COMPARE_KEYS},
                "model_list": models[name],
                "probe": probes[name],
            }
            for name in ENDPOINTS
        },
        "endpoint_configs_equal": True,
        "gpu_state_csv": gpu_csv.splitlines(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "git_commit": commit,
        "manifest_sha256": output["client"]["manifest_sha256"],
        "endpoint_configs_equal": True,
        "probes": probes,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
