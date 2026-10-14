"""Fail-closed filesystem bootstrap used only by the internal-test entry."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat


_BASE = Path("/private/tmp/lulu-internal-test")


def _assert_owned_private_dir(path: Path) -> None:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError("内部测试目录不是安全普通目录")
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError("内部测试目录所有者或权限不安全")


def _mkdir_private_at(parent_fd: int, name: str) -> None:
    if not name or name in {".", ".."} or "/" in name:
        raise RuntimeError("内部测试目录名称无效")
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError("内部测试目录所有者或权限不安全")
    finally:
        os.close(fd)


def lock_internal_root() -> Path:
    """Create one isolated run root before importing any product-state module."""
    private_tmp = Path("/private/tmp")
    tmp_st = private_tmp.lstat()
    if stat.S_ISLNK(tmp_st.st_mode) or not stat.S_ISDIR(tmp_st.st_mode):
        raise RuntimeError("系统临时目录不安全")

    tmp_fd = os.open(private_tmp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        _mkdir_private_at(tmp_fd, _BASE.name)
    finally:
        os.close(tmp_fd)
    _assert_owned_private_dir(_BASE)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    base_fd = os.open(_BASE, flags)
    root_fd = -1
    try:
        run_id = secrets.token_hex(16)
        _mkdir_private_at(base_fd, run_id)
        root_fd = os.open(run_id, flags, dir_fd=base_fd)
        root_identity = os.fstat(root_fd)
        for child in ("home", "tmp", "state", "library", "voices", "tts", "logs", "output"):
            _mkdir_private_at(root_fd, child)
        # A same-UID process may rename the freshly created root and replace its
        # path while children are being made. The held dirfd stays on the old
        # inode, so compare the namespace entry again before exposing paths.
        namespace_identity = os.stat(run_id, dir_fd=base_fd, follow_symlinks=False)
        if (namespace_identity.st_dev, namespace_identity.st_ino) != (root_identity.st_dev, root_identity.st_ino):
            raise RuntimeError("内部测试目录在初始化期间被替换")
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(base_fd)
    root = _BASE / run_id
    _assert_owned_private_dir(root)
    for child in ("home", "tmp", "state", "library", "voices", "tts", "logs", "output"):
        _assert_owned_private_dir(root / child)

    # Overwrite inherited values. They select paths only; they cannot select the build profile.
    os.environ["HOME"] = str(root / "home")
    os.environ["TMPDIR"] = str(root / "tmp")
    os.environ["XDG_CONFIG_HOME"] = str(root / "state" / "config")
    os.environ["XDG_DATA_HOME"] = str(root / "state" / "data")
    os.environ["XDG_CACHE_HOME"] = str(root / "state" / "cache")
    return root
