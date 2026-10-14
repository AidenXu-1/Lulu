"""跨平台主页采集批次历史仓库。

``collection/history.json`` 与作品主档 ``records.json`` 分离。仓库只使用标准库，
所有写入都带 source-hash 并发保护、同目录临时文件、fsync、原子替换和回滚。
旧 ``collections/snapshots.json`` 只在已知 schema v1 下做逐字节备份与全批导入。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Iterable, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import AWEME_TYPE_IMAGE, CollectedItem


SCHEMA_VERSION = 1
EMPTY_SOURCE_HASH = hashlib.sha256(b"").hexdigest()
_PLATFORMS = frozenset({"douyin", "xiaohongshu", "bilibili"})
_COMPLETIONS = frozenset({"complete", "partial", "cancelled"})
_MODEL_FIELDS = frozenset(field.name for field in fields(CollectedItem))
_ENVELOPE_FIELDS = frozenset({
    "schema_version",
    "selected_record_id",
    "active_records",
    "trashed_records",
    "legacy_import_receipts",
})
_RECORD_FIELDS = frozenset({
    "record_id",
    "name",
    "platform",
    "author_id",
    "author_name",
    "homepage_url",
    "source_method",
    "created_at",
    "updated_at",
    "completion",
    "reason_code",
    "confirmed_empty",
    "items",
    "item_trash",
    "trashed_at",
})
_LEGACY_ENVELOPE_FIELDS = frozenset({"schema_version", "selected_id", "active", "trash"})
_LEGACY_RECORD_FIELDS = frozenset({
    "id",
    "name",
    "author_name",
    "created_at",
    "method",
    "items",
    "deleted_items",
    "source_profile_url",
    "partial",
    "author_fans",
    "author_total_like",
    "trashed_at",
})
_LEGACY_ITEM_FIELDS = frozenset({
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
    "source_url",
    "aweme_type",
    "author_fans",
    "author_total_like",
})
_LOCKS: dict[Path, RLock] = {}
_LOCKS_GUARD = RLock()


class HistoryError(RuntimeError):
    """稳定、可测试的历史仓库错误。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class LegacyImportReceipt:
    source_hash: str
    imported_count: int

    def to_dict(self) -> dict:
        return {
            "source_hash": self.source_hash,
            "imported_count": self.imported_count,
        }


def _aware_iso(value: datetime | str | None = None) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HistoryError("invalid_timestamp") from exc
    else:
        parsed = value or datetime.now().astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone().isoformat(timespec="seconds")


