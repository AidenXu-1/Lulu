"""Atomic revision-and-content-hash CAS under a held resource lease."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from .contracts import (
    CASRevision,
    ResourceKind,
    ResourceLease,
    conflict,
    integrity_error,
)
from .locks import ResourceLockManager


CAS_SCHEMA = "lulu.resource-cas/v1"
EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_CONTENT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class CASRecord:
    resource_kind: ResourceKind
    resource_id_sha256: str
    revision: CASRevision
    content: bytes
    record_path: Path


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
        raise integrity_error("RESOURCE_CAS_VALUE_INVALID") from exc


def _validate_no_follow_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("RESOURCE_CAS_SYMLINK_FORBIDDEN")


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise integrity_error("RESOURCE_CAS_PATH_UNAVAILABLE") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise integrity_error("RESOURCE_CAS_DIRECTORY_INVALID")


def _validate_revision(value: CASRevision) -> None:
    if (
        not isinstance(value, CASRevision)
        or type(value.revision) is not int
        or value.revision < 0
        or type(value.content_sha256) is not str
        or len(value.content_sha256) != 64
    ):
        raise integrity_error("RESOURCE_CAS_REVISION_INVALID")
    try:
        bytes.fromhex(value.content_sha256)
    except ValueError as exc:
        raise integrity_error("RESOURCE_CAS_REVISION_INVALID") from exc


class ResourceCASStore:
    def __init__(
        self,
        resource_root: Path,
        lock_manager: ResourceLockManager,
    ) -> None:
        if (
            not isinstance(resource_root, Path)
            or not resource_root.is_absolute()
            or not isinstance(lock_manager, ResourceLockManager)
            or lock_manager.resource_root != resource_root
        ):
            raise integrity_error("RESOURCE_CAS_ROOT_MISMATCH")
        self.resource_root = resource_root
        self.lock_manager = lock_manager
        self.record_directory = resource_root / "resource-cas-v1"
        _validate_no_follow_ancestors(self.record_directory)
        if not self.record_directory.exists():
            try:
                self.record_directory.mkdir(mode=0o700)
                os.chmod(self.record_directory, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("RESOURCE_CAS_DIRECTORY_CREATE_FAILED") from exc
        _validate_directory(self.record_directory)

    @staticmethod
    def _resource_digest(resource_id: str) -> str:
        if type(resource_id) is not str or not resource_id or len(resource_id) > 16 * 1024:
            raise integrity_error("RESOURCE_ID_INVALID")
        return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()

    def record_path(self, resource_kind: ResourceKind, resource_id: str) -> Path:
        if not isinstance(resource_kind, ResourceKind):
            raise integrity_error("RESOURCE_KIND_INVALID")
        digest = self._resource_digest(resource_id)
        return self.record_directory / f"{resource_kind.value}-{digest}.json"

    @staticmethod
    def _decode_record(
        encoded: bytes,
        *,
        resource_kind: ResourceKind,
        resource_digest: str,
        record_path: Path,
    ) -> CASRecord:
        if not encoded.endswith(b"\n"):
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID")
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID") from exc
        if _canonical_bytes(value) + b"\n" != encoded:
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID")
        fields = frozenset(
            {
                "schema",
                "resource_kind",
                "resource_id_sha256",
                "revision",
                "content_sha256",
                "content_base64",
            }
        )
        if not isinstance(value, dict) or frozenset(value) != fields:
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID")
        revision = CASRevision(value["revision"], value["content_sha256"])
        _validate_revision(revision)
        if (
            value["schema"] != CAS_SCHEMA
            or value["resource_kind"] != resource_kind.value
            or value["resource_id_sha256"] != resource_digest
            or revision.revision < 1
            or type(value["content_base64"]) is not str
        ):
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID")
        try:
            content = base64.b64decode(value["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise integrity_error("RESOURCE_CAS_RECORD_INVALID") from exc
        if (
            len(content) > MAX_CONTENT_BYTES
            or base64.b64encode(content).decode("ascii") != value["content_base64"]
            or hashlib.sha256(content).hexdigest() != revision.content_sha256
        ):
            raise integrity_error("RESOURCE_CAS_CONTENT_INVALID")
        return CASRecord(
            resource_kind=resource_kind,
            resource_id_sha256=resource_digest,
            revision=revision,
            content=content,
            record_path=record_path,
        )

    def read(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> CASRecord | None:
        path = self.record_path(resource_kind, resource_id)
        _validate_no_follow_ancestors(path)
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
                    or metadata.st_size > MAX_RECORD_BYTES
                    or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                ):
                    raise integrity_error("RESOURCE_CAS_FILE_INVALID")
                chunks: list[bytes] = []
                remaining = MAX_RECORD_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                encoded = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise integrity_error("RESOURCE_CAS_READ_FAILED") from exc
        if len(encoded) > MAX_RECORD_BYTES:
            raise integrity_error("RESOURCE_CAS_FILE_INVALID")
        return self._decode_record(
            encoded,
            resource_kind=resource_kind,
            resource_digest=self._resource_digest(resource_id),
            record_path=path,
        )

    def _atomic_write(self, path: Path, encoded: bytes) -> None:
        _validate_no_follow_ancestors(path)
        _validate_directory(self.record_directory)
        name = f".cas.{secrets.token_hex(16)}.tmp"
        temporary_path = self.record_directory / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(temporary_path, flags, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("short CAS write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_path, path)
            os.chmod(path, 0o600, follow_symlinks=False)
            directory_descriptor = os.open(self.record_directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise integrity_error("RESOURCE_CAS_WRITE_FAILED") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def write(
        self,
        lease: ResourceLease,
        *,
        expected: CASRevision,
        content: bytes,
    ) -> CASRecord:
        self.lock_manager.assert_held(lease)
        _validate_revision(expected)
        if type(content) is not bytes or len(content) > MAX_CONTENT_BYTES:
            raise integrity_error("RESOURCE_CAS_CONTENT_INVALID")
        current = self.read(lease.resource_kind, lease.resource_id)
        authoritative = (
            CASRevision(0, EMPTY_CONTENT_SHA256)
            if current is None
            else current.revision
        )
        if expected != authoritative:
            raise conflict("RESOURCE_CAS_CONFLICT")
        content_sha256 = hashlib.sha256(content).hexdigest()
        value = {
            "schema": CAS_SCHEMA,
            "resource_kind": lease.resource_kind.value,
            "resource_id_sha256": self._resource_digest(lease.resource_id),
            "revision": authoritative.revision + 1,
            "content_sha256": content_sha256,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        encoded = _canonical_bytes(value) + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise integrity_error("RESOURCE_CAS_RECORD_TOO_LARGE")
        path = self.record_path(lease.resource_kind, lease.resource_id)
        self._atomic_write(path, encoded)
        readback = self.read(lease.resource_kind, lease.resource_id)
        if (
            readback is None
            or readback.revision
            != CASRevision(authoritative.revision + 1, content_sha256)
            or readback.content != content
        ):
            raise integrity_error("RESOURCE_CAS_READBACK_MISMATCH")
        return readback
