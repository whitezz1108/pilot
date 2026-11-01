#!/usr/bin/env bash
# Run on the designated Linux GPU host; no existing container is stopped.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
[ -x "$PY" ] || { echo "Create the project .venv and install .[dev] first" >&2; exit 1; }
"$PY" scripts/config_manifest.py --check
GPU=2
NAME=pilot-native-seven-layer-v1
IMAGE=sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae
MODEL=/home/evan/ai/models/Qwen3.8-27B
[ -d "$MODEL" ] || { echo "Model directory missing: $MODEL" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null
[ -z "$(nvidia-smi -i "$GPU" --query-compute-apps=pid --format=csv,noheader | tr -d '[:space:]')" ] || { echo "GPU $GPU has active compute processes" >&2; exit 1; }
# Power cap is an explicit precondition, to avoid changing a shared GPU's state.
POWER=$(nvidia-smi -i "$GPU" --query-gpu=power.limit --format=csv,noheader,nounits)
awk -v p="$POWER" 'BEGIN { exit !(p >= 199.9 && p <= 200.1) }' || { echo "GPU $GPU power limit must already be 200 W (actual: $POWER W)" >&2; exit 1; }
docker run -d --name "$NAME" --gpus '"device=2"' --ipc=host \
  -p 127.0.0.1:30002:30000 -v "$MODEL:/models/Qwen3.8-27B:ro" "$IMAGE" \
  python3 -m sglang.launch_server --model-path /models/Qwen3.8-27B \
  --served-model-name Qwen3.8-27B --host 0.0.0.0 --port 30000 --tp 1 \
  --dtype bfloat16 --context-length 49152 --mem-fraction-static 0.8 \
  --max-running-requests 1 --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
  --speculative-algorithm NEXTN --speculative-num-steps 3 --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 4 --disable-radix-cache \
  --preferred-sampling-params '{"max_new_tokens":32768,"temperature":0.6,"top_p":0.95,"top_k":20,"min_p":0.0}' \
  --default-chat-template-kwargs '{"enable_thinking":true,"preserve_thinking":true}'
"$PY" deploy/native/verify.py