def _validate_aware_iso(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoryError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise HistoryError("invalid_timestamp", "历史时间必须携带时区")


def _platform_label(platform: str) -> str:
    return {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}[platform]


@dataclass(frozen=True)
class CollectionRecord:
    record_id: str
    name: str
    platform: str
    author_id: str
    author_name: str
    homepage_url: str
    source_method: str
    created_at: str
    updated_at: str
    completion: str
    reason_code: str
    confirmed_empty: bool
    items: tuple[CollectedItem, ...]
    item_trash: tuple[CollectedItem, ...] = ()
    trashed_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "item_trash", tuple(self.item_trash))
        if not self.record_id.strip() or not self.name.strip():
            raise HistoryError("invalid_record", "记录身份和名称不能为空")
        if self.platform not in _PLATFORMS:
            raise HistoryError("invalid_record", "记录平台非法")
        if self.completion not in _COMPLETIONS:
            raise HistoryError("invalid_record", "记录完成状态非法")
        if not isinstance(self.confirmed_empty, bool):
            raise HistoryError("invalid_record", "confirmed_empty 必须是布尔值")
        _validate_aware_iso(self.created_at)
        _validate_aware_iso(self.updated_at)
        if self.trashed_at:
            _validate_aware_iso(self.trashed_at)
        if self.confirmed_empty and (
            self.completion != "complete" or self.items or self.item_trash
        ):
            raise HistoryError("invalid_record", "确认空主页只能是无作品的完整记录")
        seen: set[str] = set()
        for item in self.items + self.item_trash:
            if item.platform != self.platform or not item.stable_key:
                raise HistoryError("invalid_record", "批次不得混合平台或缺少稳定身份")
            if item.stable_key in seen:
                raise HistoryError("duplicate_item", "同一批次内作品身份重复")
            seen.add(item.stable_key)

    @classmethod
    def create(
        cls,
        items: Iterable[CollectedItem],
        *,
        platform: str,
        homepage_url: str,
        source_method: str,
        completion: str,
        reason_code: str = "",
        confirmed_empty: bool = False,
        now: datetime | None = None,
        record_id: str | None = None,
        name: str | None = None,
    ) -> "CollectionRecord":
        rows = tuple(items)
        if platform not in _PLATFORMS:
            raise HistoryError("invalid_record", "记录平台非法")
        if not rows and not confirmed_empty:
            raise HistoryError("invalid_record", "无合法作品的采集结果不能进入历史")
        if completion in {"partial", "cancelled"} and not rows:
            raise HistoryError("invalid_record", "部分或取消记录必须保留作品")
        stamp = _aware_iso(now)
        author_id = next((item.author_id.strip() for item in rows if item.author_id.strip()), "")
        author_name = next(
            (item.author_name.strip() for item in rows if item.author_name.strip()), ""
        )
        local_time = datetime.fromisoformat(stamp).astimezone()
        display_author = author_name or "未知作者"
        default_name = (
            f"{display_author} · {_platform_label(platform)} · {local_time:%m-%d %H:%M}"
        )
        return cls(
            record_id=record_id or f"record-{uuid4().hex}",
            name=(name or default_name).strip(),
            platform=platform,
            author_id=author_id,
            author_name=author_name,
            homepage_url=str(homepage_url or ""),
            source_method=str(source_method or ""),
            created_at=stamp,
            updated_at=stamp,
            completion=completion,
            reason_code=str(reason_code or ""),
            confirmed_empty=confirmed_empty,
            items=rows,
            item_trash=(),
        )

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "name": self.name,
            "platform": self.platform,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "homepage_url": self.homepage_url,
            "source_method": self.source_method,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completion": self.completion,
            "reason_code": self.reason_code,
            "confirmed_empty": self.confirmed_empty,
            "items": [item.to_dict() for item in self.items],
            "item_trash": [item.to_dict() for item in self.item_trash],
            "trashed_at": self.trashed_at,
        }


@dataclass(frozen=True)
class HistorySnapshot:
    active_records: tuple[CollectionRecord, ...]
    trashed_records: tuple[CollectionRecord, ...]
    selected_record_id: str
    legacy_import_receipts: tuple[LegacyImportReceipt, ...]
    schema_version: int
    writable: bool
    code: str
    source_hash: str

    @property
    def selected_record(self) -> CollectionRecord | None:
        return next(
            (row for row in self.active_records if row.record_id == self.selected_record_id),
            None,
        )


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


def _record_from_dict(raw: object) -> CollectionRecord:
    if not isinstance(raw, Mapping) or set(raw) != _RECORD_FIELDS:
        raise HistoryError("invalid_record", "记录包含未知或缺失字段")

    def parse_items(value: object) -> tuple[CollectedItem, ...]:
        if not isinstance(value, list):
            raise HistoryError("invalid_record", "作品容器必须是列表")
        parsed: list[CollectedItem] = []
        for item_raw in value:
            if not isinstance(item_raw, Mapping) or set(item_raw) - _MODEL_FIELDS:
                raise HistoryError("invalid_record", "作品字段非法")
            try:
                item = CollectedItem(**dict(item_raw))
            except (TypeError, ValueError) as exc:
                raise HistoryError("invalid_record", "作品无法按 schema v2 解析") from exc
            if item.schema_version != 2:
                raise HistoryError("invalid_record", "历史只接受作品 schema v2")
            parsed.append(item)
        return tuple(parsed)

    return CollectionRecord(
        record_id=str(raw["record_id"]),
        name=str(raw["name"]),
        platform=str(raw["platform"]),
        author_id=str(raw["author_id"]),
        author_name=str(raw["author_name"]),
        homepage_url=str(raw["homepage_url"]),
        source_method=str(raw["source_method"]),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
        completion=str(raw["completion"]),
        reason_code=str(raw["reason_code"]),
        confirmed_empty=raw["confirmed_empty"],
        items=parse_items(raw["items"]),
        item_trash=parse_items(raw["item_trash"]),
        trashed_at=str(raw["trashed_at"]),
    )


