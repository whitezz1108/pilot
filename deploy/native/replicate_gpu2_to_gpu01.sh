#!/usr/bin/env bash
set -euo pipefail

stamp="$(date +%Y%m%d-%H%M%S)"
evidence="/home/evan/ai/benchmarks/gpu2_config_replica_${stamp}"
old0="sglang-qwen38-c4"
old1="sglang-qwen32k-test"
backup0="${old0}-backup-${stamp}"
backup1="${old1}-backup-${stamp}"
new0="pilot-native-gpu0-v1"
new1="pilot-native-gpu1-v1"
image="sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae"
model="/home/evan/ai/models/Qwen3.8-27B"

mkdir -p "$evidence"
docker inspect pilot-native-seven-layer-v1 "$old0" "$old1" > "$evidence/containers-before.json"
nvidia-smi --query-gpu=index,uuid,power.limit,temperature.gpu,memory.used --format=csv > "$evidence/gpus-before.csv"

rollback() {
  code=$?
  if (( code == 0 )); then
    return
  fi
  echo "deployment failed; restoring previous GPU0/GPU1 services" >&2
  docker rm -f "$new0" "$new1" >/dev/null 2>&1 || true
  if docker container inspect "$backup0" >/dev/null 2>&1; then
    docker rename "$backup0" "$old0" || true
    docker start "$old0" || true
  fi
  if docker container inspect "$backup1" >/dev/null 2>&1; then
    docker rename "$backup1" "$old1" || true
    docker start "$old1" || true
  fi
  exit "$code"
}
trap rollback EXIT

docker stop "$old0" "$old1"
docker rename "$old0" "$backup0"
docker rename "$old1" "$backup1"

common=(
  python3 -m sglang.launch_server
  --model-path /models/Qwen3.8-27B
  --served-model-name Qwen3.8-27B
  --host 0.0.0.0
  --port 30000
  --tp 1
  --dtype bfloat16
  --context-length 49152
  --mem-fraction-static 0.8
  --max-running-requests 1
  --reasoning-parser qwen3
  --tool-call-parser qwen3_coder
  --speculative-algorithm NEXTN
  --speculative-num-steps 3
  --speculative-eagle-topk 1
  --speculative-num-draft-tokens 4
  --disable-radix-cache
  --preferred-sampling-params '{"max_new_tokens":32768,"temperature":0.6,"top_p":0.95,"top_k":20,"min_p":0.0}'
  --default-chat-template-kwargs '{"enable_thinking":true,"preserve_thinking":true}'
)

docker run -d --name "$new0" --gpus 'device=0' --ipc=host \
  -p 127.0.0.1:30000:30000 \
  -v "$model:/models/Qwen3.8-27B:ro" \
  "$image" "${common[@]}" > "$evidence/gpu0-container-id.txt"

docker run -d --name "$new1" --gpus 'device=1' --ipc=host \
  -p 127.0.0.1:30001:30000 \
  -v "$model:/models/Qwen3.8-27B:ro" \
  "$image" "${common[@]}" > "$evidence/gpu1-container-id.txt"

for port in 30000 30001; do
  ready=0
  for _ in $(seq 1 120); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${port}/v1/models" > "$evidence/models-${port}.json"; then
      ready=1
      break
    fi
    sleep 5
  done
  if (( ready == 0 )); then
    echo "port ${port} did not become ready" >&2
    exit 1
  fi
done

docker inspect "$new0" "$new1" > "$evidence/containers-after.json"
nvidia-smi --query-gpu=index,uuid,power.limit,temperature.gpu,memory.used --format=csv > "$evidence/gpus-after.csv"
printf '%s\n' 'GPU0/GPU1 retain their existing 300 W host power limits; this account cannot change NVML power limits.' > "$evidence/power-limit-note.txt"
printf '%s\n' "$backup0" "$backup1" > "$evidence/rollback-container-names.txt"
printf '%s\n' "$evidence" > /home/evan/ai/benchmarks/latest_gpu2_config_replica.txt

trap - EXIT
echo "EVIDENCE_DIR=$evidence"
echo "GPU0_CONTAINER=$new0 PORT=30000 BACKUP=$backup0"
echo "GPU1_CONTAINER=$new1 PORT=30001 BACKUP=$backup1"
