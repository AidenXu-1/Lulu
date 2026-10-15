#!/usr/bin/env bash
# 显式构建 Lulu.app；只使用调用方提供的既有 Python 3.11 venv。
set -euo pipefail

cd "$(dirname "$0")/.."          # 切到 app/

BUILD_IDENTITY=""
VENV_PATH=""
BASELINE_REVISION=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --identity)
      [[ "$#" -ge 2 ]] || { echo "错误：--identity 缺少值。" >&2; exit 2; }
      BUILD_IDENTITY="$2"
      shift 2
      ;;
    --venv)
      [[ "$#" -ge 2 ]] || { echo "错误：--venv 缺少值。" >&2; exit 2; }
      VENV_PATH="$2"
      shift 2
      ;;
    --baseline)
      [[ "$#" -ge 2 && -n "$2" ]] || { echo "错误：--baseline 缺少已验收的源码版本。" >&2; exit 2; }
      BASELINE_REVISION="$2"
      shift 2
      ;;
    *)
      echo "错误：未知参数；只接受 --identity、--venv 与 --baseline。" >&2
      exit 2
      ;;
  esac
done
if [[ -z "$BUILD_IDENTITY" || -z "$VENV_PATH" ]]; then
  echo "错误：必须显式提供 --identity candidate|production 与 --venv /absolute/path。" >&2
  exit 2
