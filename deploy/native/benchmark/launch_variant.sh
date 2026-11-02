#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --power W --concurrency N --container NAME [--gpu GPU] [--port PORT] [--output-dir DIR]" >&2
}

POWER=""; CONCURRENCY=""; NAME=""; GPU=2; PORT=30002; OUT=""
while (($#)); do
  case "$1" in
    --power) POWER=${2:?}; shift 2 ;;
    --concurrency) CONCURRENCY=${2:?}; shift 2 ;;
    --container) NAME=${2:?}; shift 2 ;;
    --gpu) GPU=${2:?}; shift 2 ;;
    --port) PORT=${2:?}; shift 2 ;;
    --output-dir) OUT=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ "$POWER" =~ ^(200|230|240|250|260)$ ]] || { echo "Unsupported benchmark power: $POWER" >&2; exit 2; }
[[ "$CONCURRENCY" =~ ^(2|4|8|16)$ ]] || { echo "Unsupported benchmark concurrency: $CONCURRENCY" >&2; exit 2; }
[[ "$GPU" =~ ^(0|2)$ ]] || { echo "Benchmark GPU must be 0 or 2" >&2; exit 2; }
[[ "$NAME" =~ ^pilot-native-bench-p[0-9]+-c(2|4|8|16)(-[a-zA-Z0-9_.-]+)?$ ]] || {
  echo "Container name must identify a pilot-native-bench power/concurrency variant" >&2; exit 2;
}

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
cd "$ROOT"
PY=.venv/bin/python
[[ -x "$PY" ]] || { echo "Create .venv and install the project first" >&2; exit 1; }
IMAGE=sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae
MODEL=/home/evan/ai/models/Qwen3.8-27B
OUT=${OUT:-outputs/native-performance-sweep-20261101/launches/$NAME}
mkdir -p "$OUT"
STARTED=0
on_error() {
  code=$?
  if [[ "$STARTED" == "1" ]]; then
    docker logs "$NAME" >"$OUT/stdout.log" 2>"$OUT/stderr.log" || true
    docker stop "$NAME" >/dev/null 2>&1 || true
    echo "Variant failed verification and was stopped but retained: $NAME" >&2
  fi
  exit "$code"
}
trap on_error ERR

"$PY" scripts/config_manifest.py --check
[[ -d "$MODEL" ]] || { echo "Model directory missing: $MODEL" >&2; exit 1; }
docker image inspect "$IMAGE" >"$OUT/image-inspect.json"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Container already exists (it is intentionally never removed automatically): $NAME" >&2
  exit 1
fi

nvidia-smi -i "$GPU" \
  --query-gpu=name,persistence_mode,power.limit,power.min_limit,power.max_limit,temperature.gpu,memory.used \
  --format=csv >"$OUT/gpu-preflight.csv"
nvidia-smi -q -i "$GPU" -d ECC >"$OUT/gpu-ecc-preflight.txt"
UNCORRECTED=$(nvidia-smi -i "$GPU" --query-gpu=ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits | tr -d '[:space:]')
[[ "$UNCORRECTED" =~ ^[0-9]+$ && "$UNCORRECTED" -eq 0 ]] || {
  echo "GPU $GPU uncorrectable volatile ECC state is not clean: $UNCORRECTED" >&2; exit 1;
}
PERSISTENCE=$(nvidia-smi -i "$GPU" --query-gpu=persistence_mode --format=csv,noheader | tr -d '[:space:]')
[[ "$PERSISTENCE" == "Enabled" ]] || { echo "GPU $GPU persistence mode is not Enabled" >&2; exit 1; }
RANGE=$(nvidia-smi -i "$GPU" --query-gpu=power.min_limit,power.max_limit --format=csv,noheader,nounits)
awk -F, '{gsub(/ /,""); exit !($1 <= 150.1 && $2 >= 299.9)}' <<<"$RANGE" || {
  echo "GPU $GPU does not report the expected 150-300 W range: $RANGE" >&2; exit 1;
}
ACTUAL_POWER=$(nvidia-smi -i "$GPU" --query-gpu=power.limit --format=csv,noheader,nounits)
awk -v actual="$ACTUAL_POWER" -v expected="$POWER" 'BEGIN { exit !(actual >= expected-0.1 && actual <= expected+0.1) }' || {
  echo "Set GPU $GPU to $POWER W before launch (actual: $ACTUAL_POWER W)" >&2; exit 1;
}
PROCESSES=$(nvidia-smi -i "$GPU" --query-compute-apps=pid,process_name --format=csv,noheader,nounits)
if [[ -n "$PROCESSES" ]]; then
  UNEXPECTED=$(awk -F, -v gpu="$GPU" '
    { name=$2; gsub(/^ +| +$/, "", name) }
    gpu != "0" || name !~ /gnome-remote-desktop-daemon$/ { print }
  ' <<<"$PROCESSES")
  [[ -z "$UNEXPECTED" ]] || { echo "GPU $GPU has active unexpected compute processes: $UNEXPECTED" >&2; exit 1; }
  printf '%s\n' "$PROCESSES" >"$OUT/allowed-preexisting-processes.csv"
fi
git rev-parse HEAD >"$OUT/git-commit.txt"

docker run -d --name "$NAME" \
  --label pilot01.native-benchmark=true \
  --label pilot01.native-benchmark.power-w="$POWER" \
  --label pilot01.native-benchmark.concurrency="$CONCURRENCY" \
  --label pilot01.native-benchmark.gpu="$GPU" \
  --gpus "device=$GPU" --ipc=host \
  -p "127.0.0.1:${PORT}:30000" -v "$MODEL:/models/Qwen3.8-27B:ro" "$IMAGE" \
  python3 -m sglang.launch_server --model-path /models/Qwen3.8-27B \
  --served-model-name Qwen3.8-27B --host 0.0.0.0 --port 30000 --tp 1 \
  --dtype bfloat16 --context-length 49152 --mem-fraction-static 0.8 \
  --max-running-requests "$CONCURRENCY" --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
  --speculative-algorithm NEXTN --speculative-num-steps 3 --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 4 --disable-radix-cache \
  --enable-metrics \
  --preferred-sampling-params '{"max_new_tokens":32768,"temperature":0.6,"top_p":0.95,"top_k":20,"min_p":0.0}' \
  --default-chat-template-kwargs '{"enable_thinking":true,"preserve_thinking":true}' \
  >"$OUT/container-id.txt"
STARTED=1

"$PY" deploy/native/benchmark/verify_variant.py \
  --container "$NAME" --power "$POWER" --concurrency "$CONCURRENCY" \
  --gpu "$GPU" --base-url "http://127.0.0.1:${PORT}" --output-dir "$OUT"
docker logs "$NAME" >"$OUT/stdout.log" 2>"$OUT/stderr.log" || true
trap - ERR
echo "Benchmark service verified: $NAME (p${POWER}, c${CONCURRENCY})"