def _receipt_from_dict(raw: object) -> LegacyImportReceipt:
    if not isinstance(raw, Mapping) or set(raw) != {"source_hash", "imported_count"}:
        raise HistoryError("invalid_receipt")
    source_hash = str(raw["source_hash"])
    count = raw["imported_count"]
    if len(source_hash) != 64 or not all(char in "0123456789abcdef" for char in source_hash):
        raise HistoryError("invalid_receipt")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise HistoryError("invalid_receipt")
    return LegacyImportReceipt(source_hash, count)


def _newest_record_id(records: Iterable[CollectionRecord]) -> str:
    rows = tuple(records)
    if not rows:
        return ""
    return max(rows, key=lambda row: datetime.fromisoformat(row.created_at)).record_id


def _decode_document(payload: bytes, *, strict: bool) -> HistorySnapshot:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryError("invalid_json") from exc
    if not isinstance(document, Mapping):
        raise HistoryError("invalid_envelope")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HistoryError("invalid_schema_version")
    if version == SCHEMA_VERSION and set(document) != _ENVELOPE_FIELDS:
        if strict:
            raise HistoryError("invalid_envelope")
    elif version < SCHEMA_VERSION:
        raise HistoryError("invalid_schema_version")

    active_raw = document.get("active_records", [])
    trash_raw = document.get("trashed_records", [])
    receipts_raw = document.get("legacy_import_receipts", [])
    if not isinstance(active_raw, list) or not isinstance(trash_raw, list):
        raise HistoryError("invalid_records")
    if not isinstance(receipts_raw, list):
        raise HistoryError("invalid_receipts")

    problems = False

    def parse_bucket(values: list[object]) -> tuple[CollectionRecord, ...]:
        nonlocal problems
        parsed: list[CollectionRecord] = []
        for value in values:
            try:
                parsed.append(_record_from_dict(value))
            except HistoryError:
                if strict:
                    raise
                problems = True
        return tuple(parsed)

    active = parse_bucket(active_raw)
    trash = parse_bucket(trash_raw)
    receipts: list[LegacyImportReceipt] = []
    for value in receipts_raw:
        try:
            receipts.append(_receipt_from_dict(value))
        except HistoryError:
            if strict:
                raise
            problems = True

    ids: set[str] = set()
    for record in active + trash:
        if record.record_id in ids:
            if strict:
                raise HistoryError("duplicate_record")
            problems = True
            continue
        ids.add(record.record_id)
    selected = str(document.get("selected_record_id") or "")
    active_ids = {record.record_id for record in active}
    if selected not in active_ids:
        selected = _newest_record_id(active)

    writable = version == SCHEMA_VERSION and not problems and set(document) == _ENVELOPE_FIELDS
    code = "ok" if writable else (
        "future_schema_read_only" if version > SCHEMA_VERSION else "corrupt_repository"
    )
    return HistorySnapshot(
        active,
        trash,
        selected,
        tuple(receipts),
        version,
        writable,
        code,
        hashlib.sha256(payload).hexdigest(),
    )


