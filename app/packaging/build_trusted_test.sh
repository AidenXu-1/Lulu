#!/usr/bin/env bash
# Build and sign the R16 Trusted Test App, then close post-build authority.
set -euo pipefail

WRAPPER_INPUT="$0"
if [[ "$WRAPPER_INPUT" != /* ]]; then
  WRAPPER_INPUT="$(/bin/pwd -P)/$WRAPPER_INPUT"
fi
RAW_COMPONENT="$WRAPPER_INPUT"
while [[ "$RAW_COMPONENT" != "/" ]]; do
  if [[ -L "$RAW_COMPONENT" ]]; then
    echo "错误：可信内测 wrapper 及其祖先不能是符号链接。" >&2
    exit 1
  fi
  RAW_COMPONENT="${RAW_COMPONENT%/*}"
  if [[ -z "$RAW_COMPONENT" ]]; then
    RAW_COMPONENT="/"
  fi
done
WRAPPER_PARENT="${WRAPPER_INPUT%/*}"
WRAPPER_NAME="${WRAPPER_INPUT##*/}"
if [[ -z "$WRAPPER_PARENT" ]]; then
  WRAPPER_PARENT="/"
fi
WRAPPER_DIR="$(builtin cd -P -- "$WRAPPER_PARENT" && /bin/pwd -P)"
WRAPPER_PATH="$WRAPPER_DIR/$WRAPPER_NAME"
APP_ROOT="$(builtin cd -P -- "$WRAPPER_DIR/.." && /bin/pwd -P)"
if [[ "$WRAPPER_PATH" != "$APP_ROOT/packaging/build_trusted_test.sh" ||
      ! -f "$WRAPPER_PATH" || -L "$WRAPPER_PATH" ]]; then
  echo "错误：无法从受管 wrapper 的物理位置固定 App root。" >&2
  exit 1
fi
builtin cd -- "$APP_ROOT"

VALIDATE_ONLY=0
VENV_PATH=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --validate-only)
      VALIDATE_ONLY=1
      shift
      ;;
    --venv)
      [[ "$#" -ge 2 ]] || { echo "错误：--venv 缺少值。" >&2; exit 2; }
      VENV_PATH="$2"
      shift 2
      ;;
    *)
      echo "用法：bash packaging/build_trusted_test.sh [--validate-only] [--venv /absolute/path]" >&2
      exit 2
      ;;
  esac
done
if [[ "$VALIDATE_ONLY" == "1" ]]; then
  PYTHON_BIN="$(builtin command -v python3)"
