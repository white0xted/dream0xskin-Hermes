#!/bin/bash
set -euo pipefail

# Hermes Skin diagnostics

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="${HERMES_SKIN_PYTHON:-python3}"
PORT=9334
LIVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --live) LIVE=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "[check] app"
APP_PATH="/Applications/Hermes.app"
[[ -d "$APP_PATH" ]] || { echo "Missing $APP_PATH" >&2; exit 1; }
echo "  path: $APP_PATH"

echo "[check] python"
"$PYTHON_BIN" --version || { echo "Python not found: $PYTHON_BIN" >&2; exit 1; }
"$PYTHON_BIN" -c "import websockets" 2>/dev/null || {
  echo "websockets library not found in $PYTHON_BIN" >&2
  echo "Try: ~/.hermes/hermes-agent/venv/bin/python3" >&2
  exit 1
}
echo "  python: $("$PYTHON_BIN" --version 2>&1)"

echo "[check] selectors"
SELECTORS="$ROOT_DIR/runtime/selectors-hermes.json"
[[ -f "$SELECTORS" ]] || { echo "Missing $SELECTORS" >&2; exit 1; }
echo "  selectors: OK"

echo "[check] css"
CSS="$ROOT_DIR/runtime/hermes-skin.css"
[[ -f "$CSS" ]] || { echo "Missing $CSS" >&2; exit 1; }
echo "  css: $(wc -c < "$CSS") bytes"

echo "[check] theme"
THEMES_ROOT="$ROOT_DIR/runtime/themes-hermes"
[[ -d "$THEMES_ROOT" ]] || { echo "Missing themes dir $THEMES_ROOT" >&2; exit 1; }
COUNT=0
for THEME_DIR in "$THEMES_ROOT"/*/; do
  [[ -d "$THEME_DIR" ]] || continue
  THEME_DIR="${THEME_DIR%/}"
  THEME_JSON="$THEME_DIR/theme.json"
  [[ -f "$THEME_JSON" ]] || { echo "Missing $THEME_JSON" >&2; exit 1; }
  NAME=$(python3 -c "import json; print(json.load(open('$THEME_JSON'))['name'])" 2>/dev/null) || { echo "Invalid $THEME_JSON" >&2; exit 1; }
  echo "  theme: $NAME ($(basename "$THEME_DIR"))"
  COUNT=$((COUNT+1))
done
[[ "$COUNT" -gt 0 ]] || { echo "No themes found in $THEMES_ROOT" >&2; exit 1; }

if (( LIVE )); then
  echo "[check] live renderer"
  curl -fsS --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 || {
    echo "  CDP port $PORT not responding. Is Hermes running with --remote-debugging-port=$PORT?" >&2
    exit 1
  }
  echo "  CDP: port $PORT responding"
  "$PYTHON_BIN" "$ROOT_DIR/runtime/injector-hermes.py" --port "$PORT" --probe-only
fi

echo "Doctor completed successfully."
