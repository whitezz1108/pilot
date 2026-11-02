#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/evan/ai/staging/protocol-refactor-20261101
OUT=/home/evan/ai/benchmarks/phase_a_dual_gpu_p200_c2_20261101
EXPERIMENT=phase-a-dual-gpu-p200-c2-20261101-r02
REGISTRY=$ROOT/data/pilot01/clean80_v1/case_registry_v2.json
PROFILE=$ROOT/config/experiment_native_tools_v1.yaml
EXECUTION=$ROOT/config/execution_phase_a_dual_gpu_c2_v1.yaml
PLAN=$OUT/tasks/manifests/$EXPERIMENT/run_plan.json
ATTEMPT=$OUT/attempts/$EXPERIMENT
PY=$ROOT/.venv/bin/python
export PYTHONPATH=$ROOT/src
export PILOT01_BASE_URL=http://127.0.0.1:30003/v1
export PILOT01_BASE_URL_2=http://127.0.0.1:30004/v1
export PILOT01_API_KEY=local-no-auth
export PILOT01_API_KEY_2=local-no-auth

mkdir -p "$ATTEMPT/audit" "$ATTEMPT/preflight" "$ATTEMPT/postflight" "$ATTEMPT/telemetry"
[[ -f "$PLAN" ]] || { echo "Missing frozen run plan: $PLAN" >&2; exit 1; }
[[ ! -e "$ATTEMPT/audit/wire.jsonl" ]] || { echo "Audit already exists; refusing to append" >&2; exit 1; }

cd "$ROOT"
git rev-parse HEAD >"$ATTEMPT/preflight/git-commit.txt"
git status --short >"$ATTEMPT/preflight/git-status.txt"
cp "$PLAN" "$ATTEMPT/preflight/run-plan.json"
cp "$EXECUTION" "$ATTEMPT/preflight/execution-config.yaml"
sha256sum "$PLAN" "$EXECUTION" >"$ATTEMPT/preflight/input-sha256.txt"
for item in manager:30003:pilot-native-bench-p200-c2-manager compliance:30004:pilot-native-bench-p200-c2-compliance; do
  IFS=: read -r role port container <<<"$item"
  curl -fsS "http://127.0.0.1:$port/get_server_info" >"$ATTEMPT/preflight/$role-server-info.json"
  curl -fsS "http://127.0.0.1:$port/v1/models" >"$ATTEMPT/preflight/$role-models.json"
  docker inspect "$container" >"$ATTEMPT/preflight/$role-docker-inspect.json"
done
nvidia-smi -i 0,2 --query-gpu=index,name,persistence_mode,power.limit,temperature.gpu,memory.used --format=csv >"$ATTEMPT/preflight/gpus.csv"
nvidia-smi -q -i 0 -d ECC >"$ATTEMPT/preflight/gpu0-ecc.txt"
nvidia-smi -q -i 2 -d ECC >"$ATTEMPT/preflight/gpu2-ecc.txt"

monitor_metrics() {
  local role=$1 port=$2 destination=$3
  while true; do
    printf '# sampled_at_seconds %s\n' "$(date +%s.%N)" >>"$destination"
    curl -fsS "http://127.0.0.1:$port/metrics" | grep -E '^sglang:(num_running_reqs|num_queue_reqs|token_usage|gen_throughput)' >>"$destination" || true
    sleep 1
  done
}

bash deploy/native/benchmark/monitor_gpu.sh --gpu 0 --output "$ATTEMPT/telemetry/gpu0.csv" & GPU0_PID=$!
bash deploy/native/benchmark/monitor_gpu.sh --gpu 2 --output "$ATTEMPT/telemetry/gpu2.csv" & GPU2_PID=$!
monitor_metrics manager 30003 "$ATTEMPT/telemetry/manager-metrics.prom" & MANAGER_METRICS_PID=$!
monitor_metrics compliance 30004 "$ATTEMPT/telemetry/compliance-metrics.prom" & COMPLIANCE_METRICS_PID=$!
cleanup() {
  kill "$GPU0_PID" "$GPU2_PID" "$MANAGER_METRICS_PID" "$COMPLIANCE_METRICS_PID" >/dev/null 2>&1 || true
  wait "$GPU0_PID" "$GPU2_PID" "$MANAGER_METRICS_PID" "$COMPLIANCE_METRICS_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

set +e
"$PY" scripts/audit_native_workflow.py \
  --audit-dir "$ATTEMPT/audit" \
  --allowed-base-url http://127.0.0.1:30003/ \
  --allowed-base-url http://127.0.0.1:30004/ \
  --allow-missing-seed \
  run --profile "$PROFILE" --registry "$REGISTRY" --out "$OUT/tasks" --plan "$PLAN" \
  --live --live-batch --fail-fast --execution-config "$EXECUTION" \
  2>&1 | tee "$ATTEMPT/runner.log"
RUN_STATUS=${PIPESTATUS[0]}
set -e
cleanup
trap - EXIT

nvidia-smi -i 0,2 --query-gpu=index,power.limit,temperature.gpu,memory.used,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total --format=csv >"$ATTEMPT/postflight/gpus.csv"
nvidia-smi -q -i 0 -d ECC >"$ATTEMPT/postflight/gpu0-ecc.txt"
nvidia-smi -q -i 2 -d ECC >"$ATTEMPT/postflight/gpu2-ecc.txt"
docker logs pilot-native-bench-p200-c2-manager >"$ATTEMPT/postflight/manager-stdout.log" 2>"$ATTEMPT/postflight/manager-stderr.log" || true
docker logs pilot-native-bench-p200-c2-compliance >"$ATTEMPT/postflight/compliance-stdout.log" 2>"$ATTEMPT/postflight/compliance-stderr.log" || true
docker inspect pilot-native-bench-p200-c2-manager >"$ATTEMPT/postflight/manager-docker-inspect.json"
docker inspect pilot-native-bench-p200-c2-compliance >"$ATTEMPT/postflight/compliance-docker-inspect.json"
printf '%s\n' "$RUN_STATUS" >"$ATTEMPT/postflight/runner-exit-code.txt"
exit "$RUN_STATUS"
