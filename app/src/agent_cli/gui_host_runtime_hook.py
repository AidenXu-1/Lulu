"""PyInstaller pre-entry hook for the same-bundle GUI approval child."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import stat
import sys


def _is_exact_bundle_gui_executable() -> bool:
    if not bool(getattr(sys, "frozen", False)):
        return False
    executable = Path(str(getattr(sys, "executable", "")))
    if not executable.is_absolute():
        return False
    try:
        executable_metadata = executable.lstat()
        contents = executable.parent.parent
        info_path = contents / "Info.plist"
        info_metadata = info_path.lstat()
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except Exception:  # noqa: BLE001 - fail closed before UI/user data
        return False
    return bool(
        executable.parent.name == "MacOS"
        and contents.name == "Contents"
        and contents.parent.suffix == ".app"
        and stat.S_ISREG(executable_metadata.st_mode)
        and not executable.is_symlink()
        and executable_metadata.st_uid == os.getuid()
        and not executable_metadata.st_mode & 0o022
        and stat.S_ISREG(info_metadata.st_mode)
        and not info_path.is_symlink()
        and info_metadata.st_uid == os.getuid()
        and not info_metadata.st_mode & 0o022
        and info.get("CFBundleExecutable") == executable.name
    )


def _dispatch_if_requested() -> None:
    from src.core.cli_runtime.link_host_bridge import HOST_MODE_ARGUMENT

    if len(sys.argv) != 2 or sys.argv[1] != HOST_MODE_ARGUMENT:
        return
    if not _is_exact_bundle_gui_executable():
        os._exit(2)
    from src.ui.cli_url_approval import run_link_approval_host

    os._exit(run_link_approval_host())


_dispatch_if_requested()
