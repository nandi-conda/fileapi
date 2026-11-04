#!/usr/bin/env bash
# Build the forge web UI (Qt for WebAssembly) into fileapi/static/forge/, which is committed so
# `just deploy` ships it without any build step on FastAPI Cloud.
#
# Downloads its own toolchain into ui/.toolchain/ (gitignored, ~2 GB): Qt (host + wasm) via aqtinstall,
# cmake and ninja from PyPI, and the emsdk version that Qt release was built with.
# Usage: ui/build.sh   (or `just ui`). PYTHON picks the interpreter for the tool venv.
set -euo pipefail

QT_VERSION="${QT_VERSION:-6.8.3}"
EMSDK_VERSION="${EMSDK_VERSION:-3.1.56}"   # must match the Qt release: https://doc.qt.io/qt-6/wasm.html
here="$(cd "$(dirname "$0")" && pwd)"
tc="$here/.toolchain"
out="$here/../fileapi/static/forge"
python="${PYTHON:-python3}"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) host_os=linux; host_arch=linux_gcc_64; host_dir=gcc_64 ;;
    Linux-aarch64) host_os=linux_arm64; host_arch=linux_gcc_arm64; host_dir=gcc_arm64 ;;
    Darwin-*) host_os=mac; host_arch=clang_64; host_dir=macos ;;
    *) echo "Unsupported build host $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$tc"
if [ ! -x "$tc/venv/bin/aqt" ]; then
    echo "==> Tool venv (aqtinstall, cmake, ninja)"
    "$python" -m venv "$tc/venv"
    "$tc/venv/bin/pip" install -q aqtinstall cmake ninja
fi
export PATH="$tc/venv/bin:$PATH"

qt="$tc/qt/$QT_VERSION"
if [ ! -d "$qt/$host_dir" ]; then
    echo "==> Qt $QT_VERSION host tools"
    aqt install-qt "$host_os" desktop "$QT_VERSION" "$host_arch" -O "$tc/qt"
fi
if [ ! -d "$qt/wasm_singlethread" ]; then
    echo "==> Qt $QT_VERSION for WebAssembly"
    aqt install-qt all_os wasm "$QT_VERSION" wasm_singlethread -O "$tc/qt"
fi

if [ ! -d "$tc/emsdk" ]; then
    echo "==> emsdk"
    git clone -q --depth 1 https://github.com/emscripten-core/emsdk "$tc/emsdk"
fi
if [ ! -f "$tc/emsdk/.installed-$EMSDK_VERSION" ]; then
    echo "==> Emscripten $EMSDK_VERSION"
    "$tc/emsdk/emsdk" install "$EMSDK_VERSION"
    touch "$tc/emsdk/.installed-$EMSDK_VERSION"
fi
"$tc/emsdk/emsdk" activate "$EMSDK_VERSION" >/dev/null
# shellcheck disable=SC1091
source "$tc/emsdk/emsdk_env.sh" >/dev/null 2>&1

echo "==> Building"
build="$tc/build"
sh "$qt/wasm_singlethread/bin/qt-cmake" -S "$here" -B "$build" -G Ninja \
    -DCMAKE_BUILD_TYPE=MinSizeRel -DQT_HOST_PATH="$qt/$host_dir" >/dev/null
cmake --build "$build"

echo "==> Installing into fileapi/static/forge"
mkdir -p "$out"
cp "$here/web/index.html" "$build/forgeui.js" "$build/qtloader.js" "$out/"
gzip -9 -n -c "$build/forgeui.wasm" > "$out/forgeui.wasm.gz"
rm -f "$out/forgeui.wasm"
ls -lh "$out"
