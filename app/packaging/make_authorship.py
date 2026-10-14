#!/usr/bin/env python3
"""Generate Lulu package authorship metadata.

This file is intentionally small and stdlib-only: it writes a forensic
metadata file into the app bundle without affecting runtime behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


_SOURCE_IDENTITY_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-time", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--build-profile", required=True)
    parser.add_argument("--build-profile-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--product-source-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not _SOURCE_IDENTITY_RE.fullmatch(args.source_commit):
        parser.error("--source-commit 必须是40位小写十六进制Git提交")
    if not _SOURCE_IDENTITY_RE.fullmatch(args.source_tree):
        parser.error("--source-tree 必须是40位小写十六进制Git tree")
    if not re.fullmatch(r"[0-9a-f]{64}", args.product_source_sha256):
        parser.error("--product-source-sha256 必须是64位小写十六进制SHA-256")

    payload = {
        "project": "Lulu",
        "package_version": args.version,
        "author": "兆基",
        "copyright": "Copyright © 2026 兆基. Lulu.",
        "build_time_utc": args.build_time,
        "build_git_commit": args.git_commit,
        "build_profile": args.build_profile,
        "build_profile_sha256": args.build_profile_sha256,
        "source_commit": args.source_commit,
        "source_tree": args.source_tree,
        "product_source_sha256": args.product_source_sha256,
        "purpose": "作者归属元数据/取证水印",
        "boundary": "不构成 DRM、加密、反逆向或法律登记。",
        "privacy_note": "本声明不包含凭据、用户目录、钥匙串或用户数据。",
    }

    output = Path(args.output)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
