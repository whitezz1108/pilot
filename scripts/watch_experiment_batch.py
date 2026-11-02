"""Read-only batch watcher. Archive confirmed terminal failures with raw evidence.

run.json is the runner's atomic completion marker. Neither a missing file nor
an observation timeout is classified as a failed run. This script never starts
experiments, changes their configuration, or invokes a model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def rows(path):
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def call_facts(record):
    response = record.get("provider_response") or {}
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    names = Counter((call.get("function") or {}).get("name") for call in calls)
    return {
        "model_call_id": record.get("model_call_id"),
        "role": record.get("role"),
        "call_index": record.get("call_index"),
        "prompt_version": record.get("prompt_version"),
        "requested_at": record.get("requested_at"),
        "responded_at": record.get("responded_at"),
        "latency_seconds": record.get("latency_seconds"),
        "params": record.get("params"),
        "usage": record.get("usage") or response.get("usage"),
        "finish_reason": record.get("finish_reason") or choice.get("finish_reason"),
        "parse_error": record.get("parse_error"),
        "error": record.get("error"),
        "native_call_count": len(calls),
        "native_function_counts": dict(names),
        "tool_request": record.get("tool_request"),
        "visible_response_characters": record.get("visible_response_characters"),
        "rendered_input_sha256": hashlib.sha256((record.get("rendered_input") or "").encode()).hexdigest(),
    }


def failure_facts(directory):
    artifact = read_json(directory / "run.json")
    execution = artifact.get("execution") or {}
    facts = [call_facts(row) for row in rows(directory / "model_calls.jsonl")]
    tools = list(rows(directory / "tool_calls.jsonl"))
    failure = artifact.get("failure") or {}
    error = failure.get("error") or ""
    signals = []
    if any(row["native_call_count"] > 1 for row in facts):
        signals.append("multiple_native_calls_in_one_response")
    if any(row["finish_reason"] == "length" for row in facts):
        signals.append("output_truncated_at_generation_limit")
    if "cursor" in error.lower():
        signals.append("cursor_related_error_requires_trace_review")
    if "context" in error.lower():
        signals.append("context_related_error_requires_trace_review")
    return {
        "recorded_at": now(), "run_id": artifact.get("run_id"),
        "case_id": artifact.get("case_id"), "condition_id": artifact.get("condition_id"),
        "error_condition": artifact.get("error_condition"),
        "repeat_index": artifact.get("repeat_index"),
        "status": execution.get("status"), "failure": failure,
        "runtime_config": artifact.get("runtime_config"),
        "profile_fingerprint": artifact.get("profile_fingerprint"),
        "manager_delivered": execution.get("manager_output") is not None,
        "compliance_delivered": execution.get("compliance_output") is not None,
        "calls_by_role": dict(Counter(row["role"] for row in facts)),
        "tool_calls_by_node": dict(Counter(row.get("node", row.get("role", "unknown")) for row in tools)),
        "observed_signals": signals,
        "attribution_status": "pending_evidence_review",
        "model_calls": facts,
    }


def process_observation():
    result = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True, timeout=8)
    return [line.strip() for line in result.stdout.splitlines()
            if "pilot01.experiment.console" in line and "watch_experiment" not in line]


def initialize(root, batch, expected):
    base = root / "outputs/experiment_console"
    manifest = base / "manifests" / batch
    plan = read_json(manifest / "run_plan.json")
    selected = read_json(manifest / "launch_selection.json")["run_ids"]
    if len(selected) != expected or len(set(selected)) != expected:
        raise ValueError(f"expected {expected} unique selected runs, found {len(selected)}")
    if set(selected) - {job["run_id"] for job in plan["jobs"]}:
        raise ValueError("selection contains runs outside the frozen plan")
    out = root / "outputs/experiment_console_watch" / batch
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "baseline.json").exists():
        shutil.copytree(manifest, out / "baseline_manifests", dirs_exist_ok=True)
        files = list((root / "src").rglob("*.py")) + list((root / "prompts").glob("*.md"))
        files += list((root / "config").rglob("*.yaml"))
        files = [p for p in files if not p.name.endswith(".local.yaml")]
        hashes = {}
        for source in files:
            relative = source.relative_to(root)
            target = out / "baseline_source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            hashes[relative.as_posix()] = digest(target)
        write_json(out / "baseline.json", {
            "created_at": now(), "experiment_id": batch, "expected": expected,
            "selected_run_ids": selected, "profile_fingerprint": plan.get("profile_fingerprint"),
            "runtime_config": plan.get("runtime_config"), "source_sha256": hashes,
            "policy": "observe_until_all_original_runs_are_terminal; never_modify_original_results",
        })
    return base, out, selected


def inspect_once(base, out, selected, batch, api):
    api_state = None
    api_error = None
    try:
        with urllib.request.urlopen(api.rstrip("/") + "/api/state", timeout=12) as stream:
            api_state = json.load(stream)
    except Exception as exc:
        api_error = f"{type(exc).__name__}: {exc}"
    session = (api_state or {}).get("session") or {}
    api_matches = session.get("experiment_id") == batch
    api_rows = {row["run_id"]: row for row in (api_state or {}).get("rows", [])} if api_matches else {}
    states, failed, newly_archived, deliveries_missing = {}, [], [], []
    archive_errors = []
    for run_id in selected:
        directory = base / "raw" / batch / run_id
        marker = directory / "run.json"
        if not marker.is_file():
            states[run_id] = api_rows.get(run_id, {}).get("status", "unconfirmed")
            continue
        artifact = read_json(marker)
        execution = artifact.get("execution") or {}
        status = execution.get("status", "unconfirmed")
        states[run_id] = status
        if status == "completed" and any(execution.get(key) is None
                                          for key in ("manager_output", "compliance_output")):
            deliveries_missing.append(run_id)
        if status != "failed":
            continue
        failed.append(run_id)
        target = out / "failures" / run_id
        if (target / "evidence_manifest.json").is_file():
            continue
        try:
            raw = target / "raw"
            shutil.copytree(directory, raw, dirs_exist_ok=True)
            shutil.copytree(base / "manifests" / batch, target / "batch_manifests", dirs_exist_ok=True)
            write_json(target / "facts.json", failure_facts(raw))
            copied = list(raw.rglob("*")) + list((target / "batch_manifests").rglob("*"))
            hashes = {p.relative_to(target).as_posix(): {"sha256": digest(p), "bytes": p.stat().st_size}
                      for p in copied if p.is_file()}
            write_json(target / "evidence_manifest.json", {
                "archived_at": now(), "source_run_dir": str(directory), "files": hashes,
                "run_json_source_sha256": digest(marker), "run_json_copy_sha256": digest(raw / "run.json"),
            })
            newly_archived.append(run_id)
        except Exception as exc:
            archive_errors.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
    counts = dict(Counter(states.values()))
    all_terminal = sum(counts.get(key, 0) for key in ("completed", "failed")) == len(selected)
    observation = {
        "observed_at": now(), "monitor_pid": os.getpid(), "experiment_id": batch,
        "expected": len(selected), "counts": counts, "failed_run_ids": failed,
        "newly_archived_failures": newly_archived, "archive_errors": archive_errors,
        "completed_but_missing_delivery": deliveries_missing, "all_original_runs_terminal": all_terminal,
        "stage": "original_complete_needs_failure_review" if all_terminal else "watching_original",
        "api_batch_matches": api_matches, "api_error": api_error,
        "api_session_status": session.get("status"),
        "console_processes": process_observation(),
        "active_rows": [r for r in api_rows.values() if r.get("status") == "running"],
    }
    write_json(out / "latest.json", observation)
    with (out / "observations.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(observation, ensure_ascii=False) + "\n")
    print(json.dumps({k: observation[k] for k in ("observed_at", "counts", "newly_archived_failures", "stage", "api_error")}), flush=True)
    return all_terminal and not archive_errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--expected", type=int, default=96)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--api", default="http://127.0.0.1:8766")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.batch or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in args.batch):
        parser.error("invalid batch id")
    base, out, selected = initialize(args.root.resolve(), args.batch, args.expected)
    lock = (out / "watcher.lock").open("a")
    if not args.once:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_json(out / "watcher_process.json", {"pid": os.getpid(), "started_at": now(), "batch": args.batch})
    while True:
        try:
            terminal = inspect_once(base, out, selected, args.batch, args.api)
        except Exception as exc:
            write_json(out / "observation_error.json", {"observed_at": now(), "error": f"{type(exc).__name__}: {exc}"})
            print(f"observation error: {exc}", flush=True)
            terminal = False
        if args.once or terminal:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
