"""Frozen internal-test entrypoint with a build-sealed identity."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sys


_EXPECTED_PROFILE = "internal_test"


def _assert_packaged_profile() -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("内部测试入口只能由冻结构建启动")
    path = Path(getattr(sys, "_MEIPASS", "")) / "build_profile.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("构建身份缺失或损坏") from exc
    if payload.get("schema") != 1 or payload.get("profile") != _EXPECTED_PROFILE:
        raise RuntimeError("构建身份与内部测试入口不一致")


def main() -> int:
    _assert_packaged_profile()
    from src.internal_main import main as internal_main

    return internal_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
