"""三平台采集 profile 的受管路径、独占与安全清除合同。

调用方必须显式传入 app-data 根。模块不会读取 Lulu 的真实默认目录，也不会
扫描或导入日常浏览器 profile。P1B 只开放抖音和小红书命名空间；B站保持
无登录 profile 的能力边界。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, NoReturn
from uuid import uuid4

_PLATFORMS = frozenset({"douyin", "xiaohongshu", "bilibili"})
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_PROFILE_ENABLED = frozenset({"douyin", "xiaohongshu"})


class ProfileError(RuntimeError):
    """失败关闭的 profile 错误，code 供上层稳定处理。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProfileResolution:
    platform: str
    profile_id: str
    path: Path
    state: str  # managed | legacy_alias | missing


@dataclass(frozen=True)
class BrowserProfilePaths:
    """一个平台浏览器身份及其易失运行目录的完整受管路径。"""

    resolution: ProfileResolution
    user_data_dir: Path
    cache_dir: Path
    temp_dir: Path
    download_temp_dir: Path


@dataclass(frozen=True)
class ClearResult:
    success: bool
    code: str
    tombstone_name: str = ""


@dataclass(frozen=True)
class XhsProcessLeaseReceipt:
    """只供 XHS 监督进程使用的 lease 恢复凭据。"""

    run_id: str
    profile_token: str
    port_token: str
    port: int
    owner: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "platform": "xiaohongshu",
            "profile_id": "default",
            "profile_token": self.profile_token,
            "port_token": self.port_token,
            "port": self.port,
            "owner": dict(self.owner),
        }


@dataclass(frozen=True)
class XhsLeaseRecoveryResult:
    success: bool
    code: str


@dataclass(frozen=True)
class XhsBrowserProfile:
    """小红书常驻浏览器唯一允许使用的专用身份。"""

    resolution: ProfileResolution
    identity_digest: str


@dataclass(frozen=True)
class XhsBrowserReceipt:
    """跨 Lulu 重启保留的脱敏浏览器线索，不是接管信任源。"""

    profile_identity_digest: str
    chrome_pid: int
    chrome_birth: str
    launched_at_ns: int
    verified_at_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "xhs_persistent_browser",
            "platform": "xiaohongshu",
            "profile_id": "default",
            "profile_identity_digest": self.profile_identity_digest,
            "chrome_pid": self.chrome_pid,
            "chrome_birth": self.chrome_birth,
            "launched_at_ns": self.launched_at_ns,
            "verified_at_ns": self.verified_at_ns,
        }


class XhsProcessLeases:
    """操作进程持有的两把锁；释放时保留恢复收据给监督进程验尸。"""

    def __init__(
        self,
        profile_lease: ProfileLease,
        port_lease: DebugPortLease,
        receipt: XhsProcessLeaseReceipt,
        receipt_path: Path,
    ) -> None:
        self.profile_lease = profile_lease
        self.port_lease = port_lease
        self.receipt = receipt
        self.receipt_path = receipt_path

    def release_locks(self) -> None:
        self.port_lease.release()
        self.profile_lease.release()


class ProfileLease:
    """进程内外共用的最小独占凭据；只删除自己创建且 token 一致的锁。"""

    def __init__(self, lock_path: Path, token: str) -> None:
        self.lock_path = lock_path
        self.token = token
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._released = True
            return
        if payload.get("token") == self.token:
            self.lock_path.unlink(missing_ok=True)
        self._released = True

    def __enter__(self) -> ProfileLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class DebugPortLease(ProfileLease):
    """浏览器调试端口的跨平台独占凭据。"""

    def __init__(self, lock_path: Path, token: str, port: int) -> None:
        super().__init__(lock_path, token)
        self.port = port


class XhsControllerLease(ProfileLease):
    """当前 Lulu 实例对常驻小红书浏览器的可持久释放控制锁。"""

    def release(self) -> None:
        if self._released:
            return
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._released = True
            return
        if payload.get("token") == self.token:
            self.lock_path.unlink(missing_ok=True)
            ProfileRegistry._fsync_directory(self.lock_path.parent)
        self._released = True


