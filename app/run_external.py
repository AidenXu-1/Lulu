"""Frozen external-candidate entrypoint.

This file deliberately contains no diagnostic or license-bypass dispatch.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sys


_EXPECTED_PROFILE = "external_candidate"


def _assert_packaged_profile() -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("对外候选入口只能由冻结构建启动")
    path = Path(getattr(sys, "_MEIPASS", "")) / "build_profile.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("构建身份缺失或损坏") from exc
    if payload.get("schema") != 1 or payload.get("profile") != _EXPECTED_PROFILE:
        raise RuntimeError("构建身份与对外候选入口不一致")


def main() -> int:
    _assert_packaged_profile()
    if len(sys.argv) == 4 and sys.argv[1:3] == ["--install-lulu", "--source-volume"]:
        from src.core.agent_entry.installer_ui import run_installer
        return run_installer(Path(sys.argv[3]))
    from src.main import main as gui_main

    return gui_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
