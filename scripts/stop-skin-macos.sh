#!/bin/bash
set -Eeuo pipefail

# Stop Hermes skin injection and clean up

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
# Prefer the Hermes venv python (has websockets) — system python3 3.9 lacks it
if [[ -x "$HOME/.hermes/hermes-agent/venv/bin/python3" ]]; then
  PYTHON_BIN="${HERMES_SKIN_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python3}"
else
  PYTHON_BIN="${HERMES_SKIN_PYTHON:-python3}"
fi
PORT=9334

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Kill recorded injector — launcher writes the pid to its support dir;
# fall back to the legacy in-bundle path for older installs.
# SIGTERM lets the watch loop run its cleanup (detach persistent script
# + remove DOM); wait up to 3s before escalating to SIGKILL.
for PID_FILE in \
  "$HOME/Library/Application Support/HermesSkinLauncher/state/injector.pid" \
  "$ROOT_DIR/state/injector.pid"; do
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      kill -TERM "$PID" 2>/dev/null || true
      for _ in $(seq 1 30); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.1
      done
      kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
done

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
