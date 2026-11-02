"""Versioned, reversible KV-capacity deployment after the original batch ends."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request


NAMES = {0: "pilot-native-gpu0-v1", 1: "pilot-native-gpu1-v1", 2: "pilot-native-seven-layer-v1"}


def run(argv, timeout=60):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(**value):
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **value}), flush=True)


def set_option(command, option, value):
    if option in command:
        command[command.index(option) + 1] = str(value)
    else:
        command.extend([option, str(value)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--original", required=True)
    parser.add_argument("--gpu", type=int, choices=NAMES, required=True)
    parser.add_argument("--tokens", type=int, default=65536)
    parser.add_argument("--memory-fraction", type=float, default=0.85)
    parser.add_argument("--cycle", default="repair_cycle_01")
    args = parser.parse_args()
    if not 50176 <= args.tokens <= 131072 or not 0.8 <= args.memory_fraction <= 0.9:
        parser.error("capacity values outside the bounded repair range")
    for part in (args.original, args.cycle):
        if not part or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in part):
            parser.error("invalid artifact identifier")
    root = args.root.resolve()
    watch = root / "outputs/experiment_console_watch" / args.original
    frozen = json.loads((watch / "original_terminal_summary.json").read_text())
    assert frozen["expected"] == len(frozen["runs"]) == 96
    assert frozen["no_active_requests_confirmed"]
    for rid, proof in frozen["runs"].items():
        path = root / "outputs/experiment_console/raw" / args.original / rid / "run.json"
        assert proof["status"] in ("completed", "failed")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == proof["run_json_sha256"]
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/api/state", timeout=5) as response:
            state = json.load(response)
        assert not state["busy"] and all(x["active"] == 0 for x in state["api_limits"])
    except OSError:
        pass  # Console is intentionally offline during maintenance.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = watch / args.cycle / f"gpu{args.gpu}-capacity-{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    name, port = NAMES[args.gpu], 30000 + args.gpu
    old = json.loads(run(["docker", "inspect", name]))[0]
    save(out / "container_before.json", old)
    command = list(old["Config"]["Cmd"])
    for key, expected in (("--max-running-requests", "4"), ("--context-length", "49152"),
                          ("--dtype", "bfloat16"), ("--tool-call-parser", "qwen3_coder")):
        assert command[command.index(key) + 1] == expected
    set_option(command, "--mem-fraction-static", args.memory_fraction)
    set_option(command, "--max-total-tokens", args.tokens)
    launch = ["docker", "run", "-d", "--name", name, "--gpus", f"device={args.gpu}", "--ipc=host",
              "-p", f"127.0.0.1:{port}:30000"]
    for mount in old["Mounts"]:
        assert mount["Type"] == "bind" and not mount["RW"]
        launch.extend(["--mount", f"type=bind,source={mount['Source']},target={mount['Destination']},readonly"])
    restart = old["HostConfig"]["RestartPolicy"]["Name"]
    if restart and restart != "no":
        launch.extend(["--restart", restart])
    launch.extend([old["Image"], *command])
    save(out / "deployment.json", {"gpu": args.gpu, "image": old["Image"], "command": command,
                                  "target_tokens": args.tokens, "memory_fraction": args.memory_fraction})
    backup = f"{name}-before-kv-{stamp}"
    backed_up = stopped = created = False
    try:
        emit(stage="stopping_idle_original_service", gpu=args.gpu, evidence=str(out))
        run(["docker", "stop", "--time", "20", name], timeout=60)
        stopped = True
        run(["docker", "rename", name, backup])
        backed_up = True
        container_id = run(launch)
        created = True
        emit(stage="warming_new_service", gpu=args.gpu, container_id=container_id)
        deadline, last_update = time.monotonic() + 1200, 0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/get_server_info", timeout=5) as response:
                    info = json.load(response)
                states = info.get("internal_states", [])
                capacities = [x["max_total_num_tokens"] for x in [info, *states]
                              if isinstance(x.get("max_total_num_tokens"), int)]
                assert capacities and min(capacities) >= args.tokens, "actual KV pool below requested target"
                assert info["max_running_requests"] == 4 and info["context_length"] == 49152
                keys = ("max_total_num_tokens", "max_req_input_len", "context_length", "max_running_requests",
                        "mem_fraction_static", "dtype", "quantization", "speculative_algorithm",
                        "speculative_num_steps", "speculative_num_draft_tokens", "tool_call_parser", "reasoning_parser")
                save(out / "verified_capacity.json", {"values": {k: info.get(k) for k in keys},
                       "internal_values": [{k: x.get(k) for k in keys} for x in states],
                       "minimum_actual_kv_tokens": min(capacities)})
                save(out / "container_after.json", json.loads(run(["docker", "inspect", name]))[0])
                save(out / "rollback.json", {"active_name": name, "backup_name": backup})
                emit(stage="capacity_verified", gpu=args.gpu, actual_kv_tokens=min(capacities), backup=backup)
                return
            except OSError:
                running = run(["docker", "inspect", "--format", "{{.State.Running}}", name])
                if running != "true":
                    raise RuntimeError("new service exited during warmup")
            if time.monotonic() - last_update > 30:
                emit(stage="waiting_for_warmup", gpu=args.gpu)
                last_update = time.monotonic()
            time.sleep(5)
        raise TimeoutError("new service did not become ready in 1200 seconds")
    except Exception as exc:
        save(out / "deployment_failure.json", {"type": type(exc).__name__, "error": str(exc)})
        if created:
            logs = subprocess.run(["docker", "logs", "--tail", "150", name], capture_output=True, text=True)
            save(out / "failed_service_log.json", {"stdout": logs.stdout, "stderr": logs.stderr})
            run(["docker", "stop", "--time", "20", name], timeout=60)
            run(["docker", "rename", name, f"{name}-failed-kv-{stamp}"])
        if backed_up:
            run(["docker", "rename", backup, name])
        if stopped:
            run(["docker", "start", name])
        emit(stage="restored_original_container_start_requested", gpu=args.gpu, failure=type(exc).__name__)
        raise


if __name__ == "__main__":
    main()
