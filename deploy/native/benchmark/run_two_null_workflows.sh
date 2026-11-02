#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/evan/ai/staging/protocol-refactor-20261101
OUT=/home/evan/ai/benchmarks/two_null_engineering_20261101_v3
SOURCE=/home/evan/ai/benchmarks/native_tool_failed_rerun_20261101
REGISTRY=$ROOT/data/pilot01/clean80_v1/case_registry_v2.json
EXECUTION=$ROOT/config/execution_native_tools_v1.yaml
PY=$ROOT/.venv/bin/python
export PYTHONPATH=$ROOT/src

mkdir -p "$OUT/tasks" "$OUT/audits/seed303" "$OUT/audits/seed101"
cd "$ROOT"

git rev-parse HEAD >"$OUT/git-commit.txt"
git status --short >"$OUT/git-status.txt"
nvidia-smi -i 2 --query-gpu=name,persistence_mode,power.limit,power.min_limit,power.max_limit,temperature.gpu,memory.used --format=csv >"$OUT/gpu-preflight.csv"
nvidia-smi -q -i 2 -d ECC >"$OUT/gpu-ecc-preflight.txt"
docker inspect pilot-native-seven-layer-v1 >"$OUT/docker-inspect.json"
curl -fsS http://127.0.0.1:30002/get_server_info >"$OUT/server-info.json"
curl -fsS http://127.0.0.1:30002/v1/models >"$OUT/models.json"

if [[ ! -f "$OUT/tasks/manifests/two-null-engineering-20261101-seed303/run_plan.json" ]]; then
  "$PY" -m pilot01.experiment plan \
    --profile "$SOURCE/config/seed303/experiment.yaml" --registry "$REGISTRY" --out "$OUT/tasks" \
    --experiment-id two-null-engineering-20261101-seed303 --repeat 1 --seed 26 \
    --conditions A1V0D0 --cases CASE-0032
fi
if [[ ! -f "$OUT/tasks/manifests/two-null-engineering-20261101-seed101/run_plan.json" ]]; then
  "$PY" -m pilot01.experiment plan \
    --profile "$SOURCE/config/seed101/experiment.yaml" --registry "$REGISTRY" --out "$OUT/tasks" \
    --experiment-id two-null-engineering-20261101-seed101 --repeat 1 --seed 26 \
    --conditions A1V0D0 --cases CASE-0032
fi

filter_e1_plan() {
  local plan=$1
  "$PY" -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); x=json.loads(p.read_text()); x["jobs"]=[j for j in x["jobs"] if j["error_condition"]=="E1"]; x["selected_error_conditions"]=["E1"]; assert len(x["jobs"])==1; p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+"\n")' "$plan"
  "$PY" -c 'import sys; from pathlib import Path; from pilot01.experiment.runspec import RunPlan,write_run_plan_csv; p=Path(sys.argv[1]); plan=RunPlan.load_json(p); assert len(plan.jobs)==1 and plan.jobs[0].error_condition=="E1"; write_run_plan_csv(plan,p.with_suffix(".csv"))' "$plan"
}
filter_e1_plan "$OUT/tasks/manifests/two-null-engineering-20261101-seed303/run_plan.json"
filter_e1_plan "$OUT/tasks/manifests/two-null-engineering-20261101-seed101/run_plan.json"

export PILOT01_BASE_URL=http://127.0.0.1:30002/v1
export PILOT01_BASE_URL_2=http://127.0.0.1:30002/v1
export PILOT01_API_KEY=local-no-auth
export PILOT01_API_KEY_2=local-no-auth

echo 'timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total' >"$OUT/gpu-telemetry.csv"
nvidia-smi -i 2 --query-gpu=timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits -l 1 >>"$OUT/gpu-telemetry.csv" &
MONITOR_PID=$!
cleanup_monitor() {
  kill "$MONITOR_PID" >/dev/null 2>&1 || true
  wait "$MONITOR_PID" >/dev/null 2>&1 || true
}
trap cleanup_monitor EXIT

run_case() {
  local seed=$1
  local experiment="two-null-engineering-20261101-seed${seed}"
  local audit="$OUT/audits/seed${seed}"
  local profile="$SOURCE/config/seed${seed}/experiment.yaml"
  "$PY" scripts/audit_native_workflow.py --audit-dir "$audit" run \
    --profile "$profile" --registry "$REGISTRY" --out "$OUT/tasks" \
    --plan "$OUT/tasks/manifests/$experiment/run_plan.json" --live --fail-fast \
    --execution-config "$EXECUTION"
}

if [[ ! -f "$OUT/audits/seed303/completed" ]]; then
  run_case 303 2>&1 | tee "$OUT/audits/seed303/runner.log"
  touch "$OUT/audits/seed303/completed"
fi
if [[ ! -f "$OUT/audits/seed101/completed" ]]; then
  run_case 101 2>&1 | tee "$OUT/audits/seed101/runner.log"
  touch "$OUT/audits/seed101/completed"
fi

cleanup_monitor
trap - EXIT

nvidia-smi -i 2 --query-gpu=timestamp,power.limit,temperature.gpu,memory.used,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total --format=csv >"$OUT/gpu-postflight.csv"
nvidia-smi -q -i 2 -d ECC >"$OUT/gpu-ecc-postflight.txt"
docker logs pilot-native-seven-layer-v1 >"$OUT/server-stdout.log" 2>"$OUT/server-stderr.log" || true
echo "two null-history engineering workflows completed"

