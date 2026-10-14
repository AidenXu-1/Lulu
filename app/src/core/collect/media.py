"""主页采集音视频的受管本地提交与实时复核。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile
from typing import Protocol
from urllib.parse import urlsplit

from .models import CollectedItem
from .repository import CollectionRepository, RepositoryError
from ..task import TaskExecutionPolicy

_STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}:[^:\s/\\]{1,256}$")
_AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
_VIDEO_EXTENSIONS = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_MEDIA_EXTENSIONS = _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS


@dataclass(frozen=True)
class ManagedMediaReceipt:
    status: str
    relative_path: str = ""
    size: int = 0
    sha256: str = ""
    reason_code: str = ""
    reused: bool = False

    @classmethod
    def failed(cls, reason_code: str) -> "ManagedMediaReceipt":
        return cls("failed", reason_code=reason_code)


@dataclass(frozen=True)
class ManagedMediaValidation:
    valid: bool
    path: Path | None = None
    size: int = 0
    reason_code: str = ""


class ManagedMediaFetchError(RuntimeError):
    """可跨取材边界保留、但不泄露外部 URL 的稳定失败。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "media_download_failed")


def managed_media_root() -> Path:
    from ..models_manager import app_data_dir

    return app_data_dir() / "collection" / "media"


def reliable_media_sources(item: CollectedItem) -> tuple[str, ...]:
    """返回可用的媒体来源，保留「直链优先、作品页回退」顺序。"""

    if not str(getattr(item, "stable_key", "") or ""):
        return ()
    sources: list[str] = []
    for raw in (
        getattr(item, "play_url", ""),
        getattr(item, "work_url", ""),
    ):
        source = str(raw or "").strip()
        if not source or source in sources:
            continue
        try:
            parsed = urlsplit(source)
        except ValueError:
            continue
        if not (
            parsed.scheme.casefold() == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            continue
        sources.append(source)
    return tuple(sources)


class ManagedMediaStore:
    """只在 Lulu 受管根内提交、复核稳定身份的媒体文件。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else managed_media_root()
        if not self.root.is_absolute() or self.root == Path(self.root.anchor):
            raise ValueError("受管媒体根必须是非根目录绝对路径")
        self._assert_root_safe(create=False)

    def _assert_root_safe(self, *, create: bool) -> None:
        try:
            descriptor = self._open_root(create=create)
        except FileNotFoundError:
            if create:
                raise
            return
        else:
            os.close(descriptor)

    def _trusted_root(self) -> Path:
        candidates = (
            Path(tempfile.gettempdir()),
            Path.home(),
            Path("/tmp"),
            Path("/var/tmp"),
        )
        applicable: list[Path] = []
        for candidate in candidates:
            try:
                self.root.relative_to(candidate)
            except ValueError:
                continue
            applicable.append(candidate)
        if applicable:
            return max(applicable, key=lambda candidate: len(candidate.parts))
        return Path(self.root.anchor)

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    def _open_root(self, *, create: bool) -> int:
        """从受信任根逐级打开字面目录，绝不跟随中途链接。"""

        trusted = self._trusted_root()
        relative = self.root.relative_to(trusted)
        current = os.open(trusted, self._directory_flags())
        try:
            for part in relative.parts:
                try:
                    child = os.open(
                        part,
                        self._directory_flags(),
                        dir_fd=current,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    child = os.open(
                        part,
                        self._directory_flags(),
                        dir_fd=current,
                    )
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    def _open_platform(self, root_fd: int, platform: str, *, create: bool) -> int:
        try:
            return os.open(platform, self._directory_flags(), dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(platform, mode=0o700, dir_fd=root_fd)
            return os.open(platform, self._directory_flags(), dir_fd=root_fd)

    @staticmethod
    def _directory_matches(path: Path, descriptor: int) -> bool:
        try:
            lexical = path.lstat()
            opened = os.fstat(descriptor)
        except OSError:
            return False
        return bool(
            not path.is_symlink()
            and stat.S_ISDIR(lexical.st_mode)
            and (lexical.st_dev, lexical.st_ino) == (opened.st_dev, opened.st_ino)
        )

    @staticmethod
    def _entry_info(
        directory_fd: int,
        name: str,
    ) -> tuple[int, str] | None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                return None
            digest = hashlib.sha256()
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            return info.st_size, digest.hexdigest()
        except OSError:
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _source_identity(path: Path) -> tuple[int, int, int, int]:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size <= 0:
            raise OSError("媒体源必须是非符号链接的非空普通文件")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _relative_name(stable_key: str, digest: str, suffix: str) -> str:
        identity = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
        return f"{stable_key.split(':', 1)[0]}/{identity}-{digest[:20]}{suffix}"

    def _repository_owner(self, relative_path: str) -> str | None:
        """为未显式传 key 的 Finder 等只读出口补上记录身份绑定。"""

        repository_path = self.root.parent / "records.json"
        if not repository_path.exists():
            return None
        try:
            snapshot = CollectionRepository(repository_path).load()
        except RepositoryError:
            return ""
        owners = {
            item.stable_key
            for item in snapshot.items
            if item.media_status == "downloaded"
            and item.media_local_path == relative_path
        }
        if not owners:
            return None
        if len(owners) != 1:
            return ""
        return next(iter(owners))

    def validate(
        self,
        relative_path: str,
        *,
        expected_stable_key: str | None = None,
        expected_sha256: str = "",
    ) -> ManagedMediaValidation:
        value = str(relative_path or "")
        if not value or "\\" in value:
            return ManagedMediaValidation(False, reason_code="media_path_invalid")
        pure = PurePosixPath(value)
        if pure.is_absolute() or len(pure.parts) != 2 or any(
            part in {"", ".", ".."} for part in pure.parts
        ):
            return ManagedMediaValidation(False, reason_code="media_path_invalid")
        if expected_stable_key is None:
            expected_stable_key = self._repository_owner(value)
            if expected_stable_key == "":
                return ManagedMediaValidation(
                    False,
                    reason_code="media_identity_mismatch",
                )
        if expected_stable_key is not None:
            stable_key = str(expected_stable_key or "")
            if not _STABLE_KEY_RE.fullmatch(stable_key):
                return ManagedMediaValidation(False, reason_code="media_identity_invalid")
            platform = stable_key.split(":", 1)[0]
            identity = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
            if pure.parts[0] != platform or not pure.parts[1].startswith(f"{identity}-"):
                return ManagedMediaValidation(
                    False,
                    reason_code="media_identity_mismatch",
                )
        candidate = self.root.joinpath(*pure.parts)
        root_fd: int | None = None
        platform_fd: int | None = None
        try:
            root_fd = self._open_root(create=False)
            platform_fd = self._open_platform(root_fd, pure.parts[0], create=False)
            entry = self._entry_info(platform_fd, pure.parts[1])
            if entry is None:
                return ManagedMediaValidation(False, reason_code="media_file_missing")
            size, digest = entry
            stem_parts = Path(pure.parts[1]).stem.split("-", 1)
            if len(stem_parts) != 2 or stem_parts[1] != digest[:20]:
                return ManagedMediaValidation(False, reason_code="media_identity_mismatch")
            if expected_sha256 and digest != expected_sha256:
                return ManagedMediaValidation(False, reason_code="media_identity_mismatch")
            if not self._directory_matches(self.root, root_fd) or not self._directory_matches(
                self.root / pure.parts[0],
                platform_fd,
            ):
                return ManagedMediaValidation(False, reason_code="media_path_escape")
            return ManagedMediaValidation(True, candidate, size)
        except (OSError, RuntimeError):
            return ManagedMediaValidation(False, reason_code="media_file_missing")
        finally:
            if platform_fd is not None:
                os.close(platform_fd)
            if root_fd is not None:
                os.close(root_fd)

    def existing_file(self, relative_path: str, *, expected_stable_key: str) -> Path | None:
        """Bounded presence check for display. Consumers must still use validate()."""
        key = str(expected_stable_key or '')
        value = str(relative_path or '')
        if not _STABLE_KEY_RE.fullmatch(key) or '\\' in value:
            return None
        pure = PurePosixPath(value)
        prefix = hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
        if (pure.is_absolute() or len(pure.parts) != 2 or pure.parts[0] != key.split(':', 1)[0]
                or not re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{20}\.[a-z0-9]+', pure.parts[1])):
            return None
        root_fd = platform_fd = None
        try:
            root_fd = self._open_root(create=False)
            platform_fd = self._open_platform(root_fd, pure.parts[0], create=False)
            info = os.stat(pure.parts[1], dir_fd=platform_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0
                    or not self._directory_matches(self.root, root_fd)
                    or not self._directory_matches(self.root / pure.parts[0], platform_fd)):
                return None
            return self.root.joinpath(*pure.parts)
        except (OSError, RuntimeError):
            return None
        finally:
            if platform_fd is not None:
                os.close(platform_fd)
            if root_fd is not None:
                os.close(root_fd)

    def commit(
        self,
        source: Path | str,
        *,
        stable_key: str,
        download_mode: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ManagedMediaReceipt:
        cancel = is_cancelled or (lambda: False)
        if not _STABLE_KEY_RE.fullmatch(str(stable_key or "")):
            return ManagedMediaReceipt.failed("media_identity_invalid")
        if download_mode not in {"audio", "video"}:
            return ManagedMediaReceipt.failed("media_mode_invalid")
        path = Path(source)
        suffix = path.suffix.casefold()
        if suffix not in _MEDIA_EXTENSIONS:
            return ManagedMediaReceipt.failed("media_type_invalid")
        if download_mode == "video" and suffix not in _VIDEO_EXTENSIONS:
            return ManagedMediaReceipt.failed("media_video_unavailable")
        if cancel():
            return ManagedMediaReceipt.failed("media_cancelled")
        root_fd: int | None = None
        platform_fd: int | None = None
        temp_name = ""
        target_name = ""
        created_target = False
        try:
            before = self._source_identity(path)
            digest = self._hash(path)
            if self._source_identity(path) != before:
                return ManagedMediaReceipt.failed("media_source_changed")
            relative = self._relative_name(stable_key, digest, suffix)
            pure = PurePosixPath(relative)
            target_name = pure.parts[1]
            root_fd = self._open_root(create=True)
            platform_fd = self._open_platform(root_fd, pure.parts[0], create=True)
            existing = self._entry_info(platform_fd, target_name)
            if existing is not None:
                if existing[1] == digest:
                    return ManagedMediaReceipt(
                        "downloaded", relative, existing[0], digest, reused=True
                    )
                return ManagedMediaReceipt.failed("media_existing_conflict")
            if cancel():
                return ManagedMediaReceipt.failed("media_cancelled")

            for _attempt in range(16):
                temp_name = f".{target_name}.{secrets.token_hex(8)}.part"
                try:
                    descriptor = os.open(
                        temp_name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=platform_fd,
                    )
                    break
                except FileExistsError:
                    temp_name = ""
            else:
                return ManagedMediaReceipt.failed("media_commit_failed")
            try:
                with path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            if cancel():
                return ManagedMediaReceipt.failed("media_cancelled")
            staged = self._entry_info(platform_fd, temp_name)
            if (
                self._source_identity(path) != before
                or staged is None
                or staged[1] != digest
            ):
                return ManagedMediaReceipt.failed("media_source_changed")
            existing = self._entry_info(platform_fd, target_name)
            if existing is not None:
                if existing[1] == digest:
                    return ManagedMediaReceipt(
                        "downloaded", relative, existing[0], digest, reused=True
                    )
                return ManagedMediaReceipt.failed("media_existing_conflict")
            try:
                os.link(
                    temp_name,
                    target_name,
                    src_dir_fd=platform_fd,
                    dst_dir_fd=platform_fd,
                    follow_symlinks=False,
                )
                created_target = True
            except FileExistsError:
                raced = self._entry_info(platform_fd, target_name)
                if raced is not None and raced[1] == digest:
                    return ManagedMediaReceipt(
                        "downloaded",
                        relative,
                        raced[0],
                        digest,
                        reused=True,
                    )
                return ManagedMediaReceipt.failed("media_existing_conflict")
            os.unlink(temp_name, dir_fd=platform_fd)
            temp_name = ""
            os.fsync(platform_fd)
            if not self._directory_matches(self.root, root_fd) or not self._directory_matches(
                self.root / pure.parts[0],
                platform_fd,
            ):
                return ManagedMediaReceipt.failed("media_commit_uncertain")
            checked = self.validate(
                relative,
                expected_stable_key=stable_key,
                expected_sha256=digest,
            )
            if not checked.valid or checked.path is None:
                return ManagedMediaReceipt.failed("media_commit_uncertain")
            created_target = False
            return ManagedMediaReceipt(
                "downloaded", relative, checked.size, digest, reused=False
            )
        except PermissionError:
            return ManagedMediaReceipt.failed("media_permission_denied")
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                return ManagedMediaReceipt.failed("media_disk_full")
            return ManagedMediaReceipt.failed("media_commit_failed")
        finally:
            if platform_fd is not None:
                if temp_name:
                    try:
                        os.unlink(temp_name, dir_fd=platform_fd)
                    except FileNotFoundError:
                        pass
                if created_target and target_name:
                    try:
                        os.unlink(target_name, dir_fd=platform_fd)
                        os.fsync(platform_fd)
                    except FileNotFoundError:
                        pass
                os.close(platform_fd)
            if root_fd is not None:
                os.close(root_fd)


def resolve_managed_transcription_media(
    stable_key: str,
    *,
    repository: CollectionRepository | None = None,
    store: ManagedMediaStore | None = None,
) -> ManagedMediaValidation:
    """按作品稳定身份解析一份可安全读取的受管转录输入。

    这是批量转录的只读优化边界：任何仓库、路径或文件异常都收敛成
    无效结果，让调用方回到原有链接下载链，不能把本地复用变成新的
    任务失败点。
    """

    identity = str(stable_key or "")
    if not _STABLE_KEY_RE.fullmatch(identity):
        return ManagedMediaValidation(False, reason_code="media_identity_invalid")
    try:
        configured_repository = repository or CollectionRepository()
        configured_store = store or ManagedMediaStore(
            configured_repository.path.parent / "media"
        )
        matches = tuple(
            item
            for item in configured_repository.load().items
            if item.stable_key == identity
        )
        if len(matches) != 1:
            return ManagedMediaValidation(
                False,
                reason_code=(
                    "media_record_missing"
                    if not matches
                    else "media_record_ambiguous"
                ),
            )
        item = matches[0]
        if item.media_status != "downloaded":
            return ManagedMediaValidation(
                False,
                reason_code="media_not_downloaded",
            )
        if not item.media_local_path:
            return ManagedMediaValidation(
                False,
                reason_code="media_path_missing",
            )
        return configured_store.validate(
            item.media_local_path,
            expected_stable_key=identity,
        )
    except (OSError, RepositoryError, RuntimeError, ValueError):
        return ManagedMediaValidation(
            False,
            reason_code="media_lookup_unavailable",
        )


class ManagedMediaFetcher(Protocol):
    def __call__(
        self,
        item: CollectedItem,
        download_mode: str,
        destination: Path,
        is_cancelled: Callable[[], bool],
    ) -> Path: ...


@dataclass(frozen=True)
class MediaItemOutcome:
    stable_key: str
    status: str
    reason_code: str = ""
    relative_path: str = ""
    reused: bool = False


@dataclass(frozen=True)
class MediaAssociationResult:
    items: tuple[CollectedItem, ...]
    outcomes: tuple[MediaItemOutcome, ...]
    download_calls: int


class CollectionMediaAssociationService:
    """将主页作品的媒体提交与字段 CAS 结算成一个本地合同。"""

    def __init__(
        self,
        repository: CollectionRepository,
        store: ManagedMediaStore,
        fetcher: ManagedMediaFetcher,
    ) -> None:
        if not callable(fetcher):
            raise TypeError("fetcher 必须可调用")
        self.repository = repository
        self.store = store
        self.fetcher = fetcher

    @staticmethod
    def _has_reliable_source(item: CollectedItem) -> bool:
        return bool(reliable_media_sources(item))

    def run(self, items, *, policy, is_cancelled=None) -> MediaAssociationResult:
        from .media_coordination import prepare_media
        cancel = is_cancelled or (lambda: False)
        results, outcomes, calls = [], [], 0
        for item in items:
            try:
                with prepare_media(item.stable_key, cancel):
                    # A previous owner may have committed while this caller waited.
                    current = next((entry for entry in self.repository.load().items
                                    if entry.stable_key == item.stable_key), item)
                    result = self._run_unlocked([current], policy=policy, is_cancelled=cancel)
            except InterruptedError:
                results.append(item)
                outcomes.append(MediaItemOutcome(item.stable_key, "cancelled", "media_cancelled"))
                continue
            results.extend(result.items)
            outcomes.extend(result.outcomes)
            calls += result.download_calls
        return MediaAssociationResult(tuple(results), tuple(outcomes), calls)

    def _run_unlocked(
        self,
        items: tuple[CollectedItem, ...] | list[CollectedItem],
        *,
        policy: TaskExecutionPolicy,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> MediaAssociationResult:
        if not isinstance(policy, TaskExecutionPolicy):
            raise TypeError("policy 必须是 TaskExecutionPolicy")
        original = tuple(items)
        if (
            policy.mode != "materialize"
            or policy.media_destination != "managed_collection"
            or not policy.material_snapshot.save_media
        ):
            return MediaAssociationResult(
                original,
                tuple(MediaItemOutcome(item.stable_key, "not_requested") for item in original),
                0,
            )
        cancel = is_cancelled or (lambda: False)
        results: list[CollectedItem] = []
        outcomes: list[MediaItemOutcome] = []
        download_calls = 0
        for item in original:
            if cancel():
                results.append(item)
                outcomes.append(MediaItemOutcome(item.stable_key, "cancelled", "media_cancelled"))
                continue
            if not self._has_reliable_source(item):
                results.append(item)
                outcomes.append(MediaItemOutcome(item.stable_key, "failed", "media_source_unavailable"))
                continue
            valid = self.store.validate(
                item.media_local_path,
                expected_stable_key=item.stable_key,
            )
            if (
                item.media_status == "downloaded"
                and valid.reason_code == "media_identity_mismatch"
            ):
                results.append(item)
                outcomes.append(
                    MediaItemOutcome(
                        item.stable_key,
                        "failed",
                        "media_identity_mismatch",
                    )
                )
                continue
            if item.media_status == "downloaded" and valid.valid:
                results.append(item)
                outcomes.append(MediaItemOutcome(
                    item.stable_key, "downloaded", relative_path=item.media_local_path, reused=True
                ))
                continue

            with tempfile.TemporaryDirectory(prefix="lulu-managed-media-") as tmp:
                download_calls += 1
                fetch_error: Exception | None = None
                try:
                    fetched = self.fetcher(
                        item,
                        policy.material_snapshot.download_mode,
                        Path(tmp),
                        cancel,
                    )
                except InterruptedError:
                    results.append(item)
                    outcomes.append(MediaItemOutcome(item.stable_key, "cancelled", "media_cancelled"))
                    continue
                except Exception as exc:
                    fetch_error = exc
                    fetched = Path("")
                if not fetched or not fetched.is_absolute():
                    results.append(item)
                    outcomes.append(MediaItemOutcome(
                        item.stable_key,
                        "failed",
                        str(
                            getattr(
                                fetch_error,
                                "reason_code",
                                "media_download_failed",
                            )
                        ),
                    ))
                    continue
                receipt = self.store.commit(
                    fetched,
                    stable_key=item.stable_key,
                    download_mode=policy.material_snapshot.download_mode,
                    is_cancelled=cancel,
                )
            if receipt.status != "downloaded":
                results.append(item)
                outcomes.append(MediaItemOutcome(item.stable_key,
                    "cancelled" if receipt.reason_code == "media_cancelled" else "failed", receipt.reason_code))
                continue
            updated = replace(
                item,
                media_local_path=receipt.relative_path,
                media_status="downloaded",
            )
            try:
                self.repository.compare_and_swap_fields(
                    item.stable_key,
                    expected={
                        "media_local_path": item.media_local_path,
                        "media_status": item.media_status,
                    },
                    updates={
                        "media_local_path": updated.media_local_path,
                        "media_status": updated.media_status,
                    },
                )
            except RepositoryError as exc:
                latest = self.repository.load()
                newer = next(
                    (entry for entry in latest.items if entry.stable_key == item.stable_key),
                    item,
                )
                results.append(newer)
                outcomes.append(MediaItemOutcome(item.stable_key, "failed", exc.code))
                continue
            results.append(updated)
            outcomes.append(MediaItemOutcome(
                item.stable_key,
                "downloaded",
                relative_path=receipt.relative_path,
                reused=receipt.reused,
            ))
        return MediaAssociationResult(tuple(results), tuple(outcomes), download_calls)
