"""Narrow, recoverable cleanup for artifacts from a confirmed App update."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import stat

from .models import UpdateError
from .secure_io import atomic_json, ensure_private_dir

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


def _assert_owned_safe(info: os.stat_result, *, directory: bool) -> None:
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not expected
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise UpdateError("cleanup_unsafe", "更新缓存中存在不安全文件，已停止清理")


def _open_owned_dir(path: Path) -> int:
    before = path.lstat()
    _assert_owned_safe(before, directory=True)
    fd = os.open(path, _DIR_FLAGS)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise UpdateError("cleanup_changed", "更新缓存目录已变化，已停止清理")
    _assert_owned_safe(after, directory=True)
    return fd


def _remove_entry(parent_fd: int, name: str, *, staged_parts: tuple[str, ...] = ()) -> int:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        if (len(staged_parts) >= 3 and staged_parts[1] == "Lulu.app"
                and info.st_uid == os.getuid() and info.st_dev == os.fstat(parent_fd).st_dev):
            # A bundle contains real framework links. Remove the entry without
            # following it; transaction/App root links remain forbidden.
            os.unlink(name, dir_fd=parent_fd)
            return 1
        raise UpdateError("cleanup_unsafe", "更新缓存中存在链接文件，已停止清理")
    if stat.S_ISREG(info.st_mode):
        _assert_owned_safe(info, directory=False)
        os.unlink(name, dir_fd=parent_fd)
        return 1
    if not stat.S_ISDIR(info.st_mode):
        raise UpdateError("cleanup_unsafe", "更新缓存中存在特殊文件，已停止清理")
    _assert_owned_safe(info, directory=True)
    child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    removed = 0
    try:
        current = os.fstat(child_fd)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise UpdateError("cleanup_changed", "更新缓存目录已变化，已停止清理")
        _assert_owned_safe(current, directory=True)
        for child in os.listdir(child_fd):
            removed += _remove_entry(child_fd, child,
                                     staged_parts=(*staged_parts, child) if staged_parts else ())
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
    return removed + 1


def _clear_contents(path: Path) -> int:
    if not os.path.lexists(path):
        return 0
    fd = _open_owned_dir(path)
    removed = 0
    try:
        for name in os.listdir(fd):
            removed += _remove_entry(fd, name)
    finally:
        os.close(fd)
    return removed


def _remove_tree(path: Path) -> int:
    if not os.path.lexists(path):
        return 0
    parent_fd = _open_owned_dir(path.parent)
    try:
        return _remove_entry(parent_fd, path.name, staged_parts=(path.name,))
    finally:
        os.close(parent_fd)


def cleanup_successful_update(updates_root: Path, plan: dict) -> dict:
    """Remove only downloads and this transaction's staging after confirmed success."""
    txn = plan.get("txn")
    if not isinstance(txn, str) or len(txn) != 64 or any(c not in "0123456789abcdef" for c in txn):
        raise UpdateError("cleanup_txn", "更新缓存清理事务异常")
    root = ensure_private_dir(updates_root)
    downloads = ensure_private_dir(root / "downloads")
    downloads_removed = _clear_contents(downloads)
    staged_parent = ensure_private_dir(root / "staged")
    staged_removed = _remove_tree(staged_parent / txn)
    receipt = {
        "schema": 1,
        "txn": txn,
        "cleaned_at": datetime.now(timezone.utc).isoformat(),
        "downloads_removed": downloads_removed,
        "staged_removed": staged_removed,
    }
    log = ensure_private_dir(root / "logs") / (
        f"cleanup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}.json"
    )
    atomic_json(log, receipt)
    return receipt


def record_cleanup_failure(updates_root: Path, plan: dict, error: BaseException) -> dict:
    """Best-effort local receipt; never serializes paths or raw exception text."""
    txn = plan.get("txn")
    if not isinstance(txn, str) or len(txn) != 64 or any(c not in "0123456789abcdef" for c in txn):
        raise UpdateError("cleanup_txn", "更新缓存清理事务异常")
    root = ensure_private_dir(updates_root)
    receipt = {
        "schema": 1,
        "txn": txn,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "error_code": error.code if isinstance(error, UpdateError) else type(error).__name__,
    }
    log = ensure_private_dir(root / "logs") / (
        f"cleanup-failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}.json"
    )
    atomic_json(log, receipt)
    return receipt