def _encode_snapshot(
    active: Iterable[CollectionRecord],
    trash: Iterable[CollectionRecord],
    selected: str,
    receipts: Iterable[LegacyImportReceipt],
) -> bytes:
    document = {
        "schema_version": SCHEMA_VERSION,
        "selected_record_id": selected,
        "active_records": [record.to_dict() for record in active],
        "trashed_records": [record.to_dict() for record in trash],
        "legacy_import_receipts": [receipt.to_dict() for receipt in receipts],
    }
    return (json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n").encode("utf-8")


def _validate_history_bytes(payload: bytes) -> HistorySnapshot:
    snapshot = _decode_document(payload, strict=True)
    if not snapshot.writable or snapshot.schema_version != SCHEMA_VERSION:
        raise HistoryError("post_write_validation_failed")
    return snapshot


def _validate_safe_path(path: Path) -> None:
    if not path.is_absolute() or path == Path(path.anchor) or path.is_symlink():
        raise HistoryError("unsafe_path")
    candidates = (Path(tempfile.gettempdir()), Path.home(), Path("/tmp"), Path("/var/tmp"))
    applicable: list[Path] = []
    for candidate in candidates:
        try:
            path.relative_to(candidate)
        except ValueError:
            continue
        applicable.append(candidate)
    trusted_root = max(applicable, key=lambda value: len(value.parts)) if applicable else Path(path.anchor)
    cursor = path.parent
    while cursor != trusted_root:
        if cursor.is_symlink():
            raise HistoryError("unsafe_path")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent


class CollectionHistoryRepository:
    """schema v1 批次历史；所有变更返回新的完整快照。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        legacy_path: Path | str | None = None,
    ) -> None:
        if path is None:
            from ..models_manager import app_data_dir
            path = app_data_dir() / "collection" / "history.json"
        self.path = Path(path)
        self.legacy_path = (
            Path(legacy_path)
            if legacy_path is not None
            else self.path.parent.parent / "collections" / "snapshots.json"
        )
        self.rollback_path = self.path.with_name(f"{self.path.name}.rollback")
        self.legacy_backup_path = self.path.parent / "legacy-snapshots-v1.bak"
        self._temp_prefix = f".{self.path.name}.tmp-"
        self._validate_storage_boundary()
        lock_key = self.path.resolve(strict=False)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(lock_key, RLock())

    def _validate_storage_boundary(self) -> None:
        for path in (
            self.path,
            self.rollback_path,
            self.legacy_backup_path,
            self.legacy_path,
        ):
            _validate_safe_path(path)

    def _source_bytes(self) -> bytes | None:
        self._validate_storage_boundary()
        try:
            return _read_bytes(self.path) if self.path.exists() else None
        except OSError as exc:
            raise HistoryError(_error_code(exc, "read_failed")) from exc

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
        current_readable = False
        if self.path.exists():
            try:
                _decode_document(_read_bytes(self.path), strict=False)
                current_readable = True
            except (OSError, HistoryError):
                pass
        try:
            if current_readable:
                _unlink(self.rollback_path)
            else:
                rollback = _read_bytes(self.rollback_path)
                _validate_history_bytes(rollback)
                self._validate_storage_boundary()
                _replace_file(self.rollback_path, self.path)
            _fsync_directory(self.path.parent)
        except (OSError, HistoryError) as exc:
            raise HistoryError(
                _error_code(exc, "recovery_failed"),
                "主文件与回滚文件均无法安全恢复，现场已保留",
            ) from exc
        self._cleanup_orphan_temps()

    def _restore_previous(self, previous: bytes | None) -> None:
        self._validate_storage_boundary()
        if previous is None:
            _unlink(self.path)
            _fsync_directory(self.path.parent)
            return
        temp = _write_temp_bytes(self.path.parent, self._temp_prefix, previous)
        try:
            self._validate_storage_boundary()
            _replace_file(temp, self.path)
            _fsync_directory(self.path.parent)
        finally:
            _unlink(temp)

    def _atomic_store(self, payload: bytes, previous: bytes | None) -> None:
        self._validate_storage_boundary()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_storage_boundary()
        temp_path: Path | None = None
        rollback_temp: Path | None = None
        replaced = False
        try:
            temp_path = _write_temp_bytes(self.path.parent, self._temp_prefix, payload)
            _validate_history_bytes(_read_bytes(temp_path))
            if previous is not None:
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
            _validate_history_bytes(persisted)
            if persisted != payload:
                raise HistoryError("post_write_validation_failed")
            _unlink(self.rollback_path)
            _fsync_directory(self.path.parent)
        except BaseException as exc:
            if replaced:
                try:
                    self._restore_previous(previous)
                except BaseException as rollback_exc:
                    raise HistoryError(
                        "rollback_failed", "新文件失败且上一份文件未能恢复"
                    ) from rollback_exc
            if isinstance(exc, HistoryError):
                raise
            raise HistoryError(_error_code(exc, "atomic_write_failed")) from exc
        finally:
            if temp_path is not None:
                _unlink(temp_path)
            if rollback_temp is not None:
                _unlink(rollback_temp)
            if not replaced:
                _unlink(self.rollback_path)

    def _assert_current_hash(self, expected: str) -> None:
        current = self._source_bytes()
        actual = EMPTY_SOURCE_HASH if current is None else hashlib.sha256(current).hexdigest()
        if actual != expected:
            raise HistoryError("concurrent_change")

    def _backup_legacy(self, source: bytes) -> None:
        self._validate_storage_boundary()
        if self.legacy_backup_path.exists():
            if _read_bytes(self.legacy_backup_path) == source:
                return
            raise HistoryError("backup_conflict")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_storage_boundary()
        temp = _write_temp_bytes(self.path.parent, self._temp_prefix, source)
        try:
            self._validate_storage_boundary()
            _replace_file(temp, self.legacy_backup_path)
            temp = None
            _fsync_directory(self.path.parent)
            if _read_bytes(self.legacy_backup_path) != source:
                raise HistoryError("backup_validation_failed")
        except BaseException as exc:
            if isinstance(exc, HistoryError):
                raise
            raise HistoryError(_error_code(exc, "backup_failed")) from exc
        finally:
            if temp is not None:
                _unlink(temp)

    @staticmethod
    def _legacy_item(raw: object) -> CollectedItem:
        if not isinstance(raw, Mapping) or set(raw) - _LEGACY_ITEM_FIELDS:
            raise HistoryError("migration_failed", "旧作品字段无法无损解释")
        aweme_id = str(raw.get("aweme_id") or "")
        if not aweme_id:
            raise HistoryError("migration_failed", "旧作品缺少 aweme_id")

        def integer(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, bool):
                raise HistoryError("migration_failed", f"旧作品 {name} 非法")
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError) as exc:
                raise HistoryError("migration_failed", f"旧作品 {name} 非法") from exc
            if parsed < 0:
                raise HistoryError("migration_failed", f"旧作品 {name} 非法")
            return parsed

        aweme_type = integer("aweme_type")
        payload = {
            "aweme_id": aweme_id,
            "work_id": aweme_id,
            "platform": "douyin",
            "author_name": str(raw.get("author_name") or ""),
            "title": str(raw.get("title") or ""),
            "create_time": integer("create_time"),
            "duration_ms": integer("duration_ms"),
            "like": integer("like"),
            "collect": integer("collect"),
            "comment": integer("comment"),
            "share": integer("share"),
            "play_url": str(raw.get("play_url") or ""),
            "work_url": str(raw.get("source_url") or ""),
            "aweme_type": aweme_type,
            "content_kind": "image" if aweme_type == AWEME_TYPE_IMAGE else "video",
            "source_method": "legacy_snapshot_import",
        }
        try:
            return CollectedItem(**payload)
        except (TypeError, ValueError) as exc:
            raise HistoryError("migration_failed", "旧作品无法转为 schema v2") from exc

    def _legacy_records(
        self, source: bytes
    ) -> tuple[tuple[CollectionRecord, ...], tuple[CollectionRecord, ...], str]:
        try:
            document = json.loads(source.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HistoryError("migration_failed", "旧预览不是有效 JSON") from exc
        if not isinstance(document, Mapping) or set(document) != _LEGACY_ENVELOPE_FIELDS:
            raise HistoryError("migration_failed", "旧预览 envelope 不受支持")
        if document.get("schema_version") != 1:
            raise HistoryError("migration_failed", "旧预览 schema 不受支持")
        source_hash = hashlib.sha256(source).hexdigest()
        id_map: dict[str, str] = {}

        def parse_bucket(raw_bucket: object, *, trashed: bool) -> tuple[CollectionRecord, ...]:
            if not isinstance(raw_bucket, list):
                raise HistoryError("migration_failed", "旧记录容器必须是列表")
            records: list[CollectionRecord] = []
            for raw in raw_bucket:
                if not isinstance(raw, Mapping) or set(raw) - _LEGACY_RECORD_FIELDS:
                    raise HistoryError("migration_failed", "旧记录字段无法无损解释")
                old_id = str(raw.get("id") or "")
                if not old_id or old_id in id_map:
                    raise HistoryError("migration_failed", "旧记录身份缺失或重复")
                items_raw = raw.get("items")
                trash_raw = raw.get("deleted_items", [])
                if not isinstance(items_raw, list) or not isinstance(trash_raw, list):
                    raise HistoryError("migration_failed", "旧作品容器非法")
                items = tuple(self._legacy_item(item) for item in items_raw)
                item_trash = tuple(self._legacy_item(item) for item in trash_raw)
                partial = raw.get("partial", False)
                if not isinstance(partial, bool):
                    raise HistoryError("migration_failed", "旧 partial 非法")
                if not items and not item_trash:
                    raise HistoryError("migration_failed", "旧空记录无法证明真实为空")
                method = str(raw.get("method") or "")
                method_map = {
                    "anonymous": "anonymous_latest",
                    "authenticated": "profile_all",
                    "anonymous_latest": "anonymous_latest",
                    "profile_all": "profile_all",
                }
                if method not in method_map:
                    raise HistoryError("migration_failed", "旧采集方式无法解释")
                record_id = f"record-{uuid5(NAMESPACE_URL, source_hash + ':' + old_id).hex}"
                id_map[old_id] = record_id
                created_at = _aware_iso(str(raw.get("created_at") or ""))
                trashed_at = str(raw.get("trashed_at") or "")
                if trashed and not trashed_at:
                    trashed_at = created_at
                record = CollectionRecord(
                    record_id=record_id,
                    name=(str(raw.get("name") or "").strip() or "未命名记录"),
                    platform="douyin",
                    author_id="",
                    author_name=str(raw.get("author_name") or ""),
                    homepage_url=str(raw.get("source_profile_url") or ""),
                    source_method=method_map[method],
                    created_at=created_at,
                    updated_at=created_at,
                    completion="partial" if partial else "complete",
                    reason_code="",
                    confirmed_empty=False,
                    items=items,
                    item_trash=item_trash,
                    trashed_at=_aware_iso(trashed_at) if trashed_at else "",
                )
                records.append(record)
            return tuple(records)

        active = parse_bucket(document.get("active"), trashed=False)
        trash = parse_bucket(document.get("trash"), trashed=True)
        selected = id_map.get(str(document.get("selected_id") or ""), "")
        if selected not in {row.record_id for row in active}:
            selected = _newest_record_id(active)
        return active, trash, selected

    def _maybe_import_legacy(self, snapshot: HistorySnapshot) -> HistorySnapshot:
        self._validate_storage_boundary()
        if not snapshot.writable or not self.legacy_path.exists():
            return snapshot
        try:
            source = _read_bytes(self.legacy_path)
        except OSError as exc:
            raise HistoryError(_error_code(exc, "migration_failed")) from exc
        source_hash = hashlib.sha256(source).hexdigest()
        if any(receipt.source_hash == source_hash for receipt in snapshot.legacy_import_receipts):
            return snapshot
        active, trash, selected = self._legacy_records(source)
        self._backup_legacy(source)
        existing_ids = {
            row.record_id for row in snapshot.active_records + snapshot.trashed_records
        }
        if any(row.record_id in existing_ids for row in active + trash):
            raise HistoryError("migration_failed", "旧导入记录与当前身份冲突")
        receipts = snapshot.legacy_import_receipts + (
            LegacyImportReceipt(source_hash, len(active) + len(trash)),
        )
        merged_active = active + snapshot.active_records
        merged_trash = trash + snapshot.trashed_records
        selected_id = snapshot.selected_record_id or selected
        payload = _encode_snapshot(merged_active, merged_trash, selected_id, receipts)
        self._assert_current_hash(snapshot.source_hash)
        previous = self._source_bytes()
        self._atomic_store(payload, previous)
        return _decode_document(payload, strict=True)

    def load(self) -> HistorySnapshot:
        with self._lock:
            self._validate_storage_boundary()
            self._recover_if_needed()
            source = self._source_bytes()
            if source is None:
                snapshot = HistorySnapshot(
                    (), (), "", (), SCHEMA_VERSION, True, "empty", EMPTY_SOURCE_HASH
                )
            else:
                try:
                    snapshot = _decode_document(source, strict=False)
                except HistoryError:
                    snapshot = HistorySnapshot(
                        (), (), "", (), SCHEMA_VERSION, False,
                        "corrupt_repository", hashlib.sha256(source).hexdigest(),
                    )
            return self._maybe_import_legacy(snapshot)

    def _store(
        self,
        snapshot: HistorySnapshot,
        *,
        active: Iterable[CollectionRecord],
        trash: Iterable[CollectionRecord],
        selected: str,
        receipts: Iterable[LegacyImportReceipt] | None = None,
        expected_source_hash: str | None,
    ) -> HistorySnapshot:
        if not snapshot.writable:
            raise HistoryError(snapshot.code)
        if expected_source_hash is not None and expected_source_hash != snapshot.source_hash:
            raise HistoryError("concurrent_change")
        payload = _encode_snapshot(
            active,
            trash,
            selected,
            receipts if receipts is not None else snapshot.legacy_import_receipts,
        )
        _validate_history_bytes(payload)
        self._assert_current_hash(snapshot.source_hash)
        previous = self._source_bytes()
        self._atomic_store(payload, previous)
        return _decode_document(payload, strict=True)

    def add_record(
        self,
        items: Iterable[CollectedItem],
        *,
        platform: str,
        homepage_url: str,
        source_method: str,
        completion: str,
        reason_code: str = "",
        confirmed_empty: bool = False,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        with self._lock:
            snapshot = self.load()
            record = CollectionRecord.create(
                items,
                platform=platform,
                homepage_url=homepage_url,
                source_method=source_method,
                completion=completion,
                reason_code=reason_code,
                confirmed_empty=confirmed_empty,
                now=now,
            )
            return self._store(
                snapshot,
                active=(record,) + snapshot.active_records,
                trash=snapshot.trashed_records,
                selected=record.record_id,
                expected_source_hash=expected_source_hash,
            )

    def select(
        self, record_id: str, *, expected_source_hash: str | None = None
    ) -> HistorySnapshot:
        with self._lock:
            snapshot = self.load()
            if record_id not in {row.record_id for row in snapshot.active_records}:
                raise HistoryError("record_not_found")
            return self._store(
                snapshot,
                active=snapshot.active_records,
                trash=snapshot.trashed_records,
                selected=record_id,
                expected_source_hash=expected_source_hash,
            )

    def rename(
        self,
        record_id: str,
        name: str,
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        cleaned = str(name or "").strip()
        if not cleaned:
            raise HistoryError("invalid_name")
        if len(cleaned) > 120:
            raise HistoryError("invalid_name")
        with self._lock:
            snapshot = self.load()
            changed = False
            stamp = _aware_iso(now)

            def update(rows: tuple[CollectionRecord, ...]) -> tuple[CollectionRecord, ...]:
                nonlocal changed
                output = []
                for row in rows:
                    if row.record_id == record_id:
                        output.append(replace(row, name=cleaned, updated_at=stamp))
                        changed = True
                    else:
                        output.append(row)
                return tuple(output)

            active = update(snapshot.active_records)
            trash = update(snapshot.trashed_records)
            if not changed:
                raise HistoryError("record_not_found")
            return self._store(
                snapshot,
                active=active,
                trash=trash,
                selected=snapshot.selected_record_id,
                expected_source_hash=expected_source_hash,
            )

    def trash_records(
        self,
        record_ids: Iterable[str],
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        wanted = {str(value) for value in record_ids}
        with self._lock:
            snapshot = self.load()
            stamp = _aware_iso(now)
            moved = tuple(
                replace(row, trashed_at=stamp, updated_at=stamp)
                for row in snapshot.active_records if row.record_id in wanted
            )
            if not moved:
                raise HistoryError("record_not_found")
            moved_ids = {row.record_id for row in moved}
            active = tuple(
                row for row in snapshot.active_records if row.record_id not in moved_ids
            )
            selected = snapshot.selected_record_id
            if selected in moved_ids:
                selected = _newest_record_id(active)
            return self._store(
                snapshot,
                active=active,
                trash=moved + snapshot.trashed_records,
                selected=selected,
                expected_source_hash=expected_source_hash,
            )

    def restore_records(
        self,
        record_ids: Iterable[str],
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        wanted = {str(value) for value in record_ids}
        with self._lock:
            snapshot = self.load()
            stamp = _aware_iso(now)
            restored = tuple(
                replace(row, trashed_at="", updated_at=stamp)
                for row in snapshot.trashed_records if row.record_id in wanted
            )
            if not restored:
                raise HistoryError("record_not_found")
            restored_ids = {row.record_id for row in restored}
            trash = tuple(
                row for row in snapshot.trashed_records if row.record_id not in restored_ids
            )
            return self._store(
                snapshot,
                active=restored + snapshot.active_records,
                trash=trash,
                selected=restored[0].record_id,
                expected_source_hash=expected_source_hash,
            )

    def purge_records(
        self,
        record_ids: Iterable[str],
        *,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        wanted = {str(value) for value in record_ids}
        with self._lock:
            snapshot = self.load()
            removed = {
                row.record_id for row in snapshot.trashed_records if row.record_id in wanted
            }
            if not removed:
                raise HistoryError("record_not_found")
            trash = tuple(
                row for row in snapshot.trashed_records if row.record_id not in removed
            )
            return self._store(
                snapshot,
                active=snapshot.active_records,
                trash=trash,
                selected=snapshot.selected_record_id,
                expected_source_hash=expected_source_hash,
            )

    def trash_items(
        self,
        record_id: str,
        stable_keys: Iterable[str],
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        """Atomically move exact active items into the same record's item trash."""
        wanted = {str(value) for value in stable_keys if str(value)}
        if not wanted:
            raise HistoryError("item_not_found")
        with self._lock:
            snapshot = self.load()
            if not snapshot.writable:
                raise HistoryError(snapshot.code)
            stamp = _aware_iso(now)
            found_record = False
            active: list[CollectionRecord] = []
            for record in snapshot.active_records:
                if record.record_id != record_id:
                    active.append(record)
                    continue
                found_record = True
                matches = tuple(
                    row for row in record.items if row.stable_key in wanted
                )
                if {row.stable_key for row in matches} != wanted:
                    raise HistoryError("item_not_found")
                kept = tuple(
                    row for row in record.items if row.stable_key not in wanted
                )
                active.append(replace(
                    record,
                    items=kept,
                    item_trash=record.item_trash + matches,
                    updated_at=stamp,
                ))
            if not found_record:
                raise HistoryError("record_not_found")
            return self._store(
                snapshot,
                active=tuple(active),
                trash=snapshot.trashed_records,
                selected=snapshot.selected_record_id,
                expected_source_hash=expected_source_hash,
            )

    def restore_items(
        self,
        record_id: str,
        stable_keys: Iterable[str],
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        """Atomically restore exact trashed items without overwriting active truth."""
        wanted = {str(value) for value in stable_keys if str(value)}
        if not wanted:
            raise HistoryError("item_not_found")
        with self._lock:
            snapshot = self.load()
            if not snapshot.writable:
                raise HistoryError(snapshot.code)
            stamp = _aware_iso(now)
            found_record = False
            active: list[CollectionRecord] = []
            for record in snapshot.active_records:
                if record.record_id != record_id:
                    active.append(record)
                    continue
                found_record = True
                matches = tuple(
                    row for row in record.item_trash if row.stable_key in wanted
                )
                if {row.stable_key for row in matches} != wanted:
                    raise HistoryError("item_not_found")
                active_keys = {row.stable_key for row in record.items}
                if active_keys & wanted:
                    raise HistoryError("duplicate_item")
                kept = tuple(
                    row for row in record.item_trash if row.stable_key not in wanted
                )
                active.append(replace(
                    record,
                    items=record.items + matches,
                    item_trash=kept,
                    updated_at=stamp,
                ))
            if not found_record:
                raise HistoryError("record_not_found")
            return self._store(
                snapshot,
                active=tuple(active),
                trash=snapshot.trashed_records,
                selected=snapshot.selected_record_id,
                expected_source_hash=expected_source_hash,
            )

    def purge_items(
        self,
        record_id: str,
        stable_keys: Iterable[str],
        *,
        now: datetime | None = None,
        expected_source_hash: str | None = None,
    ) -> HistorySnapshot:
        """Permanently delete exact items from item trash only."""
        wanted = {str(value) for value in stable_keys if str(value)}
        if not wanted:
            raise HistoryError("item_not_found")
        with self._lock:
            snapshot = self.load()
            if not snapshot.writable:
                raise HistoryError(snapshot.code)
            stamp = _aware_iso(now)
            found_record = False
            active: list[CollectionRecord] = []
            for record in snapshot.active_records:
                if record.record_id != record_id:
                    active.append(record)
                    continue
                found_record = True
                matches = {
                    row.stable_key
                    for row in record.item_trash
                    if row.stable_key in wanted
                }
                if matches != wanted:
                    raise HistoryError("item_not_found")
                active.append(replace(
                    record,
                    item_trash=tuple(
                        row
                        for row in record.item_trash
                        if row.stable_key not in wanted
                    ),
                    updated_at=stamp,
                ))
            if not found_record:
                raise HistoryError("record_not_found")
            return self._store(
                snapshot,
                active=tuple(active),
                trash=snapshot.trashed_records,
                selected=snapshot.selected_record_id,
                expected_source_hash=expected_source_hash,
            )
