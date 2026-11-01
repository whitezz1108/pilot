#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/evan/ai/staging/protocol-refactor-native-v2-20261101
OUT=/home/evan/ai/benchmarks/native_v2_protocol_rerun_20261101
REGISTRY=$ROOT/data/pilot01/clean80_v1/case_registry_v2.json
PROFILE=$ROOT/config/experiment_native_tools_v2.yaml
EXECUTION=$ROOT/config/execution_native_tools_v1.yaml
PY=$ROOT/.venv/bin/python
export PYTHONPATH=$ROOT/src
export PILOT01_BASE_URL=http://127.0.0.1:30002/v1
export PILOT01_API_KEY=local-no-auth

mkdir -p "$OUT/telemetry" "$OUT/preflight" "$OUT/postflight"
cd "$ROOT"

git rev-parse HEAD >"$OUT/preflight/base-commit.txt"
git status --short >"$OUT/preflight/worktree-status.txt"
sha256sum "$PROFILE" "$EXECUTION" "$REGISTRY" >"$OUT/preflight/input-sha256.txt"
curl -fsS http://127.0.0.1:30002/get_server_info >"$OUT/preflight/server-info.json"
curl -fsS http://127.0.0.1:30002/v1/models >"$OUT/preflight/models.json"
docker inspect pilot-native-seven-layer-v1 >"$OUT/preflight/docker-inspect.json"
nvidia-smi -i 2 --query-gpu=index,name,power.limit,temperature.gpu,memory.used,utilization.gpu --format=csv >"$OUT/preflight/gpu.csv"

make_plan() {
  local experiment=$1 case_id=$2 error=$3
  local plan="$OUT/tasks/manifests/$experiment/run_plan.json"
  if [[ ! -f "$plan" ]]; then
    "$PY" -m pilot01.experiment plan \
      --profile "$PROFILE" --registry "$REGISTRY" --out "$OUT/tasks" \
      --experiment-id "$experiment" --repeat 1 --seed 42 \
      --conditions A1V0D0 --cases "$case_id"
    "$PY" scripts/filter_run_plan_error.py --plan "$plan" --error "$error"
  fi
}

make_plan native-v2-budget-rerun-20261101-r01 CASE-0014 E0
make_plan native-v2-cursor-rerun-20261101-r01 CASE-0032 E1

echo 'timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total' >"$OUT/telemetry/gpu2.csv"
nvidia-smi -i 2 --query-gpu=timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits -l 1 >>"$OUT/telemetry/gpu2.csv" &
MONITOR_PID=$!
cleanup() {
  kill "$MONITOR_PID" >/dev/null 2>&1 || true
  wait "$MONITOR_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_one() {
  local experiment=$1
  local audit="$OUT/audits/$experiment"
  mkdir -p "$audit"
  "$PY" scripts/audit_native_workflow.py \
    --audit-dir "$audit" \
    --allowed-base-url http://127.0.0.1:30002/ \
    --allow-missing-seed \
    run --profile "$PROFILE" --registry "$REGISTRY" --out "$OUT/tasks" \
    --plan "$OUT/tasks/manifests/$experiment/run_plan.json" \
    --live --execution-config "$EXECUTION" \
    2>&1 | tee "$audit/runner.log"
}

set +e
run_one native-v2-budget-rerun-20261101-r01
BUDGET_STATUS=${PIPESTATUS[0]}
run_one native-v2-cursor-rerun-20261101-r01
CURSOR_STATUS=${PIPESTATUS[0]}
set -e

cleanup
trap - EXIT
printf 'budget=%s\ncursor=%s\n' "$BUDGET_STATUS" "$CURSOR_STATUS" >"$OUT/postflight/runner-exit-codes.txt"
nvidia-smi -i 2 --query-gpu=index,power.limit,temperature.gpu,memory.used,utilization.gpu,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total --format=csv >"$OUT/postflight/gpu.csv"
docker logs pilot-native-seven-layer-v1 >"$OUT/postflight/server-stdout.log" 2>"$OUT/postflight/server-stderr.log" || true

if (( BUDGET_STATUS != 0 || CURSOR_STATUS != 0 )); then
  exit 1
fi
