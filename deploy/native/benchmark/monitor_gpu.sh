#!/usr/bin/env bash
set -euo pipefail
GPU=2; OUT=""; PID_FILE=""
while (($#)); do
  case "$1" in
    --gpu) GPU=${2:?}; shift 2 ;;
    --output) OUT=${2:?}; shift 2 ;;
    --pid-file) PID_FILE=${2:?}; shift 2 ;;
    *) echo "Usage: $0 --output FILE [--gpu 2] [--pid-file FILE]" >&2; exit 2 ;;
  esac
done
[[ "$GPU" =~ ^(0|2)$ ]] || { echo "Monitor GPU must be 0 or 2" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "--output is required" >&2; exit 2; }
mkdir -p "$(dirname "$OUT")"
echo 'timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total' >"$OUT"
[[ -z "$PID_FILE" ]] || { mkdir -p "$(dirname "$PID_FILE")"; echo $$ >"$PID_FILE"; }
trap '[[ -z "$PID_FILE" ]] || rm -f "$PID_FILE"' EXIT
exec nvidia-smi -i "$GPU" \
  --query-gpu=timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,utilization.memory,memory.used,clocks.sm,clocks.mem,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total \
  --format=csv,noheader,nounits -l 1 >>"$OUT"

