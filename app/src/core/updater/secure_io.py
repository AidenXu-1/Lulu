from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .models import UpdateError


def ensure_private_dir(path: Path) -> Path:
    path = path.expanduser()
    # macOS exposes /var as a system-owned compatibility symlink to /private/var.
    # Canonicalize only that fixed platform alias; all other symlink parents fail.
    if str(path).startswith("/var/"):
        path = Path("/private") / path.relative_to("/")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UpdateError("unsafe_path", "更新目录不安全")
    os.chmod(path, 0o700)
    return path


def assert_regular_nofollow(path: Path, *, mode: int | None = None) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UpdateError("unsafe_path", "更新文件类型不安全")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise UpdateError("unsafe_mode", "更新文件权限不安全")
    return info


def read_nofollow(path: Path, *, mode: int | None = None, maximum: int | None = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (mode is not None and stat.S_IMODE(st.st_mode) != mode):
            raise UpdateError("unsafe_file", "更新文件不安全")
        if maximum is not None and st.st_size > maximum:
            raise UpdateError("too_large", "更新文件过大")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read((maximum + 1) if maximum is not None else -1)
        if maximum is not None and len(data) > maximum:
            raise UpdateError("too_large", "更新文件过大")
        return data
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        assert_regular_nofollow(path)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())


def load_json(path: Path, *, allowed: set[str], required: set[str] | None = None) -> dict[str, Any]:
    assert_regular_nofollow(path, mode=0o600)
    try:
        value = json.loads(read_nofollow(path, mode=0o600, maximum=1024 * 1024).decode("utf-8"))
    except Exception as exc:
        raise UpdateError("state_corrupt", "更新状态损坏") from exc
    if not isinstance(value, dict) or set(value) - allowed or (required and not required <= set(value)):
        raise UpdateError("state_corrupt", "更新状态字段异常")
    return value
