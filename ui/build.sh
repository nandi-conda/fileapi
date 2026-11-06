#!/usr/bin/env bash
# Build the forge web UI (Qt for WebAssembly) into fileapi/static/forge/, which is committed so
# `just deploy` ships it without any build step on FastAPI Cloud.
#
# The toolchain comes from the pixi `ui` environment: qt6-wasm (Qt for WebAssembly, with the
# matching emsdk) from the nandi-testing channel on prefix.dev, plus conda-forge's qt6-main as the
# host Qt, cmake and ninja. pixi caches the packages, so they download once.
# Usage: `just ui`, or: pixi run -e ui ui/build.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/../fileapi/static/forge"

if [ -z "${QT_WASM_PATH:-}" ]; then
    exec pixi run -e ui "$0" "$@"
fi

echo "==> Building"
build="$here/.build-wasm"
qt-cmake-wasm -S "$here" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=MinSizeRel >/dev/null
cmake --build "$build"

echo "==> Installing into fileapi/static/forge"
mkdir -p "$out"
cp "$here/web/index.html" "$build/forgeui.js" "$build/qtloader.js" "$out/"
gzip -9 -n -c "$build/forgeui.wasm" > "$out/forgeui.wasm.gz"
rm -f "$out/forgeui.wasm"
ls -lh "$out"
