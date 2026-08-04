#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
OUTPUT_DIR="${1:-$ROOT_DIR/dist}"
APP_DIR="$OUTPUT_DIR/Hermes Skin.app"
APP_NAME="hermes-skin"

cd "$SCRIPT_DIR"
export CLANG_MODULE_CACHE_PATH="${TMPDIR:-/tmp}/hermes-skin-clang-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="${TMPDIR:-/tmp}/hermes-skin-swift-cache"

echo "[build] swift build..."
swift build --disable-sandbox -c release --product "$APP_NAME" --scratch-path .build

echo "[build] assembling .app bundle..."
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/Engine"

cp "$SCRIPT_DIR/.build/release/$APP_NAME" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$SCRIPT_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"

# App icon
if [ -f "$SCRIPT_DIR/Assets/hermes-skin.icns" ]; then
    cp "$SCRIPT_DIR/Assets/hermes-skin.icns" "$APP_DIR/Contents/Resources/hermes-skin.icns"
fi

ditto "$ROOT_DIR/runtime" "$APP_DIR/Contents/Resources/Engine/runtime"
ditto "$ROOT_DIR/scripts" "$APP_DIR/Contents/Resources/Engine/scripts"
chmod 755 "$APP_DIR/Contents/MacOS/$APP_NAME" "$APP_DIR/Contents/Resources/Engine/scripts/"*.sh

echo "[build] codesign..."
codesign --force --deep --sign - "$APP_DIR"

echo "[build] done: $APP_DIR"