fi
if [[ "$VENV_PATH" != /* || ! -d "$VENV_PATH" || -L "$VENV_PATH" ]]; then
  echo "错误：--venv 必须指向已存在且非符号链接的绝对目录。" >&2
  exit 2
fi
VENV="$VENV_PATH/bin"
if [[ ! -x "$VENV/python" ]]; then
  echo "错误：既有 venv 必须提供可执行的 bin/python；脚本不会创建或安装依赖。" >&2
  exit 2
fi
if ! "$VENV/python" -c 'import PyInstaller, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
  echo "错误：构建 venv 必须使用 python3.11 且已包含 PyInstaller；脚本不会安装依赖。" >&2
  exit 2
fi
if [[ -e "$PWD/.venv" || -L "$PWD/.venv" ]]; then
  echo "错误：app/.venv 已存在，构建脚本拒绝覆盖或复用未知对象。" >&2
  exit 2
fi
BUILD_ROOT="${LULU_BUILD_ROOT:-}"
DIST_ROOT="${LULU_DIST_ROOT:-}"
if [[ -z "$BUILD_ROOT" || -z "$DIST_ROOT" || "$BUILD_ROOT" != /* || "$DIST_ROOT" != /* ]]; then
  echo "错误：必须通过 LULU_BUILD_ROOT 与 LULU_DIST_ROOT 显式提供绝对候选roots。" >&2
  exit 2
fi

case "$BUILD_IDENTITY" in
  candidate)
    BUILD_PROFILE="external_candidate"
    APP_PREFIX="Lulu"
    ;;
  production)
    BUILD_PROFILE="external_candidate"
    APP_PREFIX="Lulu"
    ;;
  *)
    echo "错误：未知构建身份；只接受 candidate 或 production。" >&2
    exit 2
    ;;
esac
VERSION="$($VENV/python packaging/version_value.py version)"
BUILD="$($VENV/python packaging/version_value.py build)"
SOURCE_CHANNEL="$($VENV/python packaging/version_value.py channel)"
"$VENV/python" packaging/phase10_prepare_build_roots.py \
  --app-root "$PWD" \
  --build-root "$BUILD_ROOT" \
  --dist-root "$DIST_ROOT" \
  --identity "$BUILD_IDENTITY" \
  --version "$VERSION" \
  --build "$BUILD" >/dev/null
if [[ "$BUILD_IDENTITY" == "candidate" ]]; then
  if [[ "$SOURCE_CHANNEL" != "candidate" || "${LULU_UPDATE_RELEASE:-0}" != "0" || -n "${LULU_UPDATE_PUBLIC_KEY_PATH:-}" ]]; then
    echo "错误：candidate 只接受 candidate/non-release 源码，且禁止更新发布输入。" >&2
    exit 1
  fi
elif [[ "$BUILD_IDENTITY" == "production" ]]; then
  if [[ "${LULU_PRODUCTION_RELEASE_AUTHORIZED:-0}" != "1" || "$SOURCE_CHANNEL" != "production" ]]; then
    echo "错误：production 必须同时具备显式发布授权与 production 源码通道。" >&2
    exit 1
  fi
fi
# 新版完全不接受旧授权输入，已有配置保留现场并停止构建。
if [[ -n "${LULU_LICENSE_ENV:-}" || -n "${LULU_LICENSE_TRANSPORT:-}" || -n "${LULU_LICENSE_ENDPOINT:-}" || -e license_runtime_config.json || -L license_runtime_config.json ]]; then
  echo "错误：免授权新版不接受旧授权环境或 license_runtime_config.json；请先核实残留配置。" >&2
  exit 2
fi
cleanup_update_public_key=0
cleanup_build_profile=0
cleanup_release_features=0
cleanup_product_source=0
managed_venv_link_created=0
managed_venv_link_token=""
BASELINE_RECEIPT="$BUILD_ROOT/product-baseline-receipt.json"
CAPABILITY_RECEIPT="$BUILD_ROOT/product-capabilities-receipt.json"

cleanup_runtime_config() {
  if [[ "$cleanup_update_public_key" == "1" ]]; then
    rm -f update_public_key.pem
  fi
  if [[ "$cleanup_build_profile" == "1" ]]; then
    rm -f build_profile.json
  fi
  if [[ "$cleanup_release_features" == "1" ]]; then
    rm -f release_features.json
  fi
  if [[ "$cleanup_product_source" == "1" ]]; then
    rm -f product_source.json
  fi
}

cleanup_managed_venv_link() {
  if [[ "$managed_venv_link_created" != "1" ]]; then
    return 0
  fi
  if "$VENV/python" packaging/profile_gate.py remove-managed-venv-link \
    --build-venv-root "$VENV_PATH" \
    --link-token "$managed_venv_link_token"; then
    managed_venv_link_created=0
    managed_venv_link_token=""
    return 0
  fi
  echo "错误：受管 app/.venv 链接已被替换或篡改，保留现场并失败关闭。" >&2
  return 1
}

cleanup_build_state() {
  local original_status=$?
  local link_status=0
  local runtime_status=0
  trap - EXIT
  set +e
  cleanup_managed_venv_link
  link_status=$?
  cleanup_runtime_config
  runtime_status=$?
  if [[ "$original_status" -ne 0 ]]; then
    exit "$original_status"
  fi
  if [[ "$link_status" -ne 0 ]]; then
    exit "$link_status"
  fi
  exit "$runtime_status"
}
trap cleanup_build_state EXIT

rm -f build_profile.json release_features.json product_source.json
if [[ -n "$(/usr/bin/git -C .. status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "错误：候选构建只接受完全干净的 Git worktree；请先提交或移走未提交文件。" >&2
  exit 2
fi

echo "==> 0/5 校验完整产品源码基线"
if [[ -n "$BASELINE_REVISION" ]]; then
  "$VENV/python" packaging/product_baseline_gate.py \
    --repo .. --candidate HEAD --baseline "$BASELINE_REVISION" --output "$BASELINE_RECEIPT"
else
  "$VENV/python" packaging/product_baseline_gate.py \
    --repo .. --candidate HEAD --output "$BASELINE_RECEIPT"
fi

managed_venv_link_token="$("$VENV/python" packaging/profile_gate.py create-managed-venv-link \
  --build-venv-root "$VENV_PATH")"
managed_venv_link_created=1

echo "==> 1/5 校验完整产品用户能力"
LULU_TEST_PRODUCT_BASELINE="$BASELINE_REVISION" "$VENV/python" packaging/product_capability_gate.py \
  --repo .. --candidate HEAD --output "$CAPABILITY_RECEIPT"

echo "==> 2/5 校验 macOS 15 MLX 构建输入"
"$VENV/python" packaging/macos_compatibility.py verify-build-env

"$VENV/python" packaging/profile_gate.py create \
  --profile "$BUILD_PROFILE" --version "$VERSION" --build "$BUILD" \
  --build-venv-root "$VENV_PATH" \
  --output build_profile.json --product-source-output product_source.json
cleanup_build_profile=1
cleanup_product_source=1
BUILD_PROFILE_SHA256="$(shasum -a 256 build_profile.json | awk '{print $1}')"
PRODUCT_SOURCE_SHA256="$("$VENV/python" -c 'import json; print(json.load(open("build_profile.json", encoding="utf-8"))["product_source_sha256"])')"
SOURCE_COMMIT="$("$VENV/python" -c 'import json; print(json.load(open("build_profile.json", encoding="utf-8"))["source_commit"])')"
SOURCE_TREE="$("$VENV/python" -c 'import json; print(json.load(open("build_profile.json", encoding="utf-8"))["source_tree"])')"

rm -f update_public_key.pem
if [[ "$BUILD_IDENTITY" == "production" && "${LULU_UPDATE_RELEASE:-0}" == "1" ]]; then
  UPDATE_PUBLIC_KEY_PATH="${LULU_UPDATE_PUBLIC_KEY_PATH:-}"
  if [[ -z "$UPDATE_PUBLIC_KEY_PATH" || ! -f "$UPDATE_PUBLIC_KEY_PATH" || -L "$UPDATE_PUBLIC_KEY_PATH" ]]; then
    echo "错误：更新构建必须通过外部普通文件提供 LULU_UPDATE_PUBLIC_KEY_PATH。" >&2
    exit 1
  fi
  /usr/bin/openssl pkey -pubin -in "$UPDATE_PUBLIC_KEY_PATH" -text -noout | grep -q '3072 bit' || {
    echo "错误：更新公钥必须为 RSA-3072。" >&2; exit 1;
  }
  cp "$UPDATE_PUBLIC_KEY_PATH" update_public_key.pem
  chmod 600 update_public_key.pem
  cleanup_update_public_key=1
fi

echo "==> 3/5 生成图标与作者归属元数据"
"$VENV/python" packaging/make_icon.py packaging/icon_1024.png
rm -rf packaging/Lulu.iconset && mkdir -p packaging/Lulu.iconset
for sz in 16 32 128 256 512; do
  sips -z $sz $sz packaging/icon_1024.png --out "packaging/Lulu.iconset/icon_${sz}x${sz}.png" >/dev/null
  sips -z $((sz*2)) $((sz*2)) packaging/icon_1024.png --out "packaging/Lulu.iconset/icon_${sz}x${sz}@2x.png" >/dev/null
done
if ! iconutil -c icns packaging/Lulu.iconset -o packaging/AppIcon.icns; then
  echo "iconutil 生成失败,改用 stdlib fallback 生成 AppIcon.icns"
  "$VENV/python" packaging/make_icns.py packaging/Lulu.iconset packaging/AppIcon.icns
fi
BUILD_TIME_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
BUILD_GIT_COMMIT="${SOURCE_COMMIT:0:7}"
"$VENV/python" packaging/make_authorship.py \
  --version "$VERSION" \
  --build-time "$BUILD_TIME_UTC" \
  --git-commit "$BUILD_GIT_COMMIT" \
  --build-profile "$BUILD_PROFILE" \
  --build-profile-sha256 "$BUILD_PROFILE_SHA256" \
  --source-commit "$SOURCE_COMMIT" \
  --source-tree "$SOURCE_TREE" \
  --product-source-sha256 "$PRODUCT_SOURCE_SHA256" \
  --output packaging/AUTHORSHIP.json

echo "==> 4/5 PyInstaller 打包(较慢,在打包 torch)"
export LULU_BUILD_PROFILE_FOR_SPEC="$BUILD_PROFILE"
export LULU_BUILD_IDENTITY_FOR_SPEC="$BUILD_IDENTITY"
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  AGENT_XPC_BRIDGE="$BUILD_ROOT/liblulu_agent_xpc_bridge.dylib"
  MACOSX_DEPLOYMENT_TARGET=15.0 /usr/bin/xcrun clang \
    -mmacosx-version-min=15.0 -dynamiclib -fblocks -Wall -Wextra -Werror \
    native/lulu_agent_xpc_bridge.c -lbsm -o "$AGENT_XPC_BRIDGE"
  export LULU_AGENT_XPC_BRIDGE_FOR_SPEC="$AGENT_XPC_BRIDGE"
fi
"$VENV/python" -m PyInstaller packaging/Lulu.spec --noconfirm --distpath "$DIST_ROOT" --workpath "$BUILD_ROOT"
cleanup_managed_venv_link
APP_PATH="${DIST_ROOT}/${APP_PREFIX}.app"
if [[ "$BUILD_PROFILE" != "trusted_test_safe_e2e" ]]; then
  "$VENV/python" packaging/verify_workspace_package.py --app "$APP_PATH" \
    --source-app "$PWD" --output "$DIST_ROOT/workspace-package-receipt.json"
fi
# Importing MLX alone does not load its GPU kernels. Exercise the actual bundle.
"$VENV/python" packaging/verify_mlx_gpu.py --app "$APP_PATH"
# BUNDLE 已独立还原并通过包内运行；及早释放重复 COLLECT，给最后签名留出空间。
# 这个目录在构建完成时本来就会清理，候选 App 与构建诊断仍保留。
rm -rf "${DIST_ROOT}/${APP_PREFIX}"
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  mkdir -p "$APP_PATH/Contents/Library/LaunchAgents"
  cp packaging/com.lulu.transcribe.agent-core.plist \
    "$APP_PATH/Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist"
  chmod 0644 "$APP_PATH/Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist"
fi
# 随包 Skill 校验脚本由 Python 读取，无需资源目录执行位；与更新 ZIP 权限规则一致。
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  chmod 0644 "$APP_PATH/Contents/Resources/skills/lulu-agent/scripts/verify_lulu_install.py"
fi
# ZIP 工具会把 dist-info 元数据归一为普通只读文件。构建时先采用同一
# 权限口径，避免 DMG 与更新 ZIP 中的同一 App 只因元数据执行位而树不一致。
find "$APP_PATH/Contents/Resources" -type f -path '*/?*.dist-info/*' -exec chmod 0644 {} +
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  PYTHONPATH="$PWD" "$VENV/python" packaging/finalize_agent_component_manifest.py prepare-skill \
    --app "$APP_PATH" --version "$VERSION" --build "$BUILD" \
    --source-revision "$SOURCE_COMMIT"
fi
xattr -dr com.apple.quarantine "$APP_PATH" || true
/usr/bin/codesign --force --deep --sign - "$APP_PATH"
/usr/bin/codesign --verify --strict "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  PYTHONPATH="$PWD" "$VENV/python" packaging/finalize_agent_component_manifest.py finalize-manifest \
    --app "$APP_PATH" --version "$VERSION" --build "$BUILD" \
    --source-revision "$SOURCE_COMMIT"
fi
"$VENV/python" packaging/profile_gate.py verify-app \
  --profile "$BUILD_PROFILE" --app "$APP_PATH" \
  --analysis-receipt "$BUILD_ROOT/Lulu/profile-analysis.json" \
  --output "$DIST_ROOT/build-profile-receipt.json"
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  PYTHONPATH="$PWD" "$VENV/python" packaging/verify_generation9_candidate_identity.py \
    --app "$APP_PATH" --profile-receipt "$DIST_ROOT/build-profile-receipt.json" \
    --output "$DIST_ROOT/canonical-app-tree-receipt.json"
  PYTHONPATH="$PWD" "$VENV/python" packaging/verify_workspace_runtime.py \
    --app "$APP_PATH" --output "$DIST_ROOT/workspace-runtime-receipt.json"
fi
cp "$BASELINE_RECEIPT" "$DIST_ROOT/product-baseline-receipt.json"
cp "$CAPABILITY_RECEIPT" "$DIST_ROOT/product-capabilities-receipt.json"

echo "==> 5/5 最终签名与目录身份已固定"

echo "✅ 完成:${APP_PATH}"
echo "   版本:${VERSION} (${BUILD})"
echo "   profile:${BUILD_PROFILE}"
echo "   identity:${BUILD_IDENTITY}"
echo "   build root:${BUILD_ROOT}"
echo "   dist root:${DIST_ROOT}"
echo "   结构化收据:${DIST_ROOT}/build-profile-receipt.json"
if [[ "$BUILD_PROFILE" == "external_candidate" ]]; then
  echo "   App 唯一树身份收据:${DIST_ROOT}/canonical-app-tree-receipt.json"
fi
echo "   完整产品基线收据:${DIST_ROOT}/product-baseline-receipt.json"
echo "   完整产品能力收据:${DIST_ROOT}/product-capabilities-receipt.json"
echo "   模型权重不打进 App。转录模型与 VoxCPM2 会按需放到 ~/Library/Application Support/Lulu/models/。"
echo "   VoxCPM2 占用视缓存与重复文件而定,建议预留 10-15GB 可用空间。"
