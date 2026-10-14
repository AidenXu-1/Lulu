#!/usr/bin/env bash
# 构建一个本地 TTS 模型的「独立运行时包」(可重定位 PBS 解释器 + pip 依赖)。
#
# 产物 = 一个 tar.gz,用户下载解压到任意路径即用(主 App 子进程绝对路径调里面的 python)。
# 不含模型权重(权重走官方 HF snapshot_download,由 App 安装器另下)。
# 见 ADR-0015 / docs/spec/tts-runtime-package-implementation.md;配方由切片1 spike 验证。
#
# 用法: bash packaging/build_tts_runtime.sh <model_id> [out_dir]
#   model_id 需对应 packaging/tts_runtimes/<model_id>.requirements.txt
# 当前只支持在 macOS Apple Silicon 上构建 mac-arm64 包;Windows 包在 win runner 上另构。
set -euo pipefail

MODEL_ID="${1:?用法: build_tts_runtime.sh <model_id> [out_dir]}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
OUT_DIR="${2:-$APP_DIR/scratch/tts_dist}"
PYVER="3.11"
REQ="$HERE/tts_runtimes/${MODEL_ID}.requirements.txt"
[ -f "$REQ" ] || { echo "✗ 缺依赖清单: $REQ"; exit 1; }

OS="mac"
ARCH="$(uname -m)"   # arm64
[ "$(uname -s)" = "Darwin" ] || { echo "✗ 本脚本目前只在 macOS 构建 mac 包"; exit 1; }

echo "▶ 构建 ${MODEL_ID} 运行时包 (${OS}-${ARCH}, py${PYVER})"

# 1) 取可重定位 PBS 解释器(uv 托管的就是 python-build-standalone 构建),复制成自包含副本
#    注意:不能用 `uv python find`——在项目目录里它会返回 .venv(主程序环境)。
#    直接从 uv 托管目录定位独立 PBS 安装,避开 venv 污染。
command -v uv >/dev/null || { echo "✗ 需要 uv(python-build-standalone 来源)"; exit 1; }
uv python install "${PYVER}" >/dev/null 2>&1 || true
PYDIR="$(uv python dir)"
# PBS 目录的 arch 记号是 aarch64/x86_64(非 uname 的 arm64);按 macos + 带补丁版本号匹配真实安装
PBS_LINK="$(ls -d "$PYDIR"/cpython-${PYVER}.*-macos-* 2>/dev/null | sort -V | tail -1)"
[ -z "$PBS_LINK" ] && { echo "✗ 未找到 uv 托管的 PBS cpython ${PYVER}(macos)。先跑 uv python install ${PYVER}"; exit 1; }
PBS_ROOT="$(readlink -f "$PBS_LINK")"
case "$PBS_ROOT" in *.venv|*/.venv) echo "✗ 解析到了 venv,不是独立 PBS:$PBS_ROOT"; exit 1;; esac
echo "  PBS 源: $PBS_ROOT"

STAGE="$(mktemp -d)/runtime"
mkdir -p "$STAGE/python"
rsync -a "$PBS_ROOT/" "$STAGE/python/"
# 副本归我们管,去掉 uv 的「externally managed」标记,放行 pip
find "$STAGE/python" -name EXTERNALLY-MANAGED -delete
PY="$STAGE/python/bin/python${PYVER}"

# 2) 装依赖(--no-compile 减体积;torch 取平台默认 wheel)
echo "  装依赖(可能数分钟,pip 缓存命中会快)…"
CONSTRAINTS="$HERE/tts_runtimes/${MODEL_ID}.${OS}-${ARCH}.constraints.txt"
PIP_REQUIREMENT_ARGS=(-r "$REQ")
if [ -f "$CONSTRAINTS" ]; then
  PIP_REQUIREMENT_ARGS+=(-c "$CONSTRAINTS")
fi
"$PY" -m pip install --no-compile --disable-pip-version-check -q "${PIP_REQUIREMENT_ARGS[@]}"

# 2b) 可选 vendor 钩子:有些模型需带源码 + 额外离线资产。
#     钩子见 tts_runtimes/<id>.vendor.sh,环境变量 STAGE / PY 传给它。
VENDOR_HOOK="$HERE/tts_runtimes/${MODEL_ID}.vendor.sh"
if [ -f "$VENDOR_HOOK" ]; then
  echo "  跑 vendor 钩子:$(basename "$VENDOR_HOOK")"
  STAGE="$STAGE" PY="$PY" bash "$VENDOR_HOOK"
fi

# 3) 自检:能 import 关键模块(失败即不出包)。优先 tts_runtimes/<id>.selftest.py(用运行时解释器跑)。
SELFTEST="$HERE/tts_runtimes/${MODEL_ID}.selftest.py"
if [ -f "$SELFTEST" ]; then
  STAGE="$STAGE" "$PY" -I -s "$SELFTEST"
else
  "$PY" -I -s -c "import torch; print('  自检 OK · torch',torch.__version__,'· mps',torch.backends.mps.is_available())"
fi

# 4) 打包(tar 保真符号链接)+ manifest(sha256 / size)。有 vendor 则一并打包。
mkdir -p "$OUT_DIR"
VER="$(date +%Y%m%d)"
PKG="$OUT_DIR/LuluTTSRuntime-${MODEL_ID}-${OS}-${ARCH}-${VER}.tar.gz"
echo "  打包 → $PKG"
PKG_DIRS=(python)
[ -d "$STAGE/vendor" ] && PKG_DIRS+=(vendor)
COPYFILE_DISABLE=1 tar --exclude='._*' -C "$STAGE" -czf "$PKG" "${PKG_DIRS[@]}"
SHA="$(shasum -a 256 "$PKG" | awk '{print $1}')"
SIZE="$(stat -f%z "$PKG")"
cat > "$OUT_DIR/${MODEL_ID}-${OS}-${ARCH}.manifest.json" <<JSON
{
  "model_id": "${MODEL_ID}",
  "platform": "${OS}-${ARCH}",
  "python": "${PYVER}",
  "version": "${VER}",
  "file": "$(basename "$PKG")",
  "sha256": "${SHA}",
  "size": ${SIZE}
}
JSON
rm -rf "$(dirname "$STAGE")"
echo "✓ 完成: $(basename "$PKG")  ($(echo "scale=2;$SIZE/1073741824"|bc) GB)  sha256=${SHA:0:12}…"
