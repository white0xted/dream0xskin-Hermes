#!/bin/bash
set -Eeuo pipefail

# Stop Hermes skin injection and clean up

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="${HERMES_SKIN_PYTHON:-python3}"
PORT=9334

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Kill recorded injector
PID_FILE="$ROOT_DIR/state/injector.pid"
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
    sleep 0.5
    kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# Kill any orphaned injector processes
pkill -f "injector-hermes.py.*--port $PORT" 2>/dev/null || true

# Use the injector's --remove mode to clean up the renderer via CDP
INJECTOR="$ROOT_DIR/runtime/injector-hermes.py"
if [[ -f "$INJECTOR" ]]; then
  if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    "$PYTHON_BIN" "$INJECTOR" --port "$PORT" --remove 2>/dev/null || true
  fi
fi

echo "Hermes Skin stopped."
