"""Strict semantic config repository backed by Phase 6 lock/CAS."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from ..resource_control.cas import EMPTY_CONTENT_SHA256, ResourceCASStore
from ..resource_control.contracts import CASRevision, ResourceKind, ResourceLease
from ..resource_control.locks import ResourceLockManager
from .contracts import (
    ConfigFingerprint,
    ConfigRevision,
    ConfigSnapshot,
    SharedDataReferences,
    integrity_error,
    migration_conflict,
)
from .paths import ConfigPaths


MAX_CONFIG_BYTES = 4 * 1024 * 1024
CONFIG_RESOURCE_ID = "canonical-config"
CURRENT_CONFIG_SCHEMA_VERSION = 1
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "license_code",
        "secret",
        "token",
        "password",
        "app_secret",
        "client_secret",
        "access_token",
        "refresh_token",
        "oauth_token",
        "authorization",
        "credential",
        "credentials",
    }
)


@dataclass(frozen=True)
class ConfigDocument:
    raw: bytes
    semantic: bytes
    fingerprint: ConfigFingerprint
    values: dict[str, object]


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise integrity_error("CONFIG_SEMANTIC_VALUE_INVALID") from exc


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate config key")
        result[key] = value
    return result


def _normalized_key(value: str) -> str:
    return value.casefold().replace("-", "_")


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("config key must be text")
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_SECRET_KEYS or any(
                normalized.endswith(f"_{suffix}")
                for suffix in ("api_key", "secret", "token", "password")
            ):
                raise ValueError("secret field is forbidden")
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)


def _decode_config(raw: bytes) -> tuple[dict[str, object], bytes, int]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("config payload size invalid")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite config value")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("config JSON invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("config top level must be an object")
    _reject_secret_fields(document)
    schema_version = document.get("schema_version", CURRENT_CONFIG_SCHEMA_VERSION)
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("config schema invalid")
    return document, _canonical_bytes(document), schema_version


def _validate_no_follow(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise integrity_error("CONFIG_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("CONFIG_PATH_SYMLINK_FORBIDDEN")


def read_owner_file(path: Path) -> bytes | None:
    _validate_no_follow(path)
    if not path.exists():
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_CONFIG_BYTES
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise integrity_error("CONFIG_FILE_INVALID")
            chunks: list[bytes] = []
            remaining = MAX_CONFIG_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise integrity_error("CONFIG_READ_FAILED") from exc
    raw = b"".join(chunks)
    if len(raw) > MAX_CONFIG_BYTES:
        raise integrity_error("CONFIG_FILE_INVALID")
    return raw


def atomic_write_owner_file(path: Path, payload: bytes) -> None:
    if type(payload) is not bytes or len(payload) > MAX_CONFIG_BYTES:
        raise integrity_error("CONFIG_WRITE_PAYLOAD_INVALID")
    _validate_no_follow(path)
    name = f".config-migration.{secrets.token_hex(16)}.tmp"
    temporary_path = path.parent / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary_path, flags, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short config write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise integrity_error("CONFIG_ATOMIC_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class ConfigRepository:
    def __init__(
        self,
        paths: ConfigPaths,
        *,
        lock_manager: ResourceLockManager,
        cas_store: ResourceCASStore,
        fault_injector: object | None = None,
    ) -> None:
        if not isinstance(paths, ConfigPaths):
            raise TypeError("ConfigRepository requires ConfigPaths")
        if (
            not isinstance(lock_manager, ResourceLockManager)
            or not isinstance(cas_store, ResourceCASStore)
            or cas_store.lock_manager is not lock_manager
            or lock_manager.resource_root != paths.roots.resource_root
        ):
            raise TypeError("ConfigRepository requires shared Phase 6 lock/CAS")
        self.paths = paths
        self.lock_manager = lock_manager
        self.cas_store = cas_store
        if fault_injector is not None and not callable(
            getattr(fault_injector, "hit", None)
        ):
            raise TypeError("fault injector must expose hit(point)")
        self._fault_injector = fault_injector

    def _hit(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector.hit(point)

    def _allowed_path(self, path: Path) -> None:
        if path not in {self.paths.legacy_config, self.paths.canonical_config}:
            raise integrity_error("CONFIG_PATH_NOT_ALLOWED")

    def inspect(self, path: Path) -> ConfigDocument:
        self._allowed_path(path)
        raw = read_owner_file(path)
        if raw is None:
            return ConfigDocument(
                raw=b"",
                semantic=b"",
                fingerprint=ConfigFingerprint(
                    exists=False,
                    valid=False,
                    content_sha256=None,
                    semantic_sha256=None,
                    size_bytes=0,
                    schema_version=None,
                ),
                values={},
            )
        content_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            values, semantic, schema_version = _decode_config(raw)
        except ValueError:
            return ConfigDocument(
                raw=raw,
                semantic=b"",
                fingerprint=ConfigFingerprint(
                    exists=True,
                    valid=False,
                    content_sha256=content_sha256,
                    semantic_sha256=None,
                    size_bytes=len(raw),
                    schema_version=None,
                ),
                values={},
            )
        return ConfigDocument(
            raw=raw,
            semantic=semantic,
            fingerprint=ConfigFingerprint(
                exists=True,
                valid=True,
                content_sha256=content_sha256,
                semantic_sha256=hashlib.sha256(semantic).hexdigest(),
                size_bytes=len(raw),
                schema_version=schema_version,
            ),
            values=values,
        )

    def _assert_config_lease(self, lease: ResourceLease) -> None:
        if (
            not isinstance(lease, ResourceLease)
            or lease.resource_kind is not ResourceKind.CONFIG
            or lease.resource_id != CONFIG_RESOURCE_ID
        ):
            raise integrity_error("CONFIG_RESOURCE_LEASE_INVALID")
        self.lock_manager.assert_held(lease)

    def track_canonical(
        self,
        lease: ResourceLease,
        document: ConfigDocument,
    ) -> ConfigRevision:
        self._assert_config_lease(lease)
        if not document.fingerprint.valid:
            raise integrity_error("CANONICAL_CONFIG_INVALID")
        current = self.cas_store.read(ResourceKind.CONFIG, CONFIG_RESOURCE_ID)
        if current is None:
            current = self.cas_store.write(
                lease,
                expected=CASRevision(0, EMPTY_CONTENT_SHA256),
                content=document.raw,
            )
        elif current.content != document.raw:
            raise migration_conflict("CANONICAL_CAS_CONTENT_MISMATCH")
        return ConfigRevision(
            revision=current.revision.revision,
            content_sha256=document.fingerprint.content_sha256 or "",
            semantic_sha256=document.fingerprint.semantic_sha256 or "",
        )

    def commit_canonical(
        self,
        lease: ResourceLease,
        *,
        expected: ConfigFingerprint,
        payload: bytes,
    ) -> ConfigRevision:
        self._assert_config_lease(lease)
        current_document = self.inspect(self.paths.canonical_config)
        if current_document.fingerprint != expected:
            raise migration_conflict("CANONICAL_CONFIG_CHANGED")
        try:
            _values, semantic, _schema_version = _decode_config(payload)
        except ValueError as exc:
            raise integrity_error("CONFIG_PAYLOAD_INVALID") from exc
        current_cas = self.cas_store.read(ResourceKind.CONFIG, CONFIG_RESOURCE_ID)
        expected_cas = (
            CASRevision(0, EMPTY_CONTENT_SHA256)
            if current_cas is None
            else current_cas.revision
        )
        updated = self.cas_store.write(
            lease,
            expected=expected_cas,
            content=payload,
        )
        atomic_write_owner_file(self.paths.canonical_config, payload)
        self._hit("before_canonical_readback")
        readback = self.inspect(self.paths.canonical_config)
        if not readback.fingerprint.valid or readback.raw != payload:
            raise integrity_error("CONFIG_READBACK_MISMATCH")
        return ConfigRevision(
            revision=updated.revision.revision,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            semantic_sha256=hashlib.sha256(semantic).hexdigest(),
        )

    def read_canonical(self) -> ConfigSnapshot:
        document = self.inspect(self.paths.canonical_config)
        if not document.fingerprint.valid:
            raise integrity_error("CANONICAL_CONFIG_UNAVAILABLE")
        tracked = self.cas_store.read(ResourceKind.CONFIG, CONFIG_RESOURCE_ID)
        if tracked is None or tracked.content != document.raw:
            raise integrity_error("CANONICAL_CONFIG_CAS_MISMATCH")
        revision = ConfigRevision(
            revision=tracked.revision.revision,
            content_sha256=document.fingerprint.content_sha256 or "",
            semantic_sha256=document.fingerprint.semantic_sha256 or "",
        )
        return ConfigSnapshot(
            revision=revision,
            values=deepcopy(document.values),
        )

    def write_canonical(
        self,
        lease: ResourceLease,
        *,
        expected: ConfigRevision,
        payload: bytes,
    ) -> ConfigSnapshot:
        self._assert_config_lease(lease)
        if not isinstance(expected, ConfigRevision):
            raise integrity_error("CONFIG_REVISION_INVALID")
        current = self.read_canonical()
        if current.revision != expected:
            raise migration_conflict("CONFIG_REVISION_CONFLICT")
        updated = self.commit_canonical(
            lease,
            expected=self.inspect(self.paths.canonical_config).fingerprint,
            payload=payload,
        )
        readback = self.read_canonical()
        if readback.revision != updated:
            raise integrity_error("CONFIG_REVISION_READBACK_MISMATCH")
        return readback

    def shared_data_references(self) -> SharedDataReferences:
        product_root = self.paths.roots.application_support_root / "Lulu"
        return SharedDataReferences(
            transcripts=product_root / "library",
            voices=product_root / "voices",
            models=product_root / "models",
            jobs=product_root / "jobs",
        )
