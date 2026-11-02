#!/usr/bin/env bash
set -euo pipefail

stamp="$(date +%Y%m%d-%H%M%S)"
evidence="/home/evan/ai/benchmarks/native_all_c4_${stamp}"
image="sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae"
model="/home/evan/ai/models/Qwen3.8-27B"
names=(pilot-native-gpu0-v1 pilot-native-gpu1-v1 pilot-native-seven-layer-v1)
gpus=(0 1 2)
ports=(30000 30001 30002)
backups=()
started=()

mkdir -p "$evidence"
docker inspect "${names[@]}" > "$evidence/containers-before.json"
nvidia-smi --query-gpu=index,uuid,power.limit,temperature.gpu,memory.used --format=csv > "$evidence/gpus-before.csv"

rollback() {
  code=$?
  if (( code == 0 )); then
    return
  fi
  echo "deployment failed; restoring previous c1 services" >&2
  for name in "${started[@]}"; do
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
  for i in "${!names[@]}"; do
    if [[ -n "${backups[$i]:-}" ]] && docker container inspect "${backups[$i]}" >/dev/null 2>&1; then
      docker rename "${backups[$i]}" "${names[$i]}" || true
      docker start "${names[$i]}" || true
    fi
  done
  exit "$code"
}
trap rollback EXIT

for i in "${!names[@]}"; do
  backup="${names[$i]}-c1-backup-${stamp}"
  docker stop "${names[$i]}"
  docker rename "${names[$i]}" "$backup"
  backups+=("$backup")
done

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
  --max-running-requests 4
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

for i in "${!names[@]}"; do
  docker run -d --name "${names[$i]}" --gpus "device=${gpus[$i]}" --ipc=host \
    -p "127.0.0.1:${ports[$i]}:30000" \
    -v "$model:/models/Qwen3.8-27B:ro" \
    "$image" "${common[@]}" > "$evidence/gpu${gpus[$i]}-container-id.txt"
  started+=("${names[$i]}")
done

for port in "${ports[@]}"; do
  ready=0
  for _ in $(seq 1 180); do
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
  actual="$(curl -fsS "http://127.0.0.1:${port}/get_server_info" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("max_running_requests"))')"
  if [[ "$actual" != "4" ]]; then
    echo "port ${port} reports max_running_requests=${actual}, expected 4" >&2
    exit 1
  fi
done

docker inspect "${names[@]}" > "$evidence/containers-after.json"
nvidia-smi --query-gpu=index,uuid,power.limit,temperature.gpu,memory.used --format=csv > "$evidence/gpus-after.csv"
printf '%s\n' "${backups[@]}" > "$evidence/rollback-container-names.txt"
printf '%s\n' "$evidence" > /home/evan/ai/benchmarks/latest_native_all_c4.txt

trap - EXIT
echo "EVIDENCE_DIR=$evidence"
for i in "${!names[@]}"; do
  echo "GPU=${gpus[$i]} CONTAINER=${names[$i]} PORT=${ports[$i]} BACKUP=${backups[$i]}"
done
