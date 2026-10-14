"""采集记录迁移与 schema v2 本地仓库。

仓库只使用标准库。所有持久化都在同目录完成临时写入、fsync、原子替换和
替换后校验；失败时保留或恢复上一份可读文件。未来 schema 只允许有限读取。
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
from typing import Iterable, Mapping

from .models import CollectedItem

SCHEMA_VERSION = 2
EMPTY_SOURCE_HASH = hashlib.sha256(b"").hexdigest()
_COVER_STATUSES = frozenset({"not_requested", "url_only", "downloaded", "failed"})
_REPOSITORY_LOCKS: dict[Path, RLock] = {}
_REPOSITORY_LOCKS_GUARD = RLock()
_MODEL_FIELDS = frozenset(field.name for field in fields(CollectedItem))
_MEDIA_OWNED_FIELDS = frozenset({"media_local_path", "media_status"})
_COVER_OWNED_FIELDS = frozenset({
    "cover_local_path", "cover_status", "last_error_code"
})
_TRANSCRIPT_OWNED_FIELDS = frozenset({"transcript_status"})
_DOWNSTREAM_SETTLED_FIELDS = frozenset({
    "cover_local_path",
    "cover_status",
    "media_local_path",
    "media_status",
    "transcript_status",
    "feishu_video_status",
    "feishu_cover_status",
})
_V1_FIELDS = frozenset({
    "schema_version",
    "platform",
    "aweme_id",
    "author_name",
    "title",
    "create_time",
    "duration_ms",
    "like",
    "collect",
    "comment",
    "share",
    "play_url",
    "aweme_type",
})


class MigrationError(ValueError):
    """输入无法无损解释为 v1 抖音或 v2 记录。"""


class RepositoryError(RuntimeError):
    """仓库操作失败；``code`` 是稳定、可测试的失败原因。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class MigrationResult:
    items: tuple[CollectedItem, ...]
    migrated_count: int
    already_v2_count: int


@dataclass(frozen=True)
class MigrationReceipt:
    source_hash: str
    backup_path: Path
    backup_created: bool
    migrated_count: int


@dataclass(frozen=True)
class RepositorySnapshot:
    items: tuple[CollectedItem, ...]
    schema_version: int
    writable: bool
    code: str
    source_hash: str
    record_count: int
    migration_receipt: MigrationReceipt | None = None


@dataclass(frozen=True)
class PersistenceReceipt:
    success: bool
    code: str
    record_count: int
    previous_hash: str
    current_hash: str
    migrated_count: int = 0
    backup_created: bool = False


def _schema_version(record: Mapping[str, object], index: int) -> int:
    raw = record.get("schema_version", 1)
    if isinstance(raw, bool):
        raise MigrationError(f"第 {index} 条 schema_version 非法")
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"第 {index} 条 schema_version 非法") from exc
    if version not in {1, 2}:
        raise MigrationError(f"第 {index} 条 schema_version={version} 不受支持")
    return version


def _migrate_one(record: Mapping[str, object], index: int) -> tuple[CollectedItem, bool]:
    payload = deepcopy(dict(record))
    version = _schema_version(payload, index)

    allowed = _V1_FIELDS if version == 1 else _MODEL_FIELDS
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MigrationError(f"第 {index} 条包含未知字段: {', '.join(unknown)}")

    if version == 1:
        platform = str(payload.get("platform") or "douyin").lower()
        if platform != "douyin":
            raise MigrationError(f"第 {index} 条 v1 记录不能无损解释为非抖音平台")
        aweme_id = str(payload.get("aweme_id") or "")
        if not aweme_id:
            raise MigrationError(f"第 {index} 条 v1 抖音记录缺少 aweme_id")
        payload.update({
            "schema_version": SCHEMA_VERSION,
            "platform": "douyin",
            "work_id": aweme_id,
        })
        return CollectedItem(**payload), True

    try:
        item = CollectedItem(**payload)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"第 {index} 条 v2 记录无效: {exc}") from exc
    if not item.stable_key:
        raise MigrationError(f"第 {index} 条 v2 记录缺少稳定身份")
    return item, False


