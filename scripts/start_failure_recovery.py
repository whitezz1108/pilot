"""Launch only unresolved original failures through the real console workflow."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

from audit_failure_recovery import audit_delivery


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--original", required=True)
    parser.add_argument("--console-pid", type=int, required=True)
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    base, watches = root / "outputs/experiment_console", root / "outputs/experiment_console_watch"
    watch = watches / args.original
    frozen = read(watch / "original_terminal_summary.json")
    assert frozen["expected"] == len(frozen["runs"]) == 96 and frozen["no_active_requests_confirmed"]
    plan = read(base / "manifests" / args.original / "run_plan.json")
    jobs = {job["run_id"]: job for job in plan["jobs"]}
    mapping_path = watch / "recovery_map.json"
    mapping = read(mapping_path) if mapping_path.exists() else {}
    needed = []
    for rid, proof in frozen["runs"].items():
        marker = base / "raw" / args.original / rid / "run.json"
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == proof["run_json_sha256"]
        if proof["status"] != "failed":
            continue
        link = mapping.get(rid, {"experiment_id": args.original, "run_id": rid})
        if rid in mapping and audit_delivery(base / "raw" / link["experiment_id"] / link["run_id"], jobs[rid])["valid"]:
            continue
        assert (watches / link["experiment_id"] / "failures" / link["run_id"] / "analysis.json").is_file(), "failure must be analyzed before retry"
        needed.append(rid)
    assert needed, "no unresolved original failures remain"
    if not args.execute:
        print(json.dumps({"dry_run": True, "original_run_ids": needed, "concurrency": 4,
                          "manager_gpus": [0], "compliance_gpus": [1]}))
        return
    assert "pilot01.experiment.console" in Path(f"/proc/{args.console_pid}/cmdline").read_bytes().decode()
    url = "http://127.0.0.1:8766"
    with urllib.request.urlopen(url + "/", timeout=20) as response:
        token = re.search(r"const TOKEN='([^']+)'", response.read().decode()).group(1)
    def get(path):
        with urllib.request.urlopen(url + path, timeout=30) as response:
            return json.load(response)
    def post(path, value):
        request = urllib.request.Request(url + path, data=json.dumps(value).encode(),
            headers={"Content-Type": "application/json", "X-Console-Token": token}, method="POST")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    config = get("/api/config")
    runtime = config["runtime_parameters"]
    assert config["execution_backend"] == "local-gpu" and runtime["native_output_format"] == "closed-final-or-tool-v1"
    assert runtime["max_tool_requests_per_node"] == 16 and config["output_limit"] == 16384 and runtime["prompt_version"] == "v6"
    assert not get("/api/state")["busy"]
    selection = {"a": ["A0", "A1"], "v": ["V0", "V1"], "d": ["D0"], "e": ["E1"],
                 "cohort": "new40", "case_ids": sorted({jobs[rid]["case_id"] for rid in needed}),
                 "repeat": max(jobs[rid]["repeat_index"] for rid in needed) + 1}
    draft = post("/api/prepare", selection)
    candidates = {(row["case_id"], row["condition"], row["memo"], row["repeat"] - 1): row for row in draft["jobs"]}
    links = {}
    for rid in needed:
        job = jobs[rid]
        row = candidates[(job["case_id"], job["condition_id"], job["error_condition"], job["repeat_index"])]
        assert (row["a"] == "A1") == job["source_access"]
        assert (row["v"] == "V1") == job["verification_required"]
        links[rid] = {"experiment_id": draft["draft_id"], "run_id": row["run_id"]}
    launch = {"draft_id": draft["draft_id"], "run_ids": [link["run_id"] for link in links.values()],
              "concurrency": 4, "manager_gpus": [0], "compliance_gpus": [1]}
    out = watch / f"repair_cycle_{args.cycle:02d}"
    out.mkdir(parents=True, exist_ok=True)
    record = {"purpose": "engineering_failure_recovery", "stage": "prepared", "original": args.original,
              "recovery_batch": draft["draft_id"], "created_at": datetime.now(timezone.utc).isoformat(),
              "links": links, "launch": launch}
    record_path = out / "recovery_cycle.json"
    if record_path.exists():
        raise ValueError("this repair cycle already has a prepared launch; inspect it rather than duplicating")
    save(record_path, record)
    save(mapping_path, {**mapping, **links})
    record["start_response"] = post("/api/start", launch)
    new_plan = read(base / "manifests" / draft["draft_id"] / "run_plan.json")
    new_jobs = {job["run_id"]: job for job in new_plan["jobs"]}
    for old_id, link in links.items():
        for key in ("case_id", "contract_id", "condition_id", "error_condition", "repeat_index", "source_access", "verification_required", "model_fingerprint"):
            assert jobs[old_id][key] == new_jobs[link["run_id"]][key], "recovery identity/parameters changed: " + key
    record["profile_fingerprint"] = new_plan["profile_fingerprint"]
    record["stage"] = "running"
    new_watch = watches / draft["draft_id"]
    new_watch.mkdir(parents=True, exist_ok=True)
    with (new_watch / "watcher.log").open("a") as output:
        child = subprocess.Popen([sys.executable, "-u", str(root / "scripts/watch_experiment_batch.py"),
            "--root", str(root), "--batch", draft["draft_id"], "--expected", str(len(links)), "--interval", "30"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    record["watcher_pid"] = child.pid
    save(record_path, record)
    save(new_watch / "mission.json", {"parent_original_batch": args.original,
        "original_console_pid": args.console_pid, "original_watcher_pid": child.pid,
        "expected_recovery_runs": len(links), "links": links})
    mission = read(watch / "mission.json")
    mission["stage"] = "watching_repair_cycle_" + str(args.cycle)
    mission["current_recovery_batch"] = draft["draft_id"]
    mission["current_console_pid"] = args.console_pid
    mission["repair_cycles"].append(record)
    save(watch / "mission.json", mission)
    print(json.dumps({"stage": "recovery_started", "batch": draft["draft_id"], "selected": len(links),
                      "watcher_pid": child.pid, "concurrency": 4, "manager_gpus": [0], "compliance_gpus": [1]}))


if __name__ == "__main__":
    main()
