"""Fail closed on server-reported settings and validate both client request profiles."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from pilot01.config import load_models_v1  # noqa: E402
from pilot01.model.client import ModelMessage, ModelRequest  # noqa: E402
from pilot01.model.openai_compat import OpenAICompatibleClient  # noqa: E402

BASE = "http://127.0.0.1:30002"
def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.load(response)

for _ in range(240):
    try:
        info = get("/get_server_info")
        break
    except OSError:
        time.sleep(5)
else:
    raise SystemExit("server did not become ready")
expected = {"model_path": "/models/Qwen3.8-27B", "served_model_name": "Qwen3.8-27B",
            "dtype": "bfloat16", "quantization": None, "context_length": 49152,
            "tp_size": 1, "mem_fraction_static": 0.8, "max_running_requests": 1,
            "reasoning_parser": "qwen3", "tool_call_parser": "qwen3_coder",
            "speculative_num_steps": 3, "speculative_eagle_topk": 1,
            "speculative_num_draft_tokens": 4, "disable_radix_cache": True,
            "sampling_defaults": "model",
            "preferred_sampling_params": {"max_new_tokens": 32768, "temperature": 0.6,
                                           "top_p": 0.95, "top_k": 20, "min_p": 0.0}}
for key, value in expected.items():
    if key not in info or info[key] != value:
        raise SystemExit(f"server mismatch: {key}: expected {value!r}, got {info.get(key)!r}")
if info.get("speculative_algorithm") not in ("NEXTN", "EAGLE"):
    raise SystemExit("server did not report MTP/NEXTN")
image = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", "pilot-native-seven-layer-v1"], text=True).strip()
if image != "sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae":
    raise SystemExit(f"container image mismatch: {image}")
power = float(subprocess.check_output(["nvidia-smi", "-i", "2", "--query-gpu=power.limit", "--format=csv,noheader,nounits"], text=True).strip())
if abs(power - 200.0) > 0.1:
    raise SystemExit(f"GPU power limit mismatch: {power} W")
print(f"container image verified: {image}; GPU 2 power limit: {power} W")
models = get("/v1/models")
served_models = models.get("data", [])
if not any(model.get("id") == "Qwen3.8-27B" and model.get("max_model_len") == 49152
           for model in served_models):
    raise SystemExit("served model missing from /v1/models")
print(json.dumps({"server": {key: info[key] for key in expected},
                  "speculative_algorithm": info["speculative_algorithm"],
                  "models": served_models}, ensure_ascii=False, indent=2))

client = OpenAICompatibleClient(base_url=BASE + "/v1", api_key="local-probe", tool_transport="native")
for profile, filename, temperature in [
    ("historical-reproduction", "models_bf16_mtp_xhigh_native_v1.yaml", 1.0),
    ("recommended-primary", "models_recommended_primary_v1.yaml", 0.6),
]:
    config = load_models_v1(ROOT / "config" / filename)
    for role in ("manager", "compliance"):
        params = config.roles[role]
        request = ModelRequest(messages=(ModelMessage(role="user", content="probe"),), params=params,
                               role=role, prompt_version="probe")
        payload = json.loads(client.build_request(request).data)
        for key, value in {"model": "Qwen3.8-27B", "temperature": temperature, "top_p": 0.95, "top_k": 20, "min_p": 0.0}.items():
            if payload.get(key) != value:
                raise SystemExit(f"{profile}/{role} request mismatch: {key}")
        print(f"{profile}/{role}: request sampling parameters verified: "
              f"temperature={temperature}, top_p=0.95, top_k=20, min_p=0")
print("Sampling checks cover constructed client requests; the server does not echo per-request effective sampling.")

# A bounded synthetic request checks that the configured endpoint accepts the
# recommended sampling fields and returns a parsed native tool call.
probe = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Call the probe tool with value 7. Do not answer in text."}],
         "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
         "max_tokens": 2048, "tools": [{"type": "function", "function": {
             "name": "probe", "description": "Return the supplied value.",
             "parameters": {"type": "object", "properties": {"value": {"type": "integer"}},
                            "required": ["value"], "additionalProperties": False}}}],
         "tool_choice": "required"}
req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(probe).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=360) as response:
    result = json.load(response)
choice = result["choices"][0]
calls = choice.get("message", {}).get("tool_calls") or []
if result.get("model") != "Qwen3.8-27B" or choice.get("finish_reason") != "tool_calls" or not calls:
    raise SystemExit("live native-tool probe did not return structured tool calls")
if calls[0].get("function", {}).get("name") != "probe" or json.loads(calls[0]["function"]["arguments"]).get("value") != 7:
    raise SystemExit("live native-tool probe returned unexpected function or arguments")
print(json.dumps({"probe_request": probe, "probe_response": result}, ensure_ascii=False))
