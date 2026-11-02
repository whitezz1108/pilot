"""Capture read-only serving evidence for an already-confirmed failed run."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    return {"argv": argv, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    for value in (args.batch, args.run):
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
            parser.error("invalid batch/run identifier")
    root, batch, run_id = args.root.resolve(), args.batch, args.run
    original = root / "outputs/experiment_console/raw" / batch / run_id
    artifact = json.loads((original / "run.json").read_text())
    if artifact["execution"]["status"] != "failed":
        raise SystemExit("capture requires a confirmed failed run")
    out = root / "outputs/experiment_console_watch" / batch / "failures" / run_id / "environment"
    out.mkdir(parents=True, exist_ok=True)
    if (out / "evidence_manifest.json").exists():
        print(json.dumps({"status": "already_captured", "directory": str(out)}))
        return
    calls = [json.loads(line) for line in (original / "model_calls.jsonl").read_text().splitlines() if line.strip()]
    last = calls[-1]
    errors = [e for e in (last.get("provider_response") or {}).get("stream_events", [])
              if isinstance(e.get("data"), dict) and e["data"].get("error")]
    save(out / "last_request_failure.json", {
        "captured_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
        "model_call_id": last.get("model_call_id"), "role": last.get("role"),
        "requested_at": last.get("requested_at"), "responded_at": last.get("responded_at"),
        "provider_request_id": (last.get("provider_response") or {}).get("id"),
        "params": last.get("params"), "reported_usage": last.get("usage"),
        "stream_errors": errors,
        "native_tools_present_in_last_rendered_request": "### tools" in last.get("rendered_input", ""),
        "explicit_final_only_instruction_present": "The source-tool budget is exhausted" in last.get("rendered_input", ""),
    })
    start = datetime.fromisoformat(last["requested_at"]) - timedelta(seconds=10)
    end = datetime.fromisoformat(last["responded_at"]) + timedelta(seconds=10)
    since, until = [v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") for v in (start, end)]
    keys = ("max_total_num_tokens", "max_req_input_len", "context_length", "max_running_requests",
            "mem_fraction_static", "dtype", "quantization", "speculative_algorithm",
            "speculative_num_steps", "speculative_num_draft_tokens", "tool_call_parser", "reasoning_parser")
    for gpu, name in ((0, "pilot-native-gpu0-v1"), (1, "pilot-native-gpu1-v1"), (2, "pilot-native-seven-layer-v1")):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{30000 + gpu}/get_server_info", timeout=10) as response:
                data = json.load(response)
            save(out / f"gpu{gpu}_server_capacity.json", {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "values": {key: data.get(key) for key in keys},
                "internal_values": [{key: state.get(key) for key in keys} for state in data.get("internal_states", [])],
            })
        except Exception as exc:
            save(out / f"gpu{gpu}_server_capacity.json", {"error": f"{type(exc).__name__}: {exc}"})
        save(out / f"gpu{gpu}_serving_log.json", command([
            "docker", "logs", "--timestamps", "--since", since, "--until", until, name]))
    save(out / "gpu_memory_processes.json", command([
        "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv"]))
    save(out / "gpu_memory_snapshot.json", command([
        "nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,power.limit", "--format=csv"]))
    save(out / "evidence_manifest.json", {
        "captured_at": datetime.now(timezone.utc).isoformat(), "source_run": str(original),
        "files": {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
                  for p in sorted(out.iterdir()) if p.is_file()},
    })
    print(json.dumps({"status": "captured", "directory": str(out), "stream_error_events": len(errors)}))


if __name__ == "__main__":
    main()
