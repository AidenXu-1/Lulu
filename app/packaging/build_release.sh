#!/usr/bin/env bash
# 生成可分发 DMG。无证书时生成测试版；设置签名变量后生成公证正式版。
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$#" -ne 1 || "$1" != "external_candidate" ]]; then
  echo "错误：对外发布链只接受显式 external_candidate；internal_test 无条件拒绝。" >&2
  exit 2
fi

VERSION="$(.venv/bin/python packaging/version_value.py version)"
BUILD="$(.venv/bin/python packaging/version_value.py build)"
CHANNEL="$(.venv/bin/python packaging/version_value.py profile-channel external_candidate)"
ARCH="arm64"
if [[ "$CHANNEL" != "production" ]]; then
  echo "错误：对外候选必须封存为 production 通道。" >&2
  exit 1
fi
APP_PATH="dist/external_candidate/Lulu.app"
RELEASE_DIR="release"
DMG_NAME="Lulu-${VERSION}-${BUILD}-${ARCH}.dmg"
DMG_PATH="${RELEASE_DIR}/${DMG_NAME}"
STAGING_DIR="${RELEASE_DIR}/dmg-root"
SIGN_IDENTITY="${LULU_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${LULU_NOTARY_PROFILE:-}"

echo "==> 1/6 构建 Lulu.app"
bash packaging/build_app.sh external_candidate

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "==> 2/6 使用 Developer ID 签名"
  codesign --deep --force --options runtime --timestamp \
    --sign "$SIGN_IDENTITY" "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "==> 2/6 未配置 Developer ID，保留测试分发版"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

echo "==> 3/6 准备安装镜像"
rm -rf "$STAGING_DIR" "$DMG_PATH" "${DMG_PATH}.sha256"
mkdir -p "$STAGING_DIR" "$RELEASE_DIR"
ditto "$APP_PATH" "$STAGING_DIR/Lulu.app"
ln -s /Applications "$STAGING_DIR/应用程序"
.venv/bin/python packaging/make_install_instructions.py \
  --output "$STAGING_DIR/安装说明.txt" \
  --version "$VERSION" --build "$BUILD" --dmg-name "$DMG_NAME"

echo "==> 4/6 生成 DMG"
hdiutil create \
  -volname "Lulu ${VERSION}" \
  -srcfolder "$STAGING_DIR" \
  -ov -format UDZO \
  "$DMG_PATH"
rm -rf "$STAGING_DIR"

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "==> 5/6 签名 DMG"
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG_PATH"

  if [[ -n "$NOTARY_PROFILE" ]]; then
    echo "==> 5/6 提交 Apple 公证并装订凭据"
    xcrun notarytool submit "$DMG_PATH" \
      --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
  else
    echo "错误：正式分发需同时设置 LULU_NOTARY_PROFILE。" >&2
    exit 1
  fi
else
  echo "==> 5/6 跳过公证（测试版首次需右键打开）"
fi

echo "==> 6/6 生成 SHA-256"
bash packaging/write_sha256_sidecar.sh "$DMG_PATH"

echo "✅ 发布包已生成：$DMG_PATH"
echo "   校验文件：${DMG_PATH}.sha256"
if [[ -z "$SIGN_IDENTITY" ]]; then
  echo "   当前为未公证测试版，请把 DMG 内的安装说明一并告知收件人。"
fi

if [[ "${LULU_UPDATE_RELEASE:-0}" == "1" ]]; then
  UPDATE_REPOSITORY="${LULU_UPDATE_REPOSITORY:-}"
  if [[ -z "$UPDATE_REPOSITORY" ]]; then
    echo "错误：生成更新资产必须显式设置 LULU_UPDATE_REPOSITORY。" >&2
    exit 1
  fi
  echo "==> 生成应用内更新 ZIP、清单和离线签名"
  .venv/bin/python packaging/make_update_release.py \
    --app "$APP_PATH" --dmg "$DMG_PATH" --output "$RELEASE_DIR" \
    --private-key "${LULU_UPDATE_PRIVATE_KEY_PATH:-}" \
    --notes "${LULU_UPDATE_RELEASE_NOTES_PATH:-}" \
    --repository "$UPDATE_REPOSITORY"
fi