def migrate_records(records: Iterable[Mapping[str, object]]) -> MigrationResult:
    """原子式迁移内存记录；重复或空稳定键使整批失败。"""
    staged: list[CollectedItem] = []
    seen: set[str] = set()
    migrated = 0
    already_v2 = 0

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise MigrationError(f"第 {index} 条不是字典记录")
        item, was_migrated = _migrate_one(record, index)
        key = item.stable_key
        if not key:
            raise MigrationError(f"第 {index} 条缺少稳定身份")
        if key in seen:
            raise MigrationError(f"第 {index} 条稳定身份重复: {key}")
        seen.add(key)
        staged.append(item)
        if was_migrated:
            migrated += 1
        else:
            already_v2 += 1

    return MigrationResult(tuple(staged), migrated, already_v2)


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _error_code(exc: BaseException, fallback: str) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    return fallback


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _fsync_file(handle: object) -> None:
    os.fsync(handle.fileno())  # type: ignore[attr-defined]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_file(source: Path, target: Path) -> None:
    os.replace(source, target)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _write_temp_bytes(parent: Path, prefix: str, payload: bytes) -> Path:
    descriptor, raw_path = tempfile.mkstemp(dir=parent, prefix=prefix)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            _fsync_file(handle)
        return path
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink(path)
        raise


