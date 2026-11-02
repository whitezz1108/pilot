#!/usr/bin/env bash
set -euo pipefail
NAME=""; OUT=""
while (($#)); do
  case "$1" in
    --container) NAME=${2:?}; shift 2 ;;
    --output-dir) OUT=${2:?}; shift 2 ;;
    *) echo "Usage: $0 --container NAME [--output-dir DIR]" >&2; exit 2 ;;
  esac
done
[[ -n "$NAME" ]] || { echo "--container is required" >&2; exit 2; }
LABEL=$(docker inspect --format '{{ index .Config.Labels "pilot01.native-benchmark" }}' "$NAME")
[[ "$LABEL" == "true" ]] || { echo "Refusing to stop an unlabelled/non-benchmark container: $NAME" >&2; exit 1; }
if [[ -n "$OUT" ]]; then
  mkdir -p "$OUT"
  docker logs "$NAME" >"$OUT/stdout.log" 2>"$OUT/stderr.log" || true
  docker inspect "$NAME" >"$OUT/docker-inspect-final.json"
fi
docker stop "$NAME"
echo "Stopped but did not remove: $NAME"

