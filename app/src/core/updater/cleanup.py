from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
from typing import Any

from .models import UpdateSnapshot
from .secure_io import ensure_private_dir

_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_SAFE_STARTUP_UI_STATES = frozenset(
    {
        "idle",
        "up_to_date",
        "installed",
        "update_available",
        "manual_required",
        "check_error",
        "manifest_verify_error",
        "verify_error",
        "zip_structure_error",
        "preparation_error",
        "location_error",
        "install_error_rollback",
        "rollback_failed",
        "old_version_after_restart",
    }
)


def cleanup_after_install(updates_root: Path, txn: str) -> dict[str, Any]:
    """Best-effort cleanup while the successful install still owns the lock."""
    result = _new_result("installed", txn=txn)
    try:
        root, root_stat = _validated_root(updates_root)
        _clean_container(root, root_stat, "downloads", result)
        if _valid_txn(txn):
            _clean_container(root, root_stat, "staged", result, child=txn)
        else:
            _blocked(result, f"staged/{txn}", "invalid_txn")
        _finish(result, "completed")
        _append_log(root, result)
    except Exception as exc:
        _finish(result, "failed", reason=type(exc).__name__)
        _append_log_best_effort(updates_root, result)
    return result


def cleanup_stale_on_startup(
    updates_root: Path, snapshot: UpdateSnapshot
) -> dict[str, Any]:
    """Clean legacy leftovers only when the persisted state is non-resumable."""
    result = _new_result("startup", ui_state=snapshot.ui_state)
    try:
        root, root_stat = _validated_root(updates_root)
        if snapshot.ui_state not in _SAFE_STARTUP_UI_STATES:
            _finish(result, "skipped", reason="active_state")
        elif _transaction_lock_present(root, root_stat):
            _finish(result, "skipped", reason="active_or_unsafe_lock")
        else:
            _clean_container(root, root_stat, "downloads", result)
            _clean_container(root, root_stat, "staged", result)
            _finish(result, "completed")
        _append_log(root, result)
    except Exception as exc:
        _finish(result, "failed", reason=type(exc).__name__)
        _append_log_best_effort(updates_root, result)
    return result


def _new_result(category: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "category": category,
        "at": datetime.now(timezone.utc).isoformat(),
        "result": "running",
        "released_bytes": 0,
        "removed_entries": 0,
        "blocked": [],
        **details,
    }


def _finish(result: dict[str, Any], state: str, *, reason: str = "") -> None:
    result["result"] = "partial" if state == "completed" and result["blocked"] else state
    if reason:
        result["reason"] = reason


def _validated_root(path: Path) -> tuple[Path, os.stat_result]:
    root = ensure_private_dir(path)
    info = root.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise OSError("unsafe update root")
    return root, info


def _open_owned_dir(
    path: Path, *, device: int, expected_inode: int | None = None
) -> tuple[int, os.stat_result]:
    fd = os.open(path, _DIR_FLAGS)
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_dev != device
        or (expected_inode is not None and info.st_ino != expected_inode)
    ):
        os.close(fd)
        raise OSError("unsafe update directory")
    return fd, info