else
  if [[ "$VENV_PATH" != /* || ! -d "$VENV_PATH" || -L "$VENV_PATH" || ! -x "$VENV_PATH/bin/python" ]]; then
    echo "错误：S6 必须显式提供既有、非符号链接的绝对 Python 3.11 venv。" >&2
    exit 2
  fi
  PYTHON_BIN="$VENV_PATH/bin/python"
fi
VERSION="$("$PYTHON_BIN" packaging/version_value.py version)"
BUILD="$("$PYTHON_BIN" packaging/version_value.py build)"
CHANNEL="$("$PYTHON_BIN" packaging/version_value.py profile-channel trusted_test)"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ||
      ! "$BUILD" =~ ^[0-9]{8}\.[0-9]+$ ||
      "$CHANNEL" != "trusted-test" ]]; then
  echo "错误：可信内测构建身份不匹配。" >&2
  exit 1
fi
if [[ -n "${LULU_LICENSE_ENV:-${LULU_LICENSE_TRANSPORT:-}}" ||
      -n "${LULU_LICENSE_ENDPOINT:-}" ||
      "${LULU_UPDATE_RELEASE:-0}" != "0" ||
      -n "${LULU_UPDATE_PUBLIC_KEY_PATH:-}" ]]; then
  echo "错误：可信内测脚本不接受授权服务或更新发布配置。" >&2
  exit 1
fi
AUTHORITY_ROOT="release/post-build-authority/Lulu-${VERSION}-${BUILD}-${CHANNEL}-arm64"
ROOT_LEAF="phase10-trusted-test-${BUILD}"
BUILD_ROOT="$PWD/build/$ROOT_LEAF"
DIST_ROOT="$PWD/dist/$ROOT_LEAF"
APP_PATH="$DIST_ROOT/Lulu-Trusted-Test.app"
CANDIDATE_EVIDENCE_PATH="$DIST_ROOT/candidate-evidence.json"

if [[ "$VALIDATE_ONLY" == "1" ]]; then
  builtin printf 'profile=trusted_test\nversion=%s\nbuild=%s\nchannel=%s\napp=%s\ncandidate_evidence=%s\nauthority_root=%s\nbuild_root=%s\ndist_root=%s\nrequired_venv=absolute-python311-with-existing-dependencies\n' \
    "$VERSION" "$BUILD" "$CHANNEL" "$APP_PATH" "$CANDIDATE_EVIDENCE_PATH" "$AUTHORITY_ROOT" "$BUILD_ROOT" "$DIST_ROOT"
  exit 0
fi

if [[ -e "$AUTHORITY_ROOT" || -L "$AUTHORITY_ROOT" || -e "$BUILD_ROOT" || -L "$BUILD_ROOT" || -e "$DIST_ROOT" || -L "$DIST_ROOT" ]]; then
  echo "错误：R16 build/dist/authority 任一目标已存在；为避免覆盖，构建已停止。" >&2
  exit 1
fi

LULU_BUILD_ROOT="$BUILD_ROOT" LULU_DIST_ROOT="$DIST_ROOT" \
  /bin/bash packaging/build_app.sh --identity trusted-test --venv "$VENV_PATH"

[[ -d "$APP_PATH" && ! -L "$APP_PATH" ]]
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
SIGNATURE_INFO="$(/usr/bin/codesign -d --verbose=4 "$APP_PATH" 2>&1)"
if ! /usr/bin/grep -q '^Signature=adhoc$' <<<"$SIGNATURE_INFO"; then
  echo "错误：可信内测 App 必须保持本机 ad-hoc 签名。" >&2
  exit 1
fi

"$PYTHON_BIN" packaging/phase10_finalize_candidate.py \
  --bundle "$APP_PATH" \
  --bundle-role dist \
  --output "$CANDIDATE_EVIDENCE_PATH"

APP_ROOT_HEX="$(builtin printf '%s' "$APP_ROOT" | /usr/bin/xxd -p -c 4096)"
{
  builtin printf "APP_ROOT_HEX = '%s'\n" "$APP_ROOT_HEX"
  /bin/cat <<'PY'
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys


_PRIVATE_MODULE_NAME = "_lulu_phase10_post_build_authority_fixed"


def _path_lstat(path: Path, *, kind: str, owner_required: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"authority source path unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"authority source path is symlink: {path}")
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"authority source path is not directory: {path}")
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"authority source path is not regular file: {path}")
    if owner_required and metadata.st_uid != os.getuid():
        raise RuntimeError(f"authority source path has wrong owner: {path}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(f"authority source path is group/world writable: {path}")
    return metadata


app_root = Path(os.fsdecode(bytes.fromhex(APP_ROOT_HEX)))
if not app_root.is_absolute() or Path(os.getcwd()) != app_root:
    raise RuntimeError("authority loader cwd does not match physical App root")
packaging_root = app_root / "packaging"
wrapper_path = packaging_root / "build_trusted_test.sh"
authority_source = packaging_root / "phase10_post_build_authority.py"

_path_lstat(app_root, kind="directory", owner_required=True)
_path_lstat(packaging_root, kind="directory", owner_required=True)
_path_lstat(wrapper_path, kind="file", owner_required=True)
_path_lstat(authority_source, kind="file", owner_required=True)
ancestor = app_root.parent
while ancestor != ancestor.parent:
    _path_lstat(ancestor, kind="directory", owner_required=False)
    ancestor = ancestor.parent
_path_lstat(ancestor, kind="directory", owner_required=False)

if _PRIVATE_MODULE_NAME in sys.modules:
    raise RuntimeError("fixed authority private module already exists")
sys.path[:] = [entry for entry in sys.path if entry not in ("", str(app_root))]
sys.path.insert(0, str(app_root))
loader = importlib.machinery.SourceFileLoader(
    _PRIVATE_MODULE_NAME,
    str(authority_source),
)
spec = importlib.util.spec_from_file_location(
    _PRIVATE_MODULE_NAME,
    str(authority_source),
    loader=loader,
)
if spec is None or spec.loader is not loader:
    raise RuntimeError("fixed authority spec creation failed")
module = importlib.util.module_from_spec(spec)
sys.modules[_PRIVATE_MODULE_NAME] = module
try:
    loader.exec_module(module)
except BaseException:
    if sys.modules.get(_PRIVATE_MODULE_NAME) is module:
        del sys.modules[_PRIVATE_MODULE_NAME]
    raise
if (
    module.__file__ != str(authority_source)
    or module.__spec__ is None
    or module.__spec__.origin != str(authority_source)
    or not (
        type(module.__loader__) is importlib.machinery.SourceFileLoader
    )
):
    raise RuntimeError("fixed authority module origin or loader mismatch")

build = module.issue_build_completed_receipt()
carrier = module.produce_carrier(build)
security_app = module.review_carrier_security(carrier)
test_app = module.review_carrier_test(carrier)
artifact = module.finalize_authority_artifact(carrier, security_app, test_app)
security_artifact = module.review_authority_artifact_security(artifact)
test_artifact = module.review_authority_artifact_test(artifact)
authority = module.issue_post_build_authority(
    artifact,
    security_artifact,
    test_artifact,
)
print(json.dumps(module.post_build_public_fingerprint(authority), sort_keys=True))
PY
} | "$PYTHON_BIN" -E -s -S -

echo "✅ 本机可信内测 App：$APP_PATH"
echo "✅ R16 post-build authority：$AUTHORITY_ROOT"
echo "未制作 DMG、未安装、未写 Keychain、未启动、未上传。"