def _encode_v2(items: Iterable[CollectedItem]) -> bytes:
    document = {
        "schema_version": SCHEMA_VERSION,
        "records": [item.to_dict() for item in items],
    }
    return (json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n").encode("utf-8")


def _parse_document(payload: bytes) -> tuple[int, list[Mapping[str, object]]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryError("invalid_json", "仓库不是有效 UTF-8 JSON") from exc

    if isinstance(document, list):
        return 1, document
    if not isinstance(document, dict):
        raise RepositoryError("invalid_envelope", "仓库顶层必须是列表或对象")
    raw_version = document.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        raise RepositoryError("invalid_schema_version")
    if raw_version <= SCHEMA_VERSION and set(document) != {"schema_version", "records"}:
        raise RepositoryError("invalid_envelope", "仓库 envelope 包含未知或缺失字段")
    records = document.get("records")
    if not isinstance(records, list):
        raise RepositoryError("invalid_records")
    return raw_version, records


def _validate_cover_contract(items: Iterable[CollectedItem]) -> None:
    for item in items:
        if item.cover_status not in _COVER_STATUSES:
            raise RepositoryError(
                "invalid_cover_status",
                f"{item.stable_key} 使用非 P1C 封面状态: {item.cover_status}",
            )


def _decode_writable(payload: bytes) -> tuple[int, MigrationResult]:
    version, records = _parse_document(payload)
    if version > SCHEMA_VERSION:
        raise RepositoryError("future_schema_read_only")
    try:
        result = migrate_records(records)
    except MigrationError as exc:
        raise RepositoryError("migration_failed", str(exc)) from exc
    if version == SCHEMA_VERSION and result.migrated_count:
        raise RepositoryError("migration_failed", "v2 envelope 中存在非 v2 记录")
    _validate_cover_contract(result.items)
    return version, result


def _validate_v2_bytes(payload: bytes) -> MigrationResult:
    version, result = _decode_writable(payload)
    if version != SCHEMA_VERSION or result.migrated_count:
        raise RepositoryError("post_write_validation_failed")
    return result


class CollectionRepository:
    """线程安全、失败关闭的 schema v2 JSON 仓库。"""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            from ..models_manager import app_data_dir
            path = app_data_dir() / "collection" / "records.json"
        self.path = Path(path)
        self._validate_path()
        self.backup_path = self.path.with_name(f"{self.path.name}.v1.bak")
        self.rollback_path = self.path.with_name(f"{self.path.name}.rollback")
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._temp_prefix = f".{self.path.name}.tmp-"
        lock_key = self.path.resolve(strict=False)
        with _REPOSITORY_LOCKS_GUARD:
            self._lock = _REPOSITORY_LOCKS.setdefault(lock_key, RLock())

    def _validate_path(self) -> None:
        if not self.path.is_absolute() or self.path == Path(self.path.anchor):
            raise RepositoryError("unsafe_path", "仓库路径必须是非根目录绝对路径")
        if self.path.is_symlink():
            raise RepositoryError("unsafe_path", "仓库目标不得是符号链接")

        # macOS 的系统临时目录可能经由 /tmp 或 /var 的系统级链接暴露。
        # 把已知受信任根当作边界，只检查其下完整的 lexical ancestor chain；
        # 其他路径一直检查到文件系统根。不能在最近普通目录处提前停止。
        candidates = (
            Path(tempfile.gettempdir()),
            Path.home(),
            Path("/tmp"),
            Path("/var/tmp"),
        )
        trusted_root = Path(self.path.anchor)
        applicable: list[Path] = []
        for candidate in candidates:
            try:
                self.path.relative_to(candidate)
            except ValueError:
                continue
            applicable.append(candidate)
        if applicable:
            trusted_root = max(applicable, key=lambda candidate: len(candidate.parts))

        cursor = self.path.parent
        while cursor != trusted_root:
            if cursor.is_symlink():
                raise RepositoryError("unsafe_path", "仓库路径祖先不得是符号链接")
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent

    def _validate_storage_boundary(self) -> None:
        self._validate_path()
        self._validate_sidecars()

    def _validate_sidecars(self) -> None:
        for sidecar in (self.backup_path, self.rollback_path, self.lock_path):
            if sidecar.is_symlink():
                raise RepositoryError("unsafe_path", "仓库备份或回滚路径不得是符号链接")

    @contextmanager
    def _exclusive_write(self):
        """把线程内合并与跨进程最终发布收进同一独占区。"""

        descriptor: int | None = None
        with self._lock:
            try:
                self._validate_storage_boundary()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._validate_storage_boundary()
                descriptor = os.open(
                    self.lock_path,
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                opened = os.fstat(descriptor)
                lexical = self.lock_path.lstat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or self.lock_path.is_symlink()
                    or (opened.st_dev, opened.st_ino)
                    != (lexical.st_dev, lexical.st_ino)
                ):
                    raise RepositoryError("unsafe_path", "仓库锁文件身份不安全")
                self._validate_storage_boundary()
                yield
            except RepositoryError:
                raise
            except OSError as exc:
                raise RepositoryError(_error_code(exc, "lock_failed")) from exc
            finally:
                if descriptor is not None:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    def _cleanup_orphan_temps(self) -> None:
        self._validate_storage_boundary()
        if not self.path.parent.exists():
            return
        for candidate in self.path.parent.glob(f"{self._temp_prefix}*"):
            if candidate.is_file() and not candidate.is_symlink():
                self._validate_storage_boundary()
                _unlink(candidate)

    def _recover_if_needed(self) -> None:
        self._validate_storage_boundary()
        if not self.rollback_path.exists():
            self._cleanup_orphan_temps()
            return
        current_valid = False
        if self.path.exists():
            try:
                version, _records = _parse_document(_read_bytes(self.path))
                if version <= SCHEMA_VERSION:
                    _decode_writable(_read_bytes(self.path))
                current_valid = True
            except (OSError, RepositoryError):
                current_valid = False
        try:
            if current_valid:
                self._validate_storage_boundary()
                _unlink(self.rollback_path)
            else:
                rollback_payload = _read_bytes(self.rollback_path)
                _decode_writable(rollback_payload)
                self._validate_storage_boundary()
                _replace_file(self.rollback_path, self.path)
            _fsync_directory(self.path.parent)
        except (OSError, RepositoryError) as exc:
            raise RepositoryError(
                _error_code(exc, "recovery_failed"),
                "主文件与回滚残留无法自动恢复，现场已保留",
            ) from exc
        self._cleanup_orphan_temps()

    def _restore_previous(self, previous: bytes | None) -> None:
        self._validate_storage_boundary()
        if previous is None:
            _unlink(self.path)
            _fsync_directory(self.path.parent)
            return
        restore_temp = _write_temp_bytes(self.path.parent, self._temp_prefix, previous)
        try:
            self._validate_storage_boundary()
            _replace_file(restore_temp, self.path)
            _fsync_directory(self.path.parent)
        finally:
            _unlink(restore_temp)

    def _atomic_store(self, payload: bytes, previous: bytes | None) -> None:
        self._validate_storage_boundary()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        rollback_temp: Path | None = None
        replaced = False
        try:
            self._validate_storage_boundary()
            temp_path = _write_temp_bytes(self.path.parent, self._temp_prefix, payload)
            _validate_v2_bytes(_read_bytes(temp_path))

            if previous is not None:
                self._validate_storage_boundary()
                rollback_temp = _write_temp_bytes(
                    self.path.parent, self._temp_prefix, previous
                )
                self._validate_storage_boundary()
                _replace_file(rollback_temp, self.rollback_path)
                rollback_temp = None
                _fsync_directory(self.path.parent)

            self._validate_storage_boundary()
            _replace_file(temp_path, self.path)
            temp_path = None
            replaced = True
            _fsync_directory(self.path.parent)
            persisted = _read_bytes(self.path)
            _validate_v2_bytes(persisted)
            if persisted != payload:
                raise RepositoryError("post_write_validation_failed")
            self._validate_storage_boundary()
            _unlink(self.rollback_path)
            _fsync_directory(self.path.parent)
        except BaseException as exc:
            if replaced:
                try:
                    self._restore_previous(previous)
                except BaseException as rollback_exc:
                    raise RepositoryError(
                        "rollback_failed", "新文件失败且上一份文件未能自动恢复"
                    ) from rollback_exc
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryError(_error_code(exc, "atomic_write_failed")) from exc
        finally:
            if temp_path is not None:
                _unlink(temp_path)
            if rollback_temp is not None:
                _unlink(rollback_temp)
            if not replaced:
                _unlink(self.rollback_path)

    def _backup_v1(self, source: bytes) -> tuple[bool, str]:
        self._validate_storage_boundary()
        source_hash = _hash_bytes(source)
        if self.backup_path.exists():
            try:
                if _hash_bytes(_read_bytes(self.backup_path)) == source_hash:
                    return False, source_hash
            except OSError as exc:
                raise RepositoryError(_error_code(exc, "backup_failed")) from exc
            raise RepositoryError("backup_conflict", "已有 v1 备份与当前源文件不同")

        self._validate_storage_boundary()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            self._validate_storage_boundary()
            temp_path = _write_temp_bytes(self.path.parent, self._temp_prefix, source)
            self._validate_storage_boundary()
            _replace_file(temp_path, self.backup_path)
            temp_path = None
            _fsync_directory(self.path.parent)
            if _read_bytes(self.backup_path) != source:
                raise RepositoryError("backup_validation_failed")
            return True, source_hash
        except BaseException as exc:
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryError(_error_code(exc, "backup_failed")) from exc
        finally:
            if temp_path is not None:
                _unlink(temp_path)

    def _source_bytes(self) -> bytes | None:
        self._validate_path()
        try:
            return _read_bytes(self.path) if self.path.exists() else None
        except OSError as exc:
            raise RepositoryError(_error_code(exc, "read_failed")) from exc

    def load(self) -> RepositorySnapshot:
        with self._lock:
            self._validate_storage_boundary()
            self._recover_if_needed()
            source = self._source_bytes()
            if source is None:
                return RepositorySnapshot(
                    (), SCHEMA_VERSION, True, "empty", EMPTY_SOURCE_HASH, 0
                )

            try:
                version, records = _parse_document(source)
            except RepositoryError as exc:
                raise RepositoryError("corrupt_repository", str(exc)) from exc
            if version > SCHEMA_VERSION:
                return RepositorySnapshot(
                    (),
                    version,
                    False,
                    "future_schema_read_only",
                    _hash_bytes(source),
                    len(records),
                )

            if version == SCHEMA_VERSION:
                try:
                    _version, result = _decode_writable(source)
                except RepositoryError as exc:
                    raise RepositoryError("corrupt_repository", str(exc)) from exc
                return RepositorySnapshot(
                    result.items,
                    SCHEMA_VERSION,
                    True,
                    "ok",
                    _hash_bytes(source),
                    len(result.items),
                )

            backup_created, old_hash = self._backup_v1(source)
            try:
                _version, migrated = _decode_writable(source)
            except RepositoryError as exc:
                if exc.code == "migration_failed":
                    raise
                raise RepositoryError("migration_failed", str(exc)) from exc
            payload = _encode_v2(migrated.items)
            self._atomic_store(payload, source)
            return RepositorySnapshot(
                migrated.items,
                SCHEMA_VERSION,
                True,
                "migrated_v1",
                _hash_bytes(payload),
                len(migrated.items),
                MigrationReceipt(
                    old_hash,
                    self.backup_path,
                    backup_created,
                    migrated.migrated_count,
                ),
            )

    def _normalize_items(
        self, items: Iterable[CollectedItem | Mapping[str, object]]
    ) -> tuple[CollectedItem, ...]:
        records = [item.to_dict() if isinstance(item, CollectedItem) else item for item in items]
        try:
            result = migrate_records(records)
        except MigrationError as exc:
            raise RepositoryError("invalid_input", str(exc)) from exc
        _validate_cover_contract(result.items)
        return result.items

    def _assert_current_hash(self, expected: str) -> None:
        current = self._source_bytes()
        actual = EMPTY_SOURCE_HASH if current is None else _hash_bytes(current)
        if actual != expected:
            raise RepositoryError("concurrent_change", "仓库已被另一写入者修改")

    def upsert(
        self,
        items: Iterable[CollectedItem | Mapping[str, object]],
        *,
        expected_source_hash: str | None = None,
    ) -> PersistenceReceipt:
        staged = self._normalize_items(items)
        with self._exclusive_write():
            snapshot = self.load()
            if not snapshot.writable:
                raise RepositoryError("future_schema_read_only")
            if expected_source_hash is not None and expected_source_hash != snapshot.source_hash:
                raise RepositoryError("concurrent_change", "调用方读取的仓库版本已经过期")

            by_key = {item.stable_key: item for item in snapshot.items}
            order = [item.stable_key for item in snapshot.items]
            for item in staged:
                if item.stable_key not in by_key:
                    order.append(item.stable_key)
                by_key[item.stable_key] = item
            merged = tuple(by_key[key] for key in order)
            payload = _encode_v2(merged)

            self._assert_current_hash(snapshot.source_hash)
            previous = self._source_bytes()
            self._atomic_store(payload, previous)
            current_hash = _hash_bytes(payload)
            return PersistenceReceipt(
                True,
                "saved",
                len(merged),
                snapshot.source_hash,
                current_hash,
                (
                    snapshot.migration_receipt.migrated_count
                    if snapshot.migration_receipt else 0
                ),
                (
                    snapshot.migration_receipt.backup_created
                    if snapshot.migration_receipt else False
                ),
            )

    def upsert_collected(
        self,
        items: Iterable[CollectedItem | Mapping[str, object]],
    ) -> PersistenceReceipt:
        """合并最新采集事实，同时保留已由下游任务结算的字段。"""

        staged = self._normalize_items(items)
        with self._exclusive_write():
            snapshot = self.load()
            if not snapshot.writable:
                raise RepositoryError("future_schema_read_only")

            by_key = {item.stable_key: item for item in snapshot.items}
            order = [item.stable_key for item in snapshot.items]
            for collected in staged:
                current = by_key.get(collected.stable_key)
                if current is None:
                    order.append(collected.stable_key)
                    by_key[collected.stable_key] = collected
                    continue
                payload = collected.to_dict()
                for name in _DOWNSTREAM_SETTLED_FIELDS:
                    payload[name] = getattr(current, name)
                current_error = str(current.last_error_code or "")
                if current_error.startswith(("cover:", "cover_")):
                    payload["last_error_code"] = current_error
                try:
                    by_key[collected.stable_key] = CollectedItem(**payload)
                except (TypeError, ValueError) as exc:
                    raise RepositoryError("invalid_input", str(exc)) from exc

            merged = tuple(by_key[key] for key in order)
            payload = _encode_v2(merged)
            self._assert_current_hash(snapshot.source_hash)
            previous = self._source_bytes()
            self._atomic_store(payload, previous)
            return PersistenceReceipt(
                True,
                "saved",
                len(merged),
                snapshot.source_hash,
                _hash_bytes(payload),
            )

    @staticmethod
    def _cas_domain(fields_to_write: frozenset[str]) -> str:
        if not fields_to_write:
            raise RepositoryError("invalid_field_cas", "CAS 没有要更新的字段")
        if fields_to_write <= _MEDIA_OWNED_FIELDS:
            return "media"
        if fields_to_write <= _TRANSCRIPT_OWNED_FIELDS:
            return "transcript"
        if fields_to_write <= _COVER_OWNED_FIELDS and fields_to_write & {
            "cover_local_path", "cover_status"
        }:
            return "cover"
        raise RepositoryError("invalid_field_cas", "CAS 字段不属于同一写入者")

    def compare_and_swap_fields(
        self,
        stable_key: str,
        expected: Mapping[str, object],
        updates: Mapping[str, object],
    ) -> PersistenceReceipt:
        """在最新主档上只比较、合并本写入者拥有的字段。"""

        key = str(stable_key or "")
        if not key:
            raise RepositoryError("invalid_stable_key")
        if not isinstance(expected, Mapping) or not isinstance(updates, Mapping):
            raise RepositoryError("invalid_field_cas")
        update_keys = frozenset(str(name) for name in updates)
        expected_keys = frozenset(str(name) for name in expected)
        domain = self._cas_domain(update_keys)
        allowed = {
            "media": _MEDIA_OWNED_FIELDS,
            "cover": _COVER_OWNED_FIELDS,
            "transcript": _TRANSCRIPT_OWNED_FIELDS,
        }[domain]
        if not expected_keys <= allowed or not update_keys <= expected_keys:
            raise RepositoryError(
                "invalid_field_cas",
                "CAS 必须对每个更新字段提供同领域旧值",
            )

        with self._exclusive_write():
            for _attempt in range(3):
                snapshot = self.load()
                if not snapshot.writable:
                    raise RepositoryError("future_schema_read_only")
                by_key = {item.stable_key: item for item in snapshot.items}
                current = by_key.get(key)
                if current is None:
                    raise RepositoryError("record_not_found")
                for name, old_value in expected.items():
                    if getattr(current, str(name)) != old_value:
                        raise RepositoryError(
                            "field_conflict", f"{name} 已被较新写入修改"
                        )

                if domain == "cover" and "last_error_code" in update_keys:
                    previous_error = str(expected.get("last_error_code") or "")
                    next_error = str(updates.get("last_error_code") or "")
                    if (
                        previous_error
                        and not previous_error.startswith(("cover:", "cover_"))
                    ) or (
                        next_error and not next_error.startswith(("cover:", "cover_"))
                    ):
                        raise RepositoryError(
                            "field_conflict", "last_error_code 不属于封面命名空间"
                        )

                payload = current.to_dict()
                payload.update({str(name): value for name, value in updates.items()})
                try:
                    replacement = CollectedItem(**payload)
                except (TypeError, ValueError) as exc:
                    raise RepositoryError("invalid_input", str(exc)) from exc
                by_key[key] = replacement
                merged = tuple(by_key[item.stable_key] for item in snapshot.items)
                encoded = _encode_v2(merged)

                self._assert_current_hash(snapshot.source_hash)
                previous = self._source_bytes()
                previous_hash = (
                    EMPTY_SOURCE_HASH if previous is None else _hash_bytes(previous)
                )
                if previous_hash != snapshot.source_hash:
                    # 非协作写入落在 hash 检查后的窄窗口时，从最新真值
                    # 重新比较 owned fields 并重合并，绝不发布旧整文件。
                    continue
                self._atomic_store(encoded, previous)
                return PersistenceReceipt(
                    True,
                    "saved",
                    len(merged),
                    snapshot.source_hash,
                    _hash_bytes(encoded),
                )
            raise RepositoryError(
                "concurrent_change",
                "仓库在最终发布前持续变化，本次字段更新未写入",
            )
