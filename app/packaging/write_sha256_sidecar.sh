#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "用法：write_sha256_sidecar.sh <artifact>" >&2
  exit 2
fi

ARTIFACT="$1"
if [[ ! -f "$ARTIFACT" || -L "$ARTIFACT" ]]; then
  echo "错误：发布实物必须是普通文件。" >&2
  exit 1
fi

ARTIFACT_DIR="$(cd "$(dirname "$ARTIFACT")" && pwd -P)"
ARTIFACT_NAME="$(basename "$ARTIFACT")"
SIDECAR="${ARTIFACT_DIR}/${ARTIFACT_NAME}.sha256"
TEMP_SIDECAR="$(mktemp "${ARTIFACT_DIR}/.${ARTIFACT_NAME}.sha256.XXXXXX")"

cleanup() {
  rm -f "$TEMP_SIDECAR"
}
trap cleanup EXIT

(cd "$ARTIFACT_DIR" && shasum -a 256 "$ARTIFACT_NAME") > "$TEMP_SIDECAR"
chmod 0644 "$TEMP_SIDECAR"
mv -f "$TEMP_SIDECAR" "$SIDECAR"
(cd "$ARTIFACT_DIR" && shasum -a 256 -c "${ARTIFACT_NAME}.sha256")

