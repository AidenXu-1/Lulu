#!/bin/zsh
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env node "$SCRIPT_DIR/local-admin/launch.mjs"
