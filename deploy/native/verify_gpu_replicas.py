"""Verify GPU0/GPU1 replicas against the live GPU2 native-tool server."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


BASES = {
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


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=360) as response:
        return json.load(response)


def main() -> None:
    evidence = Path("/home/evan/ai/benchmarks/latest_gpu2_config_replica.txt").read_text().strip()
    output_dir = Path(evidence)
    infos = {name: get(base + "/get_server_info") for name, base in BASES.items()}
    baseline = {key: infos["gpu2"].get(key) for key in COMPARE_KEYS}
    mismatches = {}
    for name in ("gpu0", "gpu1"):
        current = {key: infos[name].get(key) for key in COMPARE_KEYS}
        if current != baseline:
            mismatches[name] = {
                key: {"gpu2": baseline[key], name: current[key]}
                for key in COMPARE_KEYS
                if current[key] != baseline[key]
            }
    if mismatches:
        raise SystemExit("server configuration mismatch: " + json.dumps(mismatches, ensure_ascii=False))

    probe = {
        "model": "Qwen3.8-27B",
        "messages": [{"role": "user", "content": "Call the probe tool with value 7. Do not answer in text."}],
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
                "description": "Return the supplied value.",
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
    probes = {}
    for name in ("gpu0", "gpu1"):
        response = post(BASES[name] + "/v1/chat/completions", probe)
        choice = response["choices"][0]
        calls = choice.get("message", {}).get("tool_calls") or []
        if choice.get("finish_reason") != "tool_calls" or len(calls) != 1:
            raise SystemExit(f"{name} did not return exactly one native tool call")
        function = calls[0].get("function", {})
        arguments = json.loads(function.get("arguments", "{}"))
        if function.get("name") != "probe" or arguments != {"value": 7}:
            raise SystemExit(f"{name} returned an unexpected native tool call")
        probes[name] = {
            "finish_reason": choice["finish_reason"],
            "tool_name": function["name"],
            "arguments": arguments,
            "model": response.get("model"),
        }

    result = {
        "configuration_matches_gpu2": True,
        "compared_keys": list(COMPARE_KEYS),
        "native_tool_probes": probes,
    }
    (output_dir / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