class ProfileRegistry:
    """只解析 registry 自己派生出的路径，不接受任意删除路径。"""

    def __init__(self, app_data_dir: Path | str) -> None:
        self.app_data = Path(app_data_dir)
        if not self.app_data.is_absolute():
            raise ProfileError("unsafe_app_data", "app-data 必须是显式绝对路径。")
        if self.app_data == Path(self.app_data.anchor):
            raise ProfileError("unsafe_app_data", "文件系统根目录不能作为 app-data。")
        self._reject_symlink(self.app_data)
        if self.app_data.exists() and not self.app_data.is_dir():
            raise ProfileError("unsafe_app_data", "app-data 必须是目录。")
        self.managed_root = self.app_data / "collection_profiles"

    @staticmethod
    def _fail(code: str, message: str) -> NoReturn:
        raise ProfileError(code, message)

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ProfileError("symlink_rejected", "受管 profile 路径不能是符号链接。")

    def _validate_parts(self, platform: str, profile_id: str) -> tuple[str, str]:
        platform = str(platform or "").strip().lower()
        profile_id = str(profile_id or "").strip()
        if platform not in _PLATFORMS:
            self._fail("unsupported_platform", "未知 profile 平台。")
        if not _PROFILE_ID.fullmatch(profile_id):
            self._fail("invalid_profile_id", "profile ID 只能包含小写字母、数字、_ 和 -。")
        return platform, profile_id

    def _assert_descendant(self, path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError:
            self._fail("path_escape", "profile 路径越出受管目录。")

    def _assert_no_symlink(self, root: Path, target: Path) -> None:
        self._assert_descendant(target, root)
        current = root
        self._reject_symlink(current)
        for part in target.relative_to(root).parts:
            current = current / part
            self._reject_symlink(current)

    @staticmethod
    def _require_directory(path: Path) -> None:
        if path.exists() and not path.is_dir():
            raise ProfileError("invalid_profile_path", "profile 路径存在但不是目录。")

    def _managed_path(self, platform: str, profile_id: str) -> Path:
        return self.managed_root / platform / profile_id

    def _legacy_path(self, platform: str, profile_id: str) -> Path | None:
        if (platform, profile_id) == ("douyin", "default"):
            return self.app_data / "douyin_profile"
        return None

    def resolve(
        self,
        platform: str,
        profile_id: str = "default",
        *,
        create: bool = False,
    ) -> ProfileResolution:
        platform, profile_id = self._validate_parts(platform, profile_id)
        target = self._managed_path(platform, profile_id)
        self._assert_no_symlink(self.app_data, target)
        self._require_directory(target)

        legacy = self._legacy_path(platform, profile_id)
        if legacy is not None:
            self._assert_no_symlink(self.app_data, legacy)
            self._require_directory(legacy)
            if legacy.exists() and target.exists():
                self._fail(
                    "profile_conflict",
                    "旧抖音 profile 与新目录同时存在，已停止；不会合并或清除任一目录。",
                )
            if legacy.exists():
                return ProfileResolution(platform, profile_id, legacy, "legacy_alias")

        if target.exists():
            return ProfileResolution(platform, profile_id, target, "managed")
        if not create:
            return ProfileResolution(platform, profile_id, target, "missing")
        if platform not in _PROFILE_ENABLED:
            self._fail("profile_unavailable", "该平台当前不创建登录 profile。")

        target.mkdir(parents=True, exist_ok=False)
        self._assert_no_symlink(self.app_data, target)
        return ProfileResolution(platform, profile_id, target, "managed")

    def browser_paths(
        self,
        platform: str,
        profile_id: str = "default",
        *,
        create: bool = False,
    ) -> BrowserProfilePaths:
        """派生互不复用的 user-data、缓存、临时与下载临时目录。"""
        resolution = self.resolve(platform, profile_id, create=create)
        if resolution.state == "missing":
            self._fail("profile_missing", "profile 尚未创建，不能派生浏览器目录。")

        runtime_root = resolution.path / ".lulu_runtime"
        cache_dir = runtime_root / "cache"
        temp_dir = runtime_root / "tmp"
        download_temp_dir = runtime_root / "downloads"
        for target in (runtime_root, cache_dir, temp_dir, download_temp_dir):
            self._assert_no_symlink(self.app_data, target)
            self._require_directory(target)
            if create:
                target.mkdir(parents=True, exist_ok=True)
                self._assert_no_symlink(self.app_data, target)
                self._require_directory(target)
        return BrowserProfilePaths(
            resolution=resolution,
            user_data_dir=resolution.path,
            cache_dir=cache_dir,
            temp_dir=temp_dir,
            download_temp_dir=download_temp_dir,
        )

    def acquire(self, platform: str, profile_id: str = "default") -> ProfileLease:
        resolution = self.resolve(platform, profile_id)
        if resolution.state == "missing":
            self._fail("profile_missing", "profile 尚未创建，不能取得独占锁。")

        lock_root = self.managed_root / ".locks"
        self._assert_no_symlink(self.app_data, lock_root)
        lock_root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, lock_root)
        lock_path = lock_root / f"{resolution.platform}-{resolution.profile_id}.lock"
        self._reject_symlink(lock_path)
        token = uuid4().hex
        payload = json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "token": token,
        }, ensure_ascii=True).encode("utf-8")
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProfileError("profile_busy", "该 profile 正被占用，已停止。") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return ProfileLease(lock_path, token)

    def acquire_debug_port(
        self,
        platform: str,
        profile_id: str,
        port: int,
    ) -> DebugPortLease:
        """在所有采集平台之间独占一个显式调试端口。"""
        platform, profile_id = self._validate_parts(platform, profile_id)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            self._fail("invalid_debug_port", "浏览器调试端口必须在 1024 到 65535 之间。")

        lock_root = self.managed_root / ".browser_ports"
        self._assert_no_symlink(self.app_data, lock_root)
        lock_root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, lock_root)
        lock_path = lock_root / f"{port}.lock"
        self._reject_symlink(lock_path)
        token = uuid4().hex
        payload = json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "profile_id": profile_id,
            "port": port,
            "token": token,
        }, ensure_ascii=True).encode("utf-8")
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProfileError("browser_port_busy", "浏览器调试端口正被占用，已停止。") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return DebugPortLease(lock_path, token, port)

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        try:
            left.relative_to(right)
            return True
        except ValueError:
            pass
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False

    @staticmethod
    def _same_inode(left: Path, right: Path) -> bool:
        try:
            left_stat = left.stat()
            right_stat = right.stat()
        except (FileNotFoundError, OSError):
            return False
        return (
            left_stat.st_dev == right_stat.st_dev
            and left_stat.st_ino == right_stat.st_ino
        )

    @staticmethod
    def _owned_directory_identity(path: Path) -> tuple[int, int, int]:
        try:
            value = path.lstat()
        except OSError as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "无法验证小红书专用 Profile 身份。",
            ) from exc
        current_uid = getattr(os, "getuid", lambda: value.st_uid)()
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != current_uid
            or value.st_mode & 0o022
        ):
            raise ProfileError(
                "profile_identity_blocked",
                "小红书专用 Profile 的所有权或权限不安全。",
            )
        return value.st_dev, value.st_ino, value.st_uid

    def validate_xhs_browser_profile(
        self,
        *,
        daily_chrome_roots: tuple[Path | str, ...],
        create: bool = True,
    ) -> XhsBrowserProfile:
        """验证固定 xiaohongshu/default 与日常 Chrome、Douyin 物理隔离。"""
        expected = self.managed_root / "xiaohongshu" / "default"
        protected_paths: list[Path] = [
            self.managed_root / "douyin",
            self.app_data / "douyin_profile",
        ]
        protected_paths.extend(Path(value).expanduser() for value in daily_chrome_roots)
        try:
            expected_real = expected.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "无法验证小红书专用 Profile 路径。",
            ) from exc
        for protected in protected_paths:
            if not protected.is_absolute():
                self._fail("profile_identity_blocked", "受保护浏览器路径必须是绝对路径。")
            try:
                protected_real = protected.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ProfileError(
                    "profile_identity_blocked",
                    "无法验证受保护浏览器路径。",
                ) from exc
            if self._paths_overlap(expected_real, protected_real):
                self._fail(
                    "profile_identity_blocked",
                    "小红书专用 Profile 与其他浏览器身份发生碰撞。",
                )

        resolution = self.resolve("xiaohongshu", "default", create=create)
        if resolution.state == "missing":
            self._fail("profile_identity_blocked", "小红书专用 Profile 尚未创建。")
        if resolution.path != expected or not resolution.path.is_absolute():
            self._fail("profile_identity_blocked", "小红书专用 Profile 路径发生漂移。")
        try:
            self._assert_no_symlink(self.app_data, resolution.path)
            profile_real = resolution.path.resolve(strict=True)
            app_real = self.app_data.resolve(strict=True)
        except (OSError, RuntimeError, ProfileError) as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "无法验证小红书专用 Profile 路径。",
            ) from exc
        if not self._paths_overlap(profile_real, app_real):
            self._fail("profile_identity_blocked", "小红书专用 Profile 真实路径不安全。")
        dev, ino, uid = self._owned_directory_identity(profile_real)

        for protected in protected_paths:
            try:
                protected_real = protected.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ProfileError(
                    "profile_identity_blocked",
                    "无法验证受保护浏览器路径。",
                ) from exc
            if (
                self._paths_overlap(profile_real, protected_real)
                or self._same_inode(profile_real, protected)
            ):
                self._fail(
                    "profile_identity_blocked",
                    "小红书专用 Profile 与其他浏览器身份发生碰撞。",
                )

        identity = f"{profile_real}\0{dev}\0{ino}\0{uid}".encode("utf-8")
        return XhsBrowserProfile(
            resolution=resolution,
            identity_digest=hashlib.sha256(identity).hexdigest(),
        )

    def acquire_xhs_controller(
        self,
        *,
        owner_pid: int,
        owner_birth: str,
        identity_is_alive: Callable[[int, str], bool],
    ) -> XhsControllerLease:
        """取得当前 Lulu 控制锁；只接管已严格证明死亡的旧控制器。"""
        if (
            isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid <= 1
            or not isinstance(owner_birth, str)
            or not 1 <= len(owner_birth) <= 160
        ):
            self._fail("controller_busy", "当前 Lulu 控制器身份无效。")
        root = self.managed_root / ".xhs_browser"
        self._assert_no_symlink(self.app_data, root)
        root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, root)
        lock_path = root / "controller.lock"
        token = uuid4().hex
        payload = {
            "schema_version": 1,
            "kind": "xhs_browser_controller",
            "owner_pid": owner_pid,
            "owner_birth": owner_birth,
            "token": token,
        }
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        for attempt in range(2):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(lock_path, flags, 0o600)
            except FileExistsError as exc:
                try:
                    existing = self._read_xhs_browser_json(lock_path)
                except ProfileError as read_exc:
                    raise ProfileError(
                        "controller_busy",
                        "小红书浏览器控制锁身份不明，已停止。",
                    ) from read_exc
                valid = (
                    isinstance(existing, dict)
                    and set(existing) == {
                        "schema_version",
                        "kind",
                        "owner_pid",
                        "owner_birth",
                        "token",
                    }
                    and existing.get("schema_version") == 1
                    and existing.get("kind") == "xhs_browser_controller"
                    and isinstance(existing.get("owner_pid"), int)
                    and not isinstance(existing.get("owner_pid"), bool)
                    and existing["owner_pid"] > 1
                    and isinstance(existing.get("owner_birth"), str)
                    and 1 <= len(existing["owner_birth"]) <= 160
                    and isinstance(existing.get("token"), str)
                    and bool(re.fullmatch(r"[a-f0-9]{32}", existing["token"]))
                )
                if (
                    not valid
                    or identity_is_alive(
                        existing["owner_pid"],
                        existing["owner_birth"],
                    )
                    or attempt
                ):
                    raise ProfileError(
                        "controller_busy",
                        "另一个 Lulu 实例正在控制小红书专用浏览器。",
                    ) from exc
                lock_path.unlink()
                self._fsync_directory(root)
                continue
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._fsync_directory(root)
            return XhsControllerLease(lock_path, token)
        self._fail("controller_busy", "无法取得小红书浏览器控制锁。")

    def _read_xhs_browser_json(self, path: Path) -> dict[str, Any] | None:
        self._assert_no_symlink(self.app_data, path)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "无法读取小红书浏览器身份收据。",
            ) from exc
        current_uid = getattr(os, "getuid", lambda: before.st_uid)()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != current_uid
            or before.st_mode & 0o077
            or before.st_size > 4096
        ):
            self._fail("profile_identity_blocked", "小红书浏览器身份收据不可信。")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "无法读取小红书浏览器身份收据。",
            ) from exc
        try:
            opened = os.fstat(fd)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_uid != before.st_uid
                or opened.st_mode != before.st_mode
            ):
                self._fail(
                    "profile_identity_blocked",
                    "小红书浏览器身份收据发生漂移。",
                )
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            self._fail("profile_identity_blocked", "小红书浏览器身份收据过大。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise ProfileError(
                "profile_identity_blocked",
                "小红书浏览器身份收据损坏。",
            ) from exc
        if not isinstance(payload, dict):
            self._fail("profile_identity_blocked", "小红书浏览器身份收据损坏。")
        return payload

    def load_xhs_browser_receipt(self) -> XhsBrowserReceipt | None:
        path = self.managed_root / ".xhs_browser" / "runtime.json"
        payload = self._read_xhs_browser_json(path)
        if payload is None:
            return None
        expected = {
            "schema_version",
            "kind",
            "platform",
            "profile_id",
            "profile_identity_digest",
            "chrome_pid",
            "chrome_birth",
            "launched_at_ns",
            "verified_at_ns",
        }
        valid = (
            set(payload) == expected
            and payload.get("schema_version") == 1
            and payload.get("kind") == "xhs_persistent_browser"
            and payload.get("platform") == "xiaohongshu"
            and payload.get("profile_id") == "default"
            and isinstance(payload.get("profile_identity_digest"), str)
            and bool(re.fullmatch(r"[a-f0-9]{64}", payload["profile_identity_digest"]))
            and isinstance(payload.get("chrome_pid"), int)
            and not isinstance(payload.get("chrome_pid"), bool)
            and payload["chrome_pid"] > 1
            and isinstance(payload.get("chrome_birth"), str)
            and 1 <= len(payload["chrome_birth"]) <= 160
            and isinstance(payload.get("launched_at_ns"), int)
            and not isinstance(payload.get("launched_at_ns"), bool)
            and payload["launched_at_ns"] > 0
            and isinstance(payload.get("verified_at_ns"), int)
            and not isinstance(payload.get("verified_at_ns"), bool)
            and payload["verified_at_ns"] >= payload["launched_at_ns"]
        )
        if not valid:
            self._fail("profile_identity_blocked", "小红书浏览器身份收据损坏。")
        return XhsBrowserReceipt(
            profile_identity_digest=payload["profile_identity_digest"],
            chrome_pid=payload["chrome_pid"],
            chrome_birth=payload["chrome_birth"],
            launched_at_ns=payload["launched_at_ns"],
            verified_at_ns=payload["verified_at_ns"],
        )

    def save_xhs_browser_receipt(self, receipt: XhsBrowserReceipt) -> None:
        root = self.managed_root / ".xhs_browser"
        self._assert_no_symlink(self.app_data, root)
        root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, root)
        target = root / "runtime.json"
        self._reject_symlink(target)
        temporary = root / f".runtime.{uuid4().hex}.tmp"
        raw = json.dumps(
            receipt.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, target)
            self._fsync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_xhs_browser_receipt(self, expected: XhsBrowserReceipt) -> bool:
        path = self.managed_root / ".xhs_browser" / "runtime.json"
        current = self.load_xhs_browser_receipt()
        if current is None:
            return True
        if current != expected:
            return False
        path.unlink()
        self._fsync_directory(path.parent)
        return True

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _create_owned_lock(self, lock_path: Path, payload: dict[str, Any]) -> None:
        self._assert_no_symlink(self.app_data, lock_path.parent)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, lock_path.parent)
        self._reject_symlink(lock_path)
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            code = (
                "profile_busy"
                if lock_path.parent.name == ".locks"
                else "browser_port_busy"
            )
            raise ProfileError(code, "XHS 进程 lease 已被占用，已停止。") from exc
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory(lock_path.parent)

    def _write_xhs_receipt(self, receipt: XhsProcessLeaseReceipt) -> Path:
        receipt_root = self.managed_root / ".xhs_runs"
        self._assert_no_symlink(self.app_data, receipt_root)
        receipt_root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.app_data, receipt_root)
        receipt_path = receipt_root / f"{receipt.run_id}.json"
        payload = json.dumps(
            receipt.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(receipt_path, flags, 0o600)
        except FileExistsError as exc:
            raise ProfileError("xhs_lease_corrupt", "XHS 运行收据重复，已停止。") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory(receipt_root)
        return receipt_path

    def acquire_xhs_process_leases(
        self,
        *,
        run_id: str,
        port: int,
        owner: dict[str, Any],
    ) -> XhsProcessLeases:
        """为双层 XHS 进程创建可由监督进程按 token 恢复的两把锁。"""
        if not re.fullmatch(r"[a-f0-9]{32}", str(run_id or "")):
            self._fail("xhs_lease_corrupt", "XHS run_id 非法。")
        if isinstance(port, bool) or not isinstance(port, int) or not 44100 <= port <= 44999:
            self._fail("invalid_debug_port", "XHS 调试端口必须在专用端口段。")
        required_owner = {
            "supervisor_pid", "supervisor_birth", "operation_pid",
            "operation_birth", "process_group", "children",
        }
        if set(owner) != required_owner:
            self._fail("xhs_lease_corrupt", "XHS owner 收据字段非法。")
        for pid_key in ("supervisor_pid", "operation_pid", "process_group"):
            if isinstance(owner[pid_key], bool) or not isinstance(owner[pid_key], int):
                self._fail("xhs_lease_corrupt", "XHS owner PID 非法。")
            if owner[pid_key] <= 1:
                self._fail("xhs_lease_corrupt", "XHS owner PID 非法。")
        for birth_key in ("supervisor_birth", "operation_birth"):
            value = owner[birth_key]
            if not isinstance(value, str) or not 1 <= len(value) <= 160:
                self._fail("xhs_lease_corrupt", "XHS owner 出生指纹非法。")
        children = owner["children"]
        if not isinstance(children, list) or not 1 <= len(children) <= 31:
            self._fail("xhs_lease_corrupt", "XHS 子进程收据非法。")
        for child in children:
            if (
                not isinstance(child, dict)
                or set(child) != {"pid", "birth", "role"}
                or isinstance(child.get("pid"), bool)
                or not isinstance(child.get("pid"), int)
                or child["pid"] <= 1
                or not isinstance(child.get("birth"), str)
                or not 1 <= len(child["birth"]) <= 160
                or child.get("role") not in {"chromium_root", "chromium_helper"}
            ):
                self._fail("xhs_lease_corrupt", "XHS 子进程身份非法。")
        if sum(child["role"] == "chromium_root" for child in children) != 1:
            self._fail("xhs_lease_corrupt", "XHS Chromium root 收据非法。")

        resolution = self.resolve("xiaohongshu", "default", create=True)
        profile_lock = self.managed_root / ".locks" / "xiaohongshu-default.lock"
        port_lock = self.managed_root / ".browser_ports" / f"{port}.lock"
        profile_token = uuid4().hex
        port_token = uuid4().hex
        common = {
            "schema_version": 1,
            "kind": "xhs_process",
            "run_id": run_id,
            "platform": "xiaohongshu",
            "profile_id": "default",
            "owner": dict(owner),
        }
        self._create_owned_lock(
            profile_lock,
            {**common, "resource": "profile", "token": profile_token},
        )
        profile_lease = ProfileLease(profile_lock, profile_token)
        try:
            self._create_owned_lock(
                port_lock,
                {
                    **common,
                    "resource": "port",
                    "port": port,
                    "token": port_token,
                },
            )
            port_lease = DebugPortLease(port_lock, port_token, port)
            receipt = XhsProcessLeaseReceipt(
                run_id=run_id,
                profile_token=profile_token,
                port_token=port_token,
                port=port,
                owner=dict(owner),
            )
            try:
                receipt_path = self._write_xhs_receipt(receipt)
            except Exception:
                port_lease.release()
                raise
        except Exception:
            profile_lease.release()
            raise
        if resolution.platform != "xiaohongshu":
            port_lease.release()
            profile_lease.release()
            self._fail("xhs_lease_corrupt", "XHS profile 解析漂移。")
        return XhsProcessLeases(profile_lease, port_lease, receipt, receipt_path)

    def _read_xhs_json(self, path: Path) -> dict[str, Any] | None:
        self._assert_no_symlink(self.app_data, path)
        self._reject_symlink(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileError("xhs_lease_corrupt", "XHS lease 无法读取。") from exc
        if len(raw) > 16_384:
            self._fail("xhs_lease_corrupt", "XHS lease 超出大小上限。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise ProfileError("xhs_lease_corrupt", "XHS lease JSON 损坏。") from exc
        if not isinstance(payload, dict):
            self._fail("xhs_lease_corrupt", "XHS lease 结构损坏。")
        return payload

    def load_xhs_process_receipt(self, run_id: str) -> dict[str, Any] | None:
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            self._fail("xhs_lease_corrupt", "XHS run_id 非法。")
        return self._read_xhs_json(
            self.managed_root / ".xhs_runs" / f"{run_id}.json"
        )

    def xhs_process_locks_absent(self, port: int) -> bool:
        if isinstance(port, bool) or not isinstance(port, int) or not 44100 <= port <= 44999:
            return False
        return (
            self._read_xhs_json(
                self.managed_root / ".locks" / "xiaohongshu-default.lock"
            )
            is None
            and self._read_xhs_json(
                self.managed_root / ".browser_ports" / f"{port}.lock"
            )
            is None
        )

    def _unlink_owned_lock(self, path: Path, expected: dict[str, Any]) -> bool:
        payload = self._read_xhs_json(path)
        if payload is None:
            return True
        if payload != expected:
            return False
        path.unlink()
        self._fsync_directory(path.parent)
        return True

    def recover_xhs_process_leases(
        self,
        receipt_payload: dict[str, Any],
        *,
        identity_is_alive: Callable[[int, str], bool],
        port_is_available: Callable[[int], bool],
    ) -> XhsLeaseRecoveryResult:
        """只在身份、端口和 token 联合证明后恢复 XHS 进程 lease。"""
        expected_keys = {
            "schema_version", "run_id", "platform", "profile_id",
            "profile_token", "port_token", "port", "owner",
        }
        if not isinstance(receipt_payload, dict) or set(receipt_payload) != expected_keys:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        if (
            receipt_payload.get("schema_version") != 1
            or receipt_payload.get("platform") != "xiaohongshu"
            or receipt_payload.get("profile_id") != "default"
        ):
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        run_id = receipt_payload.get("run_id")
        port = receipt_payload.get("port")
        owner = receipt_payload.get("owner")
        profile_token = receipt_payload.get("profile_token")
        port_token = receipt_payload.get("port_token")
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", run_id)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 44100 <= port <= 44999
            or not isinstance(owner, dict)
            or not isinstance(profile_token, str)
            or not re.fullmatch(r"[a-f0-9]{32}", profile_token)
            or not isinstance(port_token, str)
            or not re.fullmatch(r"[a-f0-9]{32}", port_token)
        ):
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        required_owner = {
            "supervisor_pid", "supervisor_birth", "operation_pid",
            "operation_birth", "process_group", "children",
        }
        if set(owner) != required_owner:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        for pid_key in ("supervisor_pid", "operation_pid", "process_group"):
            value = owner.get(pid_key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 1:
                return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        for birth_key in ("supervisor_birth", "operation_birth"):
            value = owner.get(birth_key)
            if not isinstance(value, str) or not 1 <= len(value) <= 160:
                return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        children = owner.get("children")
        if not isinstance(children, list) or not 1 <= len(children) <= 31:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        for child in children:
            if (
                not isinstance(child, dict)
                or set(child) != {"pid", "birth", "role"}
                or isinstance(child.get("pid"), bool)
                or not isinstance(child.get("pid"), int)
                or child["pid"] <= 1
                or not isinstance(child.get("birth"), str)
                or not 1 <= len(child["birth"]) <= 160
                or child.get("role") not in {"chromium_root", "chromium_helper"}
            ):
                return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        if sum(child["role"] == "chromium_root" for child in children) != 1:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        receipt_path = self.managed_root / ".xhs_runs" / f"{run_id}.json"
        persisted = self._read_xhs_json(receipt_path)
        if persisted != receipt_payload:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        identities = (
            (owner.get("supervisor_pid"), owner.get("supervisor_birth")),
            (owner.get("operation_pid"), owner.get("operation_birth")),
        )
        child_identities = [
            (child.get("pid"), child.get("birth"))
            for child in children
        ]
        for pid, birth in (*identities, *child_identities):
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or not isinstance(birth, str)
                or identity_is_alive(pid, birth)
            ):
                return XhsLeaseRecoveryResult(False, "xhs_recovery_blocked")
        if not port_is_available(port):
            return XhsLeaseRecoveryResult(False, "xhs_recovery_blocked")

        profile_lock = self.managed_root / ".locks" / "xiaohongshu-default.lock"
        port_lock = self.managed_root / ".browser_ports" / f"{port}.lock"
        profile_payload = self._read_xhs_json(profile_lock)
        port_payload = self._read_xhs_json(port_lock)
        common = {
            "schema_version": 1,
            "kind": "xhs_process",
            "run_id": run_id,
            "platform": "xiaohongshu",
            "profile_id": "default",
            "owner": owner,
        }
        expected_profile = {
            **common,
            "resource": "profile",
            "token": profile_token,
        }
        expected_port = {
            **common,
            "resource": "port",
            "port": port,
            "token": port_token,
        }
        if profile_payload is not None and profile_payload != expected_profile:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        if port_payload is not None and port_payload != expected_port:
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        if not self._unlink_owned_lock(profile_lock, expected_profile):
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        if not self._unlink_owned_lock(port_lock, expected_port):
            return XhsLeaseRecoveryResult(False, "xhs_lease_corrupt")
        receipt_path.unlink(missing_ok=True)
        self._fsync_directory(receipt_path.parent)
        return XhsLeaseRecoveryResult(True, "recovered")

    def clear(
        self,
        platform: str,
        profile_id: str = "default",
        *,
        close_browser: Callable[[ProfileResolution], bool],
        remove_tree: Callable[[Path], None] = shutil.rmtree,
    ) -> ClearResult:
        """关闭、取锁、墓碑 rename、严格删除；失败时不谎报成功。"""
        resolution = self.resolve(platform, profile_id)
        if resolution.state == "missing":
            return ClearResult(True, "already_absent")
        if not close_browser(resolution):
            self._fail("browser_busy", "无法确认目标浏览器已关闭，未清除任何数据。")

        with self.acquire(platform, profile_id):
            current = self.resolve(platform, profile_id)
            if current.path != resolution.path or current.state == "missing":
                self._fail("profile_changed", "profile 在清除前发生变化，已停止。")
            target = current.path
            self._assert_no_symlink(self.app_data, target)

            trash_root = self.managed_root / ".trash"
            self._assert_no_symlink(self.app_data, trash_root)
            trash_root.mkdir(parents=True, exist_ok=True)
            self._assert_no_symlink(self.app_data, trash_root)
            tombstone = trash_root / (
                f"{current.platform}-{current.profile_id}-{uuid4().hex}"
            )
            os.replace(target, tombstone)
            try:
                remove_tree(tombstone)
            except Exception:  # noqa: BLE001 - 保留墓碑供受控恢复/清理
                return ClearResult(False, "partial_clear_failed", tombstone.name)
            if target.exists() or tombstone.exists():
                return ClearResult(False, "partial_clear_failed", tombstone.name)
            return ClearResult(True, "cleared")


__all__ = [
    "BrowserProfilePaths",
    "ClearResult",
    "DebugPortLease",
    "ProfileError",
    "ProfileLease",
    "ProfileRegistry",
    "ProfileResolution",
    "XhsBrowserProfile",
    "XhsBrowserReceipt",
    "XhsControllerLease",
    "XhsLeaseRecoveryResult",
    "XhsProcessLeaseReceipt",
    "XhsProcessLeases",
]
