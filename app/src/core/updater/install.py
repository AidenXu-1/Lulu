from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Callable

from .archive import app_file_manifest, tree_manifest_sha, validate_staged_app
from .manifest import canonical_sha
from .models import UpdateError
from .secure_io import atomic_json, ensure_private_dir, load_json, read_nofollow
from .state import StateStore
from .constants import UPDATE_READY_TIMEOUT_SECONDS

_PLAN_FIELDS = {"schema", "txn", "nonce", "target_app", "target_parent", "staged_app", "swap_new", "backup_app", "old_pid", "old_pid_start_time", "old_executable_path", "version", "build", "staged_manifest_sha256", "tree_manifest_sha256", "target_lstat", "target_parent_lstat", "ready_path", "created_at", "plan_sha256"}
_READY_FIELDS = {"schema", "app", "txn", "nonce", "version", "build", "pid", "pid_start_time", "executable_path", "started_at"}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class RollbackBlocked(RuntimeError):
    """The launched App may still be using the replacement bundle."""


def ensure_install_space(staged: Path, destination: Path) -> None:
    """The verified download still needs a separate full App during exchange."""
    total = 0
    for current, dirs, files in os.walk(staged, followlinks=False):
        root = Path(current)
        dirs[:] = [name for name in dirs if not (root / name).is_symlink()]
        for name in files:
            info = (root / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    # Leave room for filesystem metadata, recovery receipts and normal startup.
    if shutil.disk_usage(destination).free < total + 1024 ** 3:
        raise UpdateError('install_space', '安装空间不足，请先腾出空间再重试；原程序和资料已保留')


def _identity(path: Path) -> dict:
    st = path.lstat()
    return {"type": "dir" if stat.S_ISDIR(st.st_mode) else "file", "uid": st.st_uid, "device": st.st_dev, "inode": st.st_ino, "mode": stat.S_IMODE(st.st_mode)}


def _same_identity(path: Path, expected: dict) -> bool:
    try:
        return _identity(path) == expected
    except FileNotFoundError:
        return False


def _identity_from_stat(st: os.stat_result) -> dict:
    return {"type": "dir" if stat.S_ISDIR(st.st_mode) else "file", "uid": st.st_uid, "device": st.st_dev, "inode": st.st_ino, "mode": stat.S_IMODE(st.st_mode)}


def _open_bound_parent(path: Path, expected: dict) -> int:
    fd = os.open(path, _DIR_FLAGS)
    if _identity_from_stat(os.fstat(fd)) != expected or not _same_identity(path, expected):
        os.close(fd)
        raise UpdateError("parent_changed", "应用程序目录发生变化")
    return fd


def _assert_bound_parent(fd: int, path: Path, expected: dict) -> None:
    if _identity_from_stat(os.fstat(fd)) != expected or not _same_identity(path, expected):
        raise UpdateError("parent_changed", "应用程序目录发生变化")


def _open_child_dir(parent_fd: int, name: str, *, create: bool = False) -> int:
    if "/" in name or name in ("", ".", ".."):
        raise UpdateError("txn_path", "更新事务路径异常")
    if create:
        try: os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc: raise UpdateError("occupied", "更新事务路径被占用") from exc
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    st = os.fstat(fd); parent_st = os.fstat(parent_fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_dev != parent_st.st_dev:
        os.close(fd); raise UpdateError("txn_path", "更新事务目录不安全")
    return fd


def _copy_app_to_dirfd(source: Path, parent_fd: int, name: str) -> str:
    source_fd = os.open(source, _DIR_FLAGS)
    destination_fd = _open_child_dir(parent_fd, name, create=True)
    records: list[dict] = []
    try:
        _copy_tree_contents(source_fd, destination_fd, "", records)
    except Exception:
        os.close(destination_fd); os.close(source_fd)
        raise
    os.close(destination_fd); os.close(source_fd)
    return tree_manifest_sha(records)


def _copy_tree_contents(source_fd: int, destination_fd: int, prefix: str, records: list[dict]) -> None:
    links: list[tuple[str, str, int]] = []
    for name in sorted(os.listdir(source_fd)):
        if name in ("", ".", "..") or "/" in name:
            raise UpdateError("copy_path", "安装副本路径异常")
        st = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        rel = f"{prefix}/{name}" if prefix else name
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(name, dir_fd=source_fd)
            links.append((name, target, mode))
            continue
        if stat.S_ISDIR(st.st_mode):
            os.mkdir(name, mode, dir_fd=destination_fd)
            source_child = os.open(name, _DIR_FLAGS, dir_fd=source_fd)
            destination_child = os.open(name, _DIR_FLAGS, dir_fd=destination_fd)
            try: _copy_tree_contents(source_child, destination_child, rel, records)
            finally: os.close(source_child); os.close(destination_child)
            records.append({"path": rel, "type": "dir", "mode": mode})
            continue
        if not stat.S_ISREG(st.st_mode):
            raise UpdateError("copy_special", "安装副本包含特殊文件")
        source_file = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=source_fd)
        destination_file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=destination_fd)
        digest = hashlib.sha256(); size = 0
        try:
            while True:
                block = os.read(source_file, 1024 * 1024)
                if not block: break
                digest.update(block); size += len(block)
                view = memoryview(block)
                while view:
                    written = os.write(destination_file, view)
                    if written <= 0: raise OSError("short write")
                    view = view[written:]
            os.fchmod(destination_file, mode)
            os.fsync(destination_file)
        finally:
            os.close(source_file); os.close(destination_file)
        records.append({"path": rel, "type": "file", "mode": mode, "size": size, "sha256": digest.hexdigest()})
    for name, target, mode in links:
        rel = f"{prefix}/{name}" if prefix else name
        before = os.fstat(destination_fd)
        os.symlink(target, name, dir_fd=destination_fd)
        st = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
        copied_target = os.readlink(name, dir_fd=destination_fd)
        after = os.fstat(destination_fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISLNK(st.st_mode) or stat.S_IMODE(st.st_mode) != mode or copied_target != target:
            raise UpdateError("copy_link", "安装副本链接发生变化")
        records.append({"path": rel, "type": "symlink", "mode": mode, "target": target})
    os.fsync(destination_fd)


def _exists_at(parent_fd: int, name: str) -> bool:
    try: os.stat(name, dir_fd=parent_fd, follow_symlinks=False); return True
    except FileNotFoundError: return False


def _exchange_apps(left_fd: int, right_fd: int) -> None:
    """macOS atomic exchange: a complete App always occupies the launch path."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    exchange = libc.renameatx_np
    exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    exchange.restype = ctypes.c_int
    if exchange(left_fd, b"Lulu.app", right_fd, b"Lulu.app", 0x00000002) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    os.fsync(left_fd)
    os.fsync(right_fd)


def _remove_tree_at(parent_fd: int, name: str, *, expected: dict | None = None) -> None:
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UpdateError("cleanup_path", "更新清理路径不安全")
    child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    identity = _identity_from_stat(st)
    try:
        if _identity_from_stat(os.fstat(child_fd)) != identity or (expected is not None and identity != expected):
            raise UpdateError('cleanup_changed', '更新残留目录身份已变化，已保留')
        _remove_dir_contents(child_fd)
    finally: os.close(child_fd)
    if _identity_from_stat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != identity:
        raise UpdateError('cleanup_changed', '更新残留目录身份已变化，已保留')
    os.rmdir(name, dir_fd=parent_fd)


def _remove_dir_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        st = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            os.unlink(name, dir_fd=directory_fd)
        elif stat.S_ISDIR(st.st_mode):
            _remove_tree_at(directory_fd, name, expected=_identity_from_stat(st))
        elif stat.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise UpdateError("cleanup_special", "更新清理路径包含特殊文件")


def _write_json_exclusive_at(parent_fd: int, name: str, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0: raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_target(target: Path, *, current_uid: int | None = None, allowed_targets: set[Path] | None = None) -> None:
    target = target.expanduser().resolve(strict=False)
    uid = os.getuid() if current_uid is None else current_uid
    allowed = {item.expanduser().resolve(strict=False) for item in (allowed_targets or {Path("/Applications/Lulu.app"), Path.home() / "Applications/Lulu.app"})}
    if target not in allowed:
        raise UpdateError("location", "当前安装位置无法自动更新")
    parent = target.parent
    st = parent.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_mode & stat.S_IWOTH:
        raise UpdateError("location", "应用程序目录不安全")
    if parent == Path("/Applications"):
        if st.st_uid not in (0, uid):
            raise UpdateError("location", "应用程序目录所有者异常")
    elif st.st_uid != uid or st.st_mode & stat.S_IWGRP:
        raise UpdateError("location", "用户应用程序目录权限异常")
    if not os.access(parent, os.W_OK):
        raise UpdateError("location", "没有权限替换当前 Lulu")
    try:
        if os.statvfs(parent).f_flag & getattr(os, "ST_RDONLY", 1):
            raise UpdateError("location", "应用程序目录位于只读卷")
        fs = __import__("subprocess").run(["/usr/bin/stat", "-f", "%T", str(parent)], capture_output=True, text=True, timeout=3, check=False)
        if fs.returncode != 0 or fs.stdout.strip().lower() in {"nfs", "smbfs", "webdav", "afpfs", "autofs"}:
            raise UpdateError("location", "应用程序目录不在本地可写卷")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("location", "无法确认应用程序卷安全") from exc
    target_st = target.lstat()
    if stat.S_ISLNK(target_st.st_mode) or not stat.S_ISDIR(target_st.st_mode):
        raise UpdateError("location", "当前 Lulu 路径不安全")


class InstallTransaction:
    def __init__(self, updates_root: Path, *, allowed_targets: set[Path] | None = None, verify_system: bool = True):
        self.root = ensure_private_dir(updates_root)
        self.transactions = ensure_private_dir(self.root / "transactions")
        self.lock = self.transactions / "update.lock"
        self.allowed_targets = {item.expanduser().resolve(strict=False) for item in allowed_targets} if allowed_targets is not None else None
        self.verify_system = verify_system

    def acquire(self, txn: str) -> None:
        if not len(txn) == 64 or any(c not in "0123456789abcdef" for c in txn):
            raise UpdateError("txn", "更新事务异常")
        transactions_fd = os.open(self.transactions, _DIR_FLAGS)
        temporary = f'.update-lock-{txn}-{secrets.token_hex(8)}'
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=transactions_fd)
            with os.fdopen(fd, 'w') as handle:
                handle.write(txn + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            # Publish a complete lock atomically, with no overwrite of an owner.
            os.link(temporary, 'update.lock', src_dir_fd=transactions_fd,
                    dst_dir_fd=transactions_fd, follow_symlinks=False)
            os.fsync(transactions_fd)
        except FileExistsError as exc:
            raise UpdateError('locked', '已有更新事务正在进行') from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=transactions_fd)
            except FileNotFoundError:
                pass
            os.close(transactions_fd)

    def owns(self, txn: str) -> bool:
        try:
            return read_nofollow(self.lock, mode=0o600, maximum=65).decode('ascii').strip() == txn
        except FileNotFoundError:
            return False

    def release(self, *, expected_txn: str | None = None) -> None:
        if expected_txn is not None and not self.owns(expected_txn):
            raise UpdateError('locked', '更新事务已变化，保留当前程序')
        transactions_fd = None
        try:
            transactions_fd = os.open(self.transactions, _DIR_FLAGS)
            st = os.stat("update.lock", dir_fd=transactions_fd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                os.unlink("update.lock", dir_fd=transactions_fd)
        except FileNotFoundError:
            pass
        finally:
            if transactions_fd is not None: os.close(transactions_fd)

    def create_plan(self, *, target: Path, staged_app: Path, version: str, build: str, old_pid: int, old_pid_start_time: str, old_executable_path: str, staged_sha: str, launch_protocol: bool = False) -> tuple[Path, dict]:
        target = target.expanduser().resolve(strict=True)
        staged_app = staged_app.expanduser().resolve(strict=True)
        validate_target(target, allowed_targets=self.allowed_targets)
        txn = staged_app.parent.name
        expected_staged = self.root / "staged" / txn / "Lulu.app"
        if staged_app != expected_staged or len(txn) != 64 or any(c not in "0123456789abcdef" for c in txn):
            raise UpdateError("staged_path", "安装准备路径异常")
        nonce = secrets.token_hex(32)
        # Publish the durable plan before the lock that makes startup recover it.
        # No helper can run until create_plan returns; a failed write must leave
        # the currently running App usable and retryable.
        txn_root = self.transactions / txn
        transactions_fd = os.open(self.transactions, _DIR_FLAGS)
        txn_fd = None
        created_identity = None
        try:
            if os.path.lexists(self.lock):
                raise UpdateError('locked', '已有更新事务正在进行')
            txn_fd = _open_child_dir(transactions_fd, txn, create=True)
            created_identity = _identity_from_stat(os.fstat(txn_fd))
            parent = target.parent
            swap_root = parent / f".Lulu.update-new.{txn}"
            backup_root = parent / f".Lulu.update-backup.{txn}"
            if os.path.lexists(swap_root) or os.path.lexists(backup_root):
                raise UpdateError("occupied", "更新事务路径被占用")
            plan = {
                "schema": 1, "txn": txn, "nonce": nonce,
                "target_app": str(target), "target_parent": str(parent), "staged_app": str(staged_app),
                "swap_new": str(swap_root / "Lulu.app"), "backup_app": str(backup_root / "Lulu.app"),
                "old_pid": old_pid, "old_pid_start_time": old_pid_start_time, "old_executable_path": old_executable_path,
                "version": version, "build": build, "staged_manifest_sha256": staged_sha, "tree_manifest_sha256": staged_sha,
                "target_lstat": _identity(target), "target_parent_lstat": _identity(parent),
                "ready_path": str(txn_root / "ready.json"), "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if launch_protocol:
                plan['launch_protocol'] = 1
            plan["plan_sha256"] = canonical_sha(plan)
            path = txn_root / "install_plan.json"
            _write_json_exclusive_at(txn_fd, "install_plan.json", plan)
            os.fsync(txn_fd)
            os.fsync(transactions_fd)
            self.acquire(txn)
            return path, plan
        except Exception:
            # Never delete a pre-existing transaction or somebody else's lock.
            if created_identity is not None and not self.owns(txn):
                _remove_tree_at(transactions_fd, txn, expected=created_identity)
            raise
        finally:
            if txn_fd is not None:
                os.close(txn_fd)
            os.close(transactions_fd)

    def load_plan(self, path: Path, *, validate_location: bool = True) -> dict:
        plan = load_json(path, allowed=_PLAN_FIELDS | {'launch_protocol'}, required=_PLAN_FIELDS)
        if 'launch_protocol' in plan and (type(plan['launch_protocol']) is not int or plan['launch_protocol'] != 1):
            raise UpdateError('plan', '安装接管协议无法识别')
        expected = dict(plan)
        claimed = expected.pop("plan_sha256")
        if claimed != canonical_sha(expected) or plan.get("schema") != 1:
            raise UpdateError("plan", "更新计划无法验证")
        txn = plan.get("txn", "")
        nonce = plan.get("nonce", "")
        if not isinstance(txn, str) or len(txn) != 64 or any(c not in "0123456789abcdef" for c in txn) or not isinstance(nonce,str) or len(nonce)!=64 or any(c not in "0123456789abcdef" for c in nonce) or path != self.transactions / txn / "install_plan.json":
            raise UpdateError("plan", "更新计划路径异常")
        if type(plan.get("old_pid")) is not int or plan["old_pid"]<=0 or any(not isinstance(plan.get(key),str) for key in ("target_app","target_parent","staged_app","swap_new","backup_app","old_pid_start_time","old_executable_path","version","build","staged_manifest_sha256","tree_manifest_sha256","ready_path","created_at","plan_sha256")) or plan["tree_manifest_sha256"] != plan["staged_manifest_sha256"]:
            raise UpdateError("plan", "更新计划类型异常")
        for identity in (plan.get("target_lstat"),plan.get("target_parent_lstat")):
            if not isinstance(identity,dict) or set(identity)!={"type","uid","device","inode","mode"} or identity.get("type") not in {"dir","file"} or any(type(identity.get(key)) is not int for key in ("uid","device","inode","mode")):
                raise UpdateError("plan", "更新计划身份异常")
        target = Path(plan["target_app"])
        if target.parent != Path(plan["target_parent"]):
            raise UpdateError("plan", "更新计划路径异常")
        expected_swap = target.parent / f".Lulu.update-new.{txn}" / "Lulu.app"
        expected_backup = target.parent / f".Lulu.update-backup.{txn}" / "Lulu.app"
        expected_staged = self.root / "staged" / txn / "Lulu.app"
        if Path(plan["staged_app"]) != expected_staged or Path(plan["swap_new"]) != expected_swap or Path(plan["backup_app"]) != expected_backup or Path(plan["ready_path"]) != self.transactions / txn / "ready.json":
            raise UpdateError("plan", "更新计划路径异常")
        if validate_location:
            validate_target(target, allowed_targets=self.allowed_targets)
        else:
            allowed = {Path(os.path.abspath(item.expanduser())) for item in (self.allowed_targets or {Path('/Applications/Lulu.app'), Path.home() / 'Applications/Lulu.app'})}
            if Path(os.path.abspath(target)) not in allowed:
                raise UpdateError("plan", "更新计划路径异常")
        return plan

    def execute_local(self, plan_path: Path, *, launch: Callable[[Path, dict], None], wait_ready: Callable[[Path, dict, int], bool], old_pid_validator: Callable[[int, str, str], bool], crash_at: str = "", on_installed: Callable[[dict], None] | None = None) -> str:
        plan = self.load_plan(plan_path)
        target = Path(plan["target_app"]); parent = target.parent
        staged = Path(plan["staged_app"]); swap = Path(plan["swap_new"]); backup = Path(plan["backup_app"])
        swap_root_name = swap.parent.name; backup_root_name = backup.parent.name
        if Path(plan["ready_path"]).exists() or Path(plan["ready_path"]).is_symlink():
            raise UpdateError("ready_replay", "更新确认文件已被预置")
        if not old_pid_validator(plan["old_pid"], plan["old_pid_start_time"], plan["old_executable_path"]):
            raise UpdateError("pid_reused", "旧进程身份发生变化")
        if not _same_identity(target, plan["target_lstat"]) or not _same_identity(parent, plan["target_parent_lstat"]):
            raise UpdateError("target_changed", "安装目标发生变化")
        if validate_staged_app(staged, version=plan["version"], build=plan["build"], verify_system=self.verify_system) != plan["tree_manifest_sha256"]:
            raise UpdateError("staged_changed", "安装包发生变化")
        parent_fd = _open_bound_parent(parent, plan["target_parent_lstat"])
        swap_root_fd = backup_root_fd = None
        try:
            _assert_bound_parent(parent_fd, parent, plan["target_parent_lstat"])
            swap_root_fd = _open_child_dir(parent_fd, swap_root_name, create=True)
            artifact_roots = {swap_root_name: _identity_from_stat(os.fstat(swap_root_fd))}
            self._record_artifact_roots(plan_path, plan, artifact_roots)
            copied_sha = _copy_app_to_dirfd(staged, swap_root_fd, "Lulu.app")
            _assert_bound_parent(parent_fd, parent, plan["target_parent_lstat"])
            if copied_sha != plan["tree_manifest_sha256"] or validate_staged_app(swap, version=plan["version"], build=plan["build"], verify_system=self.verify_system) != plan["tree_manifest_sha256"]:
                raise UpdateError("copy_changed", "安装副本发生变化")
            if crash_at == "after_copy": return "crashed"
            backup_root_fd = _open_child_dir(parent_fd, backup_root_name, create=True)
            artifact_roots[backup_root_name] = _identity_from_stat(os.fstat(backup_root_fd))
            self._record_artifact_roots(plan_path, plan, artifact_roots)
            _assert_bound_parent(parent_fd, parent, plan["target_parent_lstat"])
            target_now = os.stat("Lulu.app", dir_fd=parent_fd, follow_symlinks=False)
            if _identity_from_stat(target_now) != plan["target_lstat"]:
                raise UpdateError("target_changed", "安装目标发生变化")
            _exchange_apps(parent_fd, swap_root_fd)
            if crash_at == "after_exchange": return "crashed"
            os.rename("Lulu.app", "Lulu.app", src_dir_fd=swap_root_fd, dst_dir_fd=backup_root_fd)
            os.fsync(backup_root_fd)
            os.fsync(swap_root_fd)
            if crash_at == "after_backup": return "crashed"
            if crash_at == "after_install": return "crashed"
            try:
                expected_child = launch(target, plan)
                self._expected_child = expected_child if isinstance(expected_child, tuple) and len(expected_child) == 3 else None
                self._ready_validation_context = True
                ready = bool(wait_ready(Path(plan["ready_path"]), plan, UPDATE_READY_TIMEOUT_SECONDS))
            except RollbackBlocked:
                raise
            except Exception:
                ready = False
            finally:
                self._ready_validation_context = False
            if ready:
                atomic_json(plan_path.parent / 'result.json', {'plan_sha256': plan['plan_sha256'], 'result': 'installed'})
                if crash_at == 'after_confirmation': return 'crashed'
                StateStore(self.root).record_result('installed', txn=plan['txn'])
                cleaned = self._cleanup_artifacts(parent_fd, plan_path, plan)
                try:
                    self._consume_ready(Path(plan["ready_path"]))
                except (OSError, UpdateError) as error:
                    cleaned = False
                    self._record_cleanup_failure(plan, error)
                if on_installed is not None:
                    try:
                        on_installed(plan)
                    except Exception:
                        pass
                self.release(expected_txn=plan['txn'])
                # Keep the plan/receipt until the lock is gone: if the helper
                # dies earlier, normal startup still has a verifiable outcome.
                if cleaned:
                    try:
                        self._cleanup_transaction_dir(plan_path.parent)
                    except (OSError, UpdateError) as error:
                        self._record_cleanup_failure(plan, error)
                return "installed"
            self._consume_ready(Path(plan["ready_path"]))
            return self._rollback_installed_target(parent_fd, parent, plan, swap_root_fd, backup_root_fd, swap_root_name, backup_root_name)
        finally:
            if swap_root_fd is not None: os.close(swap_root_fd)
            if backup_root_fd is not None: os.close(backup_root_fd)
            os.close(parent_fd)

    def _rollback_installed_target(self, parent_fd: int, parent: Path, plan: dict, swap_root_fd: int, backup_root_fd: int, swap_root_name: str, backup_root_name: str) -> str:
        try:
            _assert_bound_parent(parent_fd, parent, plan["target_parent_lstat"])
            if not _same_identity(Path(plan['backup_app']), plan['target_lstat']):
                raise UpdateError("rollback_backup", "回滚备份不存在")
            if _exists_at(parent_fd, "Lulu.app"):
                self._verify_replacement(plan)
                _exchange_apps(parent_fd, backup_root_fd)
            else:
                os.rename("Lulu.app", "Lulu.app", src_dir_fd=backup_root_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            StateStore(self.root).record_result('rolled_back', txn=plan['txn'])
            self._cleanup_artifacts(parent_fd, self.transactions / plan['txn'] / 'install_plan.json', plan)
            self.release(expected_txn=plan['txn'])
            return "rolled_back"
        except Exception:
            # The old App or its durable result may still need recovery.
            return "failed"

    def _verify_replacement(self, plan):
        target = Path(plan['target_app'])
        if target.is_symlink() or app_file_manifest(target)[1] != plan['tree_manifest_sha256']:
            raise UpdateError('target_changed', '当前应用程序已变化，原程序和备份均已保留')

    def _record_cleanup_failure(self, plan, error):
        from .cache_cleanup import record_cleanup_failure
        try:
            record_cleanup_failure(self.root, plan, error)
        except (OSError, UpdateError):
            pass

    def _record_artifact_roots(self, path, plan, roots):
        atomic_json(path.parent / 'artifact-roots.json', {'plan_sha256': plan['plan_sha256'], 'roots': roots})

    def _cleanup_artifacts(self, parent_fd, path, plan):
        """Only remove roots created by this helper and still containing its App.

        Old helpers have no ownership receipt. Their verified App can be restored,
        but leftover directories are retained instead of guessed to be disposable.
        """
        names = [Path(plan[key]).parent.name for key in ('backup_app', 'swap_new')]
        names = [name for name in names if _exists_at(parent_fd, name)]
        if not names:
            return True
        try:
            receipt = load_json(path.parent / 'artifact-roots.json', allowed={'plan_sha256', 'roots'}, required={'plan_sha256', 'roots'})
            if receipt['plan_sha256'] != plan['plan_sha256'] or type(receipt['roots']) is not dict:
                raise UpdateError('cleanup_identity', '更新残留归属无法确认，已保留')
            for name in names:
                expected = receipt['roots'].get(name)
                root = Path(plan['target_parent']) / name
                if type(expected) is not dict or not _same_identity(root, expected):
                    raise UpdateError('cleanup_identity', '更新残留目录已变化，已保留')
                child_fd = _open_child_dir(parent_fd, name)
                try:
                    if _identity_from_stat(os.fstat(child_fd)) != expected or set(os.listdir(child_fd)) - {'Lulu.app'}:
                        raise UpdateError('cleanup_identity', '更新残留包含未登记文件，已保留')
                    app = root / 'Lulu.app'
                    if _exists_at(child_fd, 'Lulu.app') and not _same_identity(app, plan['target_lstat']):
                        if app.is_symlink() or app_file_manifest(app)[1] != plan['tree_manifest_sha256']:
                            raise UpdateError('cleanup_identity', '更新残留程序已经变化，已保留')
                finally:
                    os.close(child_fd)
                _remove_tree_at(parent_fd, name, expected=expected)
            return True
        except (OSError, UpdateError) as error:
            self._record_cleanup_failure(plan, error)
            return False

    def validate_ready(self, path: Path, plan: dict, *, pid_validator: Callable[[int, str, str], bool], expected_child: tuple[int, str, str] | None = None) -> bool:
        try:
            ready = load_json(path, allowed=_READY_FIELDS, required=_READY_FIELDS)
        except UpdateError:
            return False
        if ready.get("schema") != 1 or ready.get("app") != "Lulu":
            return False
        if type(ready.get("pid")) is not int or ready["pid"]<=0 or any(not isinstance(ready.get(key),str) for key in ("txn","nonce","version","build","pid_start_time","executable_path","started_at")):
            return False
        for key in ("txn", "nonce", "version", "build"):
            if ready.get(key) != plan.get(key):
                return False
        executable = str(ready.get("executable_path", ""))
        expected_root = str(Path(plan["target_app"]) / "Contents" / "MacOS") + os.sep
        if not executable.startswith(expected_root):
            return False
        expected = expected_child or getattr(self, "_expected_child", None)
        if expected is not None:
            expected_pid, expected_started, expected_executable = expected
            try:
                exact = int(ready["pid"]) == int(expected_pid) and ready["pid_start_time"] == expected_started and Path(executable).resolve() == Path(expected_executable).resolve()
            except Exception:
                return False
            if not exact:
                return False
        elif not bool(getattr(self, "_ready_validation_context", False)):
            return False
        try:
            return pid_validator(int(ready["pid"]), str(ready["pid_start_time"]), executable)
        except Exception:
            return False

    def _consume_ready(self, path: Path) -> None:
        try:
            txn = path.parent.name
            transactions_fd = os.open(self.transactions, _DIR_FLAGS)
            try:
                txn_fd = os.open(txn, _DIR_FLAGS, dir_fd=transactions_fd)
                try:
                    st = os.stat("ready.json", dir_fd=txn_fd, follow_symlinks=False)
                    if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                        raise UpdateError("ready_type", "更新确认文件类型异常")
                    os.unlink("ready.json", dir_fd=txn_fd)
                finally: os.close(txn_fd)
            finally: os.close(transactions_fd)
        except FileNotFoundError:
            pass

    def _cleanup_transaction_dir(self, txn_root: Path) -> None:
        transactions_fd = os.open(self.transactions, _DIR_FLAGS)
        try:
            _remove_tree_at(transactions_fd, txn_root.name)
        finally:
            os.close(transactions_fd)

    def recover(self, plan_path: Path) -> str:
        try:
            plan = self.load_plan(plan_path, validate_location=False)
        except UpdateError:
            return "failed_state_corrupt"
        target = Path(plan["target_app"]); parent = target.parent; swap = Path(plan["swap_new"]); backup = Path(plan["backup_app"])
        try:
            parent_fd = _open_bound_parent(parent, plan["target_parent_lstat"])
        except Exception:
            return "failed_security_rejected"
        swap_fd = backup_fd = None
        try:
            if _exists_at(parent_fd, swap.parent.name): swap_fd = _open_child_dir(parent_fd, swap.parent.name)
            if _exists_at(parent_fd, backup.parent.name): backup_fd = _open_child_dir(parent_fd, backup.parent.name)
            target_exists = _exists_at(parent_fd, "Lulu.app")
            result = 'rolled_back'
            committed = plan_path.parent / 'result.json'
            if committed.exists():
                receipt = load_json(committed, allowed={'plan_sha256', 'result'}, required={'plan_sha256', 'result'})
                if receipt != {'plan_sha256': plan['plan_sha256'], 'result': 'installed'}:
                    raise UpdateError('result', '更新完成记录无法验证')
                self._verify_replacement(plan)
                result = 'installed'
            elif not _same_identity(target, plan['target_lstat']):
                # An interruption immediately after exchange leaves the old App
                # in swap_new; legacy plans may instead have a missing target.
                old_fd = (backup_fd if backup_fd is not None and _same_identity(backup, plan['target_lstat'])
                          else swap_fd if swap_fd is not None and _same_identity(swap, plan['target_lstat']) else None)
                if old_fd is None:
                    raise UpdateError('rollback_backup', '没有可验证的旧程序，保留现场供手动恢复')
                if target_exists:
                    self._verify_replacement(plan)
                    _exchange_apps(parent_fd, old_fd)
                else:
                    os.rename('Lulu.app', 'Lulu.app', src_dir_fd=old_fd, dst_dir_fd=parent_fd)
                    os.fsync(parent_fd)
            StateStore(self.root).record_result(result, txn=plan['txn'])
            self._cleanup_artifacts(parent_fd, plan_path, plan)
            try:
                self._consume_ready(Path(plan['ready_path']))
            except (OSError, UpdateError) as error:
                self._record_cleanup_failure(plan, error)
            self.release(expected_txn=plan['txn'])
            return result
        except UpdateError:
            return "failed_security_rejected"
        except OSError:
            return "failed"
        finally:
            if swap_fd is not None: os.close(swap_fd)
            if backup_fd is not None: os.close(backup_fd)
            os.close(parent_fd)


def write_ready(path: Path, plan: dict, *, pid: int, pid_start_time: str, executable_path: str) -> None:
    parent_fd = os.open(path.parent, _DIR_FLAGS)
    try:
        _write_json_exclusive_at(parent_fd, path.name, {"schema": 1, "app": "Lulu", "txn": plan["txn"], "nonce": plan["nonce"], "version": plan["version"], "build": plan["build"], "pid": pid, "pid_start_time": pid_start_time, "executable_path": executable_path, "started_at": datetime.now(timezone.utc).isoformat()})
    except FileExistsError as exc:
        raise UpdateError("ready_exists", "更新确认文件已存在") from exc
    finally:
        os.close(parent_fd)
