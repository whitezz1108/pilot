"""Wait for new confirmed failures or batch completion without starting work."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def emit(**values):
    print(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), **values},
                     ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--expected", type=int, default=96)
    args = parser.parse_args()
    root = args.root.resolve()
    watch = root / "outputs/experiment_console_watch" / args.batch
    selected = read(watch / "baseline.json")["selected_run_ids"]
    mission = read(watch / "mission.json")
    if len(selected) != args.expected or len(set(selected)) != args.expected:
        raise ValueError("unexpected original selection")
    watched_pids = [mission["original_console_pid"], mission["original_watcher_pid"]]
    emit(event="waiter_started", pid=os.getpid(), batch=args.batch, expected=args.expected)
    last_progress = 0
    while True:
        terminal, unreviewed = [], []
        for run_id in selected:
            marker = root / "outputs/experiment_console/raw" / args.batch / run_id / "run.json"
            if not marker.is_file():
                continue
            artifact = read(marker)
            status = (artifact.get("execution") or {}).get("status")
            if status in ("completed", "failed"):
                terminal.append(status)
            if status == "failed" and not (watch / "failures" / run_id / "analysis.json").is_file():
                unreviewed.append({"run_id": run_id, "failure": artifact.get("failure")})
        counts = dict(Counter(terminal))
        if unreviewed:
            emit(event="new_confirmed_failure", counts=counts, failures=unreviewed)
            return
        if len(terminal) == args.expected:
            emit(event="all_original_runs_terminal", counts=counts, expected=args.expected)
            return
        observation = read(watch / "latest.json")
        if (observation.get("api_batch_matches")
                and observation.get("api_session_status") in ("finished", "error", "stopped", "interrupted")):
            emit(event="batch_needs_attention", counts=counts,
                 api_session_status=observation["api_session_status"],
                 note="Original selection still has missing terminal markers; inspect the batch handle before any restart.")
            return
        live = {str(pid): Path("/proc", str(pid), "cmdline").exists() for pid in watched_pids}
        if not all(live.values()):
            emit(event="observation_needs_attention", counts=counts, live_pids=live)
            return
        if time.monotonic() - last_progress >= 300:
            emit(event="verified_wait", counts=counts, expected=args.expected, live_pids=live)
            last_progress = time.monotonic()
        time.sleep(10)


if __name__ == "__main__":
    main()