def _clean_container(
    root: Path,
    root_stat: os.stat_result,
    name: str,
    result: dict[str, Any],
    *,
    child: str | None = None,
) -> None:
    container_path = root / name
    try:
        container_lstat = container_path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(container_lstat.st_mode)
        or not stat.S_ISDIR(container_lstat.st_mode)
        or container_lstat.st_uid != os.getuid()
        or container_lstat.st_dev != root_stat.st_dev
    ):
        _blocked(result, name, "unsafe_container")
        return
    container_fd, _ = _open_owned_dir(
        container_path,
        device=root_stat.st_dev,
        expected_inode=container_lstat.st_ino,
    )
    try:
        if child is None:
            _clean_dir_fd(container_fd, name, root_stat.st_dev, result)
            return
        try:
            child_stat = os.stat(child, dir_fd=container_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        child_rel = f"{name}/{child}"
        if (
            stat.S_ISLNK(child_stat.st_mode)
            or not stat.S_ISDIR(child_stat.st_mode)
            or child_stat.st_uid != os.getuid()
            or child_stat.st_dev != root_stat.st_dev
        ):
            _blocked(result, child_rel, "unsafe_directory")
            return
        child_fd = os.open(child, _DIR_FLAGS, dir_fd=container_fd)
        try:
            opened = os.fstat(child_fd)
            if (
                opened.st_ino != child_stat.st_ino
                or opened.st_dev != child_stat.st_dev
                or opened.st_uid != os.getuid()
            ):
                raise OSError("directory identity changed")
            _clean_dir_fd(child_fd, child_rel, root_stat.st_dev, result)
        finally:
            os.close(child_fd)
        try:
            os.rmdir(child, dir_fd=container_fd)
            result["removed_entries"] += 1
        except OSError:
            if not any(
                item["path"] == child_rel
                or item["path"].startswith(child_rel + "/")
                for item in result["blocked"]
            ):
                _blocked(result, child_rel, "directory_not_empty")
    finally:
        os.close(container_fd)


def _clean_dir_fd(
    directory_fd: int,
    prefix: str,
    device: int,
    result: dict[str, Any],
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        relative = f"{prefix}/{name}"
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            _blocked(result, relative, type(exc).__name__)
            continue
        if info.st_uid != os.getuid() or info.st_dev != device:
            _blocked(result, relative, "identity_mismatch")
            continue
        parts = relative.split("/")
        if (stat.S_ISLNK(info.st_mode) and len(parts) >= 4 and parts[0] == "staged"
                and _valid_txn(parts[1]) and parts[2] == "Lulu.app"):
            # Unlink only the cache entry itself, including broken framework
            # links left by older cleanup. Never open or resolve its target.
            try:
                os.unlink(name, dir_fd=directory_fd)
                result["removed_entries"] += 1
            except OSError as exc:
                _blocked(result, relative, type(exc).__name__)
            continue
        if stat.S_ISREG(info.st_mode):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                _blocked(result, relative, type(exc).__name__)
                continue
            result["released_bytes"] += info.st_size
            result["removed_entries"] += 1
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child_fd = None
            try:
                child_fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                opened = os.fstat(child_fd)
                if (
                    opened.st_ino != info.st_ino
                    or opened.st_dev != info.st_dev
                    or opened.st_uid != os.getuid()
                ):
                    raise OSError("directory identity changed")
            except OSError as exc:
                if child_fd is not None:
                    os.close(child_fd)
                _blocked(result, relative, type(exc).__name__)
                continue
            try:
                _clean_dir_fd(child_fd, relative, device, result)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
                result["removed_entries"] += 1
            except OSError:
                if not any(
                    item["path"] == relative for item in result["blocked"]
                ):
                    _blocked(result, relative, "directory_not_empty")
            continue
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "special_file"
        _blocked(result, relative, kind)


def _blocked(result: dict[str, Any], path: str, reason: str) -> None:
    if len(result["blocked"]) < 100:
        result["blocked"].append({"path": path, "reason": reason})


def _transaction_lock_present(root: Path, root_stat: os.stat_result) -> bool:
    lock = root / "transactions" / "update.lock"
    try:
        info = lock.lstat()
    except FileNotFoundError:
        return False
    return True


def _valid_txn(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _append_log(root: Path, result: dict[str, Any]) -> None:
    logs = ensure_private_dir(root / "logs")
    path = logs / "cleanup.jsonl"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unsafe cleanup log")
        os.fchmod(fd, 0o600)
        payload = (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short cleanup log write")
            view = view[written:]
    finally:
        os.close(fd)


def _append_log_best_effort(root: Path, result: dict[str, Any]) -> None:
    try:
        _append_log(root, result)
    except Exception:
        pass
