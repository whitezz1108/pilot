#!/usr/bin/env bash
set -euo pipefail
cd /home/evan/ai/experiments/pilot01
export PILOT01_BASE_URL=http://127.0.0.1:30000/v1
export PILOT01_BASE_URL_2=http://127.0.0.1:30000/v1
export PILOT01_API_KEY=local-no-auth
export PILOT01_API_KEY_2=local-no-auth
exec .venv/bin/python "$@"
