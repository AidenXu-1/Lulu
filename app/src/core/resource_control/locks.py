"""Owner-bound, non-blocking OS file locks for shared Lulu resources."""

from __future__ import annotations

from dataclasses import asdict
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import (
    CASRevision,
    LockInspection,
    LockOwner,
    RecoveryAuditV1,
    ResourceKind,
    ResourceLease,
    conflict,
    integrity_error,
    resource_busy,
)


LOCK_SCHEMA = "lulu.resource-lock/v1"
LOCK_RECORD_BYTES = 1024
MAX_IDENTITY_TEXT = 1024


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
        raise integrity_error("RESOURCE_LOCK_VALUE_INVALID") from exc


def _validate_no_follow_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("RESOURCE_LOCK_SYMLINK_FORBIDDEN")


def _validate_path(path: Path, *, directory: bool, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise integrity_error("RESOURCE_LOCK_PATH_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise integrity_error("RESOURCE_LOCK_SYMLINK_FORBIDDEN")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        raise integrity_error("RESOURCE_LOCK_FILE_TYPE_INVALID")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise integrity_error("RESOURCE_LOCK_OWNER_INVALID")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise integrity_error("RESOURCE_LOCK_MODE_INVALID")


def _validate_descriptor(descriptor: int) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise integrity_error("RESOURCE_LOCK_PATH_UNAVAILABLE") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise integrity_error("RESOURCE_LOCK_FILE_TYPE_INVALID")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise integrity_error("RESOURCE_LOCK_OWNER_INVALID")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise integrity_error("RESOURCE_LOCK_MODE_INVALID")
    return metadata


def _validate_owner(owner: LockOwner) -> None:
    if not isinstance(owner, LockOwner) or type(owner.pid) is not int or owner.pid < 1:
        raise integrity_error("RESOURCE_LOCK_OWNER_IDENTITY_INVALID")
    for value in (
        owner.process_start,
        owner.installation_id,
        owner.operation_id,
    ):
        if type(value) is not str or not value or len(value) > MAX_IDENTITY_TEXT:
            raise integrity_error("RESOURCE_LOCK_OWNER_IDENTITY_INVALID")


def _record_value(
    *,
    state: str,
    resource_kind: ResourceKind,
    resource_id_sha256: str,
    owner: LockOwner,
    recovery_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": LOCK_SCHEMA,
        "state": state,
        "resource_kind": resource_kind.value,
        "resource_id_sha256": resource_id_sha256,
        "owner": asdict(owner),
        "recovery_audit": recovery_audit,
    }


def _encode_record(value: dict[str, object]) -> bytes:
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > LOCK_RECORD_BYTES:
        raise integrity_error("RESOURCE_LOCK_RECORD_TOO_LARGE")
    return payload + b" " * (LOCK_RECORD_BYTES - len(payload))


def _decode_record(encoded: bytes) -> dict[str, object]:
    if len(encoded) != LOCK_RECORD_BYTES:
        raise integrity_error("RESOURCE_LOCK_SIZE_INVALID")
    payload = encoded.rstrip(b" ")
    if not payload.endswith(b"\n"):
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID")
    try:
        value = json.loads(payload[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID") from exc
    if _canonical_bytes(value) + b"\n" != payload:
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID")
    expected_fields = frozenset(
        {
            "schema",
            "state",
            "resource_kind",
            "resource_id_sha256",
            "owner",
            "recovery_audit",
        }
    )
    if not isinstance(value, dict) or frozenset(value) != expected_fields:
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID")
    if (
        value["schema"] != LOCK_SCHEMA
        or value["state"] not in {"claimed", "recovery_required", "released"}
        or value["resource_kind"] not in {kind.value for kind in ResourceKind}
        or type(value["resource_id_sha256"]) is not str
        or len(value["resource_id_sha256"]) != 64
        or not isinstance(value["owner"], dict)
        or frozenset(value["owner"])
        != frozenset({"pid", "process_start", "installation_id", "operation_id"})
    ):
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID")
    try:
        owner = LockOwner(**value["owner"])
    except TypeError as exc:
        raise integrity_error("RESOURCE_LOCK_SCHEMA_INVALID") from exc
    _validate_owner(owner)
    recovery_audit = value["recovery_audit"]
    if recovery_audit is not None:
        fields = frozenset(
            {
                "audit_id",
                "orphaned_record_sha256",
                "candidate_revision",
                "operation_receipt_id",
                "audited_at",
            }
        )
        if not isinstance(recovery_audit, dict) or frozenset(recovery_audit) != fields:
            raise integrity_error("RESOURCE_LOCK_RECOVERY_AUDIT_INVALID")
        candidate = recovery_audit["candidate_revision"]
        if (
            not isinstance(candidate, dict)
            or frozenset(candidate) != frozenset({"revision", "content_sha256"})
            or type(candidate["revision"]) is not int
            or candidate["revision"] < 0
            or type(candidate["content_sha256"]) is not str
            or len(candidate["content_sha256"]) != 64
        ):
            raise integrity_error("RESOURCE_LOCK_RECOVERY_AUDIT_INVALID")
        for field in (
            "audit_id",
            "orphaned_record_sha256",
            "operation_receipt_id",
            "audited_at",
        ):
            if type(recovery_audit[field]) is not str or not recovery_audit[field]:
                raise integrity_error("RESOURCE_LOCK_RECOVERY_AUDIT_INVALID")
    if value["state"] == "recovery_required" and recovery_audit is None:
        raise integrity_error("RESOURCE_LOCK_RECOVERY_AUDIT_INVALID")
    return value


def _audit_value(audit: RecoveryAuditV1) -> dict[str, object]:
    return {
        "audit_id": audit.audit_id,
        "orphaned_record_sha256": audit.orphaned_record_sha256,
        "candidate_revision": {
            "revision": audit.candidate_revision.revision,
            "content_sha256": audit.candidate_revision.content_sha256,
        },
        "operation_receipt_id": audit.operation_receipt_id,
        "audited_at": audit.audited_at,
    }


def _read_descriptor(descriptor: int) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = LOCK_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise integrity_error("RESOURCE_LOCK_READ_FAILED") from exc


def _write_descriptor(descriptor: int, encoded: bytes) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short resource lock write")
            offset += written
        os.ftruncate(descriptor, LOCK_RECORD_BYTES)
        os.fsync(descriptor)
    except OSError as exc:
        raise integrity_error("RESOURCE_LOCK_WRITE_FAILED") from exc


class ResourceLockManager:
    def __init__(self, resource_root: Path) -> None:
        if not isinstance(resource_root, Path) or not resource_root.is_absolute():
            raise integrity_error("RESOURCE_ROOT_MUST_BE_EXPLICIT_ABSOLUTE")
        self.resource_root = resource_root
        self.lock_directory = resource_root / "resource-locks-v1"
        self._held: dict[Path, tuple[int, ResourceLease]] = {}
        self._ensure_directory(resource_root)
        self._ensure_directory(self.lock_directory)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        _validate_no_follow_ancestors(path)
        if not path.exists():
            try:
                path.mkdir(mode=0o700, parents=True)
                os.chmod(path, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("RESOURCE_LOCK_DIRECTORY_CREATE_FAILED") from exc
        _validate_path(path, directory=True, mode=0o700)

    @staticmethod
    def _resource_digest(resource_id: str) -> str:
        if type(resource_id) is not str or not resource_id or len(resource_id) > 16 * 1024:
            raise integrity_error("RESOURCE_ID_INVALID")
        return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()

    def lock_path(self, resource_kind: ResourceKind, resource_id: str) -> Path:
        if not isinstance(resource_kind, ResourceKind):
            raise integrity_error("RESOURCE_KIND_INVALID")
        digest = self._resource_digest(resource_id)
        return self.lock_directory / f"{resource_kind.value}-{digest}.lock"

    def _open(self, path: Path) -> tuple[int, bool]:
        _validate_no_follow_ancestors(path)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(path, flags, 0o600), True
        except FileExistsError:
            flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                return os.open(path, flags), False
            except OSError as exc:
                raise integrity_error("RESOURCE_LOCK_OPEN_FAILED") from exc
        except OSError as exc:
            raise integrity_error("RESOURCE_LOCK_OPEN_FAILED") from exc

    @staticmethod
    def _unlock_close(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def claim(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        owner: LockOwner,
    ) -> ResourceLease:
        _validate_owner(owner)
        path = self.lock_path(resource_kind, resource_id)
        if path in self._held:
            raise resource_busy("RESOURCE_LOCK_HELD")
        descriptor, created = self._open(path)
        locked = False
        try:
            _validate_descriptor(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise resource_busy("RESOURCE_LOCK_HELD") from exc
                raise integrity_error("RESOURCE_LOCK_ACQUIRE_FAILED") from exc
            resource_digest = self._resource_digest(resource_id)
            recovery_audit = None
            if not created:
                existing_encoded = _read_descriptor(descriptor)
                existing = _decode_record(existing_encoded)
                if (
                    existing["resource_kind"] != resource_kind.value
                    or existing["resource_id_sha256"] != resource_digest
                ):
                    raise integrity_error("RESOURCE_LOCK_BINDING_INVALID")
                if existing["state"] in {"claimed", "recovery_required"}:
                    raise integrity_error("RESOURCE_LOCK_RECOVERY_AUDIT_REQUIRED")
                recovery_audit = existing["recovery_audit"]
            claimed = _record_value(
                state="claimed",
                resource_kind=resource_kind,
                resource_id_sha256=resource_digest,
                owner=owner,
                recovery_audit=recovery_audit,
            )
            encoded = _encode_record(claimed)
            _write_descriptor(descriptor, encoded)
            if created:
                directory_descriptor = os.open(self.lock_directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            lease = ResourceLease(
                resource_kind=resource_kind,
                resource_id=resource_id,
                owner=owner,
                lock_path=path,
                claimed_record_sha256=hashlib.sha256(encoded).hexdigest(),
            )
            self._held[path] = (descriptor, lease)
            return lease
        except Exception:
            if locked:
                self._unlock_close(descriptor)
            else:
                os.close(descriptor)
            raise

    def release(self, lease: ResourceLease) -> None:
        self.assert_held(lease)
        descriptor, authoritative = self._held[lease.lock_path]
        current = _decode_record(_read_descriptor(descriptor))
        released = _record_value(
            state="released",
            resource_kind=authoritative.resource_kind,
            resource_id_sha256=self._resource_digest(authoritative.resource_id),
            owner=authoritative.owner,
            recovery_audit=current["recovery_audit"],
        )
        _write_descriptor(descriptor, _encode_record(released))
        del self._held[lease.lock_path]
        self._unlock_close(descriptor)

    def assert_held(self, lease: ResourceLease) -> None:
        if not isinstance(lease, ResourceLease):
            raise integrity_error("RESOURCE_LEASE_INVALID")
        held = self._held.get(lease.lock_path)
        if held is None or held[1] != lease:
            raise integrity_error("RESOURCE_LEASE_OWNER_MISMATCH")
        descriptor, authoritative = held
        encoded = _read_descriptor(descriptor)
        record = _decode_record(encoded)
        if (
            hashlib.sha256(encoded).hexdigest()
            != authoritative.claimed_record_sha256
            or record["state"] != "claimed"
            or record["owner"] != asdict(authoritative.owner)
        ):
            raise integrity_error("RESOURCE_LEASE_RECORD_MISMATCH")

    def _open_existing_locked(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> tuple[int, Path, bytes, dict[str, object]]:
        path = self.lock_path(resource_kind, resource_id)
        _validate_no_follow_ancestors(path)
        if not path.exists():
            raise conflict("RESOURCE_LOCK_NOT_FOUND")
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise integrity_error("RESOURCE_LOCK_OPEN_FAILED") from exc
        locked = False
        try:
            _validate_descriptor(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise resource_busy("RESOURCE_LOCK_HELD") from exc
                raise integrity_error("RESOURCE_LOCK_ACQUIRE_FAILED") from exc
            encoded = _read_descriptor(descriptor)
            record = _decode_record(encoded)
            if (
                record["resource_kind"] != resource_kind.value
                or record["resource_id_sha256"]
                != self._resource_digest(resource_id)
            ):
                raise integrity_error("RESOURCE_LOCK_BINDING_INVALID")
            return descriptor, path, encoded, record
        except Exception:
            if locked:
                self._unlock_close(descriptor)
            else:
                os.close(descriptor)
            raise

    @staticmethod
    def _inspection(
        *,
        resource_kind: ResourceKind,
        path: Path,
        encoded: bytes,
        record: dict[str, object],
    ) -> LockInspection:
        recovery_audit = record["recovery_audit"]
        return LockInspection(
            resource_kind=resource_kind,
            resource_id_sha256=record["resource_id_sha256"],
            state=record["state"],
            owner=LockOwner(**record["owner"]),
            lock_path=path,
            record_sha256=hashlib.sha256(encoded).hexdigest(),
            recovery_audit_id=(
                None if recovery_audit is None else recovery_audit["audit_id"]
            ),
        )

    def inspect(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> LockInspection:
        descriptor, path, encoded, record = self._open_existing_locked(
            resource_kind,
            resource_id,
        )
        try:
            return self._inspection(
                resource_kind=resource_kind,
                path=path,
                encoded=encoded,
                record=record,
            )
        finally:
            self._unlock_close(descriptor)

    def mark_recovery_required(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        *,
        audit: RecoveryAuditV1,
    ) -> LockInspection:
        if not isinstance(audit, RecoveryAuditV1):
            raise integrity_error("RESOURCE_RECOVERY_AUDIT_INVALID")
        descriptor, path, encoded, record = self._open_existing_locked(
            resource_kind,
            resource_id,
        )
        try:
            if (
                record["state"] != "claimed"
                or record["recovery_audit"] is not None
                or audit.resource_kind is not resource_kind
                or audit.resource_id_sha256 != record["resource_id_sha256"]
                or audit.orphaned_owner != LockOwner(**record["owner"])
                or audit.orphaned_record_sha256
                != hashlib.sha256(encoded).hexdigest()
            ):
                raise conflict("RESOURCE_RECOVERY_AUDIT_STALE")
            updated = _record_value(
                state="recovery_required",
                resource_kind=resource_kind,
                resource_id_sha256=record["resource_id_sha256"],
                owner=audit.orphaned_owner,
                recovery_audit=_audit_value(audit),
            )
            updated_encoded = _encode_record(updated)
            _write_descriptor(descriptor, updated_encoded)
            return self._inspection(
                resource_kind=resource_kind,
                path=path,
                encoded=updated_encoded,
                record=updated,
            )
        finally:
            self._unlock_close(descriptor)

    def reclaim(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        *,
        audit: RecoveryAuditV1,
        new_owner: LockOwner,
    ) -> ResourceLease:
        if not isinstance(audit, RecoveryAuditV1):
            raise integrity_error("RESOURCE_RECOVERY_AUDIT_INVALID")
        _validate_owner(new_owner)
        path = self.lock_path(resource_kind, resource_id)
        if path in self._held:
            raise resource_busy("RESOURCE_LOCK_HELD")
        descriptor, path, encoded, record = self._open_existing_locked(
            resource_kind,
            resource_id,
        )
        try:
            if (
                record["state"] != "recovery_required"
                or record["owner"] != asdict(audit.orphaned_owner)
                or record["recovery_audit"] != _audit_value(audit)
            ):
                raise conflict("RESOURCE_RECOVERY_AUDIT_STALE")
            claimed = _record_value(
                state="claimed",
                resource_kind=resource_kind,
                resource_id_sha256=record["resource_id_sha256"],
                owner=new_owner,
                recovery_audit=record["recovery_audit"],
            )
            claimed_encoded = _encode_record(claimed)
            _write_descriptor(descriptor, claimed_encoded)
            lease = ResourceLease(
                resource_kind=resource_kind,
                resource_id=resource_id,
                owner=new_owner,
                lock_path=path,
                claimed_record_sha256=hashlib.sha256(claimed_encoded).hexdigest(),
            )
            self._held[path] = (descriptor, lease)
            return lease
        except Exception:
            self._unlock_close(descriptor)
            raise
