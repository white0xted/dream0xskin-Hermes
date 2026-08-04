#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
TARGET_DIR="${1:-/Applications}"
TARGET_APP="$TARGET_DIR/Hermes Skin.app"

echo "[install] building launcher..."
"$ROOT_DIR/launcher/build-launcher-app.sh" "$ROOT_DIR/dist"

echo "[install] installing to $TARGET_DIR..."
mkdir -p "$TARGET_DIR"
rm -rf "$TARGET_APP"
ditto "$ROOT_DIR/dist/Hermes Skin.app" "$TARGET_APP"
codesign --force --deep --sign - "$TARGET_APP"
mdimport "$TARGET_APP" 2>/dev/null || true

echo "[install] done: $TARGET_APP"
echo "Open it once, then use the paintpalette icon in the macOS menu bar."
