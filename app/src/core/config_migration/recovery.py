"""Journal-bound recovery for interrupted configuration migration."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re

from ..resource_control.contracts import LockOwner, ResourceKind
from ..resource_control.locks import ResourceLockManager
from .contracts import (
    ConfigFingerprint,
    ConfigRevision,
    ConflictDecision,
    MigrationJournalV1,
    RollbackReceiptV1,
    integrity_error,
    migration_conflict,
)
from .migration import (
    JOURNAL_SCHEMA,
    MIGRATION_COMPATIBILITY_VERSION,
)
from .paths import ConfigPaths
from .repository import (
    CONFIG_RESOURCE_ID,
    ConfigRepository,
    atomic_write_owner_file,
    read_owner_file,
)


_MAX_JOURNAL_BYTES = 1024 * 1024
_JOURNAL_SCHEMA_PATTERN = re.compile(
    r"^(?P<schema_id>[a-z][a-z0-9.-]{0,126})/v(?P<schema_revision>0|[1-9][0-9]{0,8})$"
)
_JOURNAL_SCHEMA_ID, _JOURNAL_SCHEMA_REVISION_TEXT = JOURNAL_SCHEMA.rsplit(
    "/v", 1
)
_JOURNAL_SCHEMA_REVISION = int(_JOURNAL_SCHEMA_REVISION_TEXT)


def _nonempty(value: object, reason_code: str) -> str:
    if type(value) is not str or not value or len(value) > 16 * 1024:
        raise integrity_error(reason_code)
    return value


def _fingerprint(value: object) -> ConfigFingerprint:
    fields = {
        "exists",
        "valid",
        "content_sha256",
        "semantic_sha256",
        "size_bytes",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise integrity_error("CONFIG_JOURNAL_FINGERPRINT_INVALID")
    fingerprint = ConfigFingerprint(**value)
    if (
        type(fingerprint.exists) is not bool
        or type(fingerprint.valid) is not bool
        or type(fingerprint.size_bytes) is not int
        or fingerprint.size_bytes < 0
    ):
        raise integrity_error("CONFIG_JOURNAL_FINGERPRINT_INVALID")
    if fingerprint.exists:
        for digest in (
            fingerprint.content_sha256,
            fingerprint.semantic_sha256 if fingerprint.valid else None,
        ):
            if digest is None and not fingerprint.valid:
                continue
            if type(digest) is not str or len(digest) != 64:
                raise integrity_error("CONFIG_JOURNAL_FINGERPRINT_INVALID")
            try:
                bytes.fromhex(digest)
            except ValueError as exc:
                raise integrity_error("CONFIG_JOURNAL_FINGERPRINT_INVALID") from exc
    return fingerprint


def _revision(value: object) -> ConfigRevision | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "revision",
        "content_sha256",
        "semantic_sha256",
    }:
        raise integrity_error("CONFIG_JOURNAL_REVISION_INVALID")
    revision = ConfigRevision(**value)
    if type(revision.revision) is not int or revision.revision < 1:
        raise integrity_error("CONFIG_JOURNAL_REVISION_INVALID")
    return revision


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise integrity_error("CONFIG_JOURNAL_INVALID") from exc


def _compatibility_header(value: object) -> tuple[str, int, int]:
    if not isinstance(value, dict):
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    schema = value.get("schema")
    journal = value.get("journal")
    if type(schema) is not str or len(schema) > 160 or not isinstance(journal, dict):
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    match = _JOURNAL_SCHEMA_PATTERN.fullmatch(schema)
    if match is None:
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    compatibility_version = journal.get("compatibility_version")
    if (
        type(compatibility_version) is not int
        or compatibility_version < 0
        or compatibility_version > 2**31 - 1
    ):
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    schema_id = match.group("schema_id")
    schema_revision = int(match.group("schema_revision"))
    return schema_id, schema_revision, compatibility_version


def _decode_journal(raw: bytes, migration_id: str) -> tuple[MigrationJournalV1, dict[str, object]]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_JOURNAL_BYTES:
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise integrity_error("CONFIG_JOURNAL_INVALID") from exc
    if _canonical_bytes(value) != raw:
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    schema_id, schema_revision, compatibility_version = _compatibility_header(
        value
    )
    if (
        schema_id != _JOURNAL_SCHEMA_ID
        or schema_revision != _JOURNAL_SCHEMA_REVISION
        or compatibility_version != MIGRATION_COMPATIBILITY_VERSION
    ):
        raise integrity_error("CONFIG_ROLLBACK_VERSION_INCOMPATIBLE")
    if set(value) != {"schema", "journal", "receipt"}:
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    journal_value = value["journal"]
    expected_fields = {
        "migration_id",
        "state",
        "source_fingerprint",
        "target_fingerprint",
        "decision",
        "backup_path",
        "backup_sha256",
        "canonical_revision",
        "started_at",
        "compatibility_version",
    }
    if set(journal_value) != expected_fields:
        raise integrity_error("CONFIG_JOURNAL_INVALID")
    if journal_value["migration_id"] != migration_id:
        raise integrity_error("CONFIG_JOURNAL_MIGRATION_MISMATCH")
    if journal_value["state"] != "prepared":
        raise migration_conflict("CONFIG_JOURNAL_NOT_RECOVERABLE")
    raw_decision = journal_value["decision"]
    if raw_decision is None:
        decision = None
    else:
        try:
            decision = ConflictDecision(raw_decision)
        except (TypeError, ValueError) as exc:
            raise integrity_error("CONFIG_JOURNAL_DECISION_INVALID") from exc
    journal = MigrationJournalV1(
        migration_id=migration_id,
        state="prepared",
        source_fingerprint=_fingerprint(journal_value["source_fingerprint"]),
        target_fingerprint=_fingerprint(journal_value["target_fingerprint"]),
        decision=decision,
        backup_path=journal_value["backup_path"],
        backup_sha256=journal_value["backup_sha256"],
        canonical_revision=_revision(journal_value["canonical_revision"]),
        started_at=journal_value["started_at"],
        compatibility_version=journal_value["compatibility_version"],
    )
    return journal, value


def _remove_owner_file(path: Path) -> None:
    if not path.exists():
        return
    read_owner_file(path)
    try:
        path.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise integrity_error("CONFIG_ROLLBACK_REMOVE_FAILED") from exc


class ConfigRecoveryService:
    def __init__(
        self,
        paths: ConfigPaths,
        *,
        repository: ConfigRepository,
        lock_manager: ResourceLockManager,
    ) -> None:
        if (
            not isinstance(paths, ConfigPaths)
            or not isinstance(repository, ConfigRepository)
            or repository.paths != paths
            or repository.lock_manager is not lock_manager
        ):
            raise TypeError("recovery requires shared paths, repository and lock")
        self.paths = paths
        self.repository = repository
        self.lock_manager = lock_manager

    def recover(
        self,
        *,
        owner: LockOwner,
        migration_id: str,
        recovered_at: str,
    ) -> RollbackReceiptV1:
        if not isinstance(owner, LockOwner):
            raise integrity_error("CONFIG_RECOVERY_OWNER_INVALID")
        _nonempty(migration_id, "CONFIG_MIGRATION_ID_INVALID")
        _nonempty(recovered_at, "CONFIG_RECOVERY_TIME_INVALID")
        journal_path = self.paths.journal_path(migration_id)
        raw_journal = read_owner_file(journal_path)
        if raw_journal is None:
            raise integrity_error("CONFIG_JOURNAL_NOT_FOUND")
        journal, journal_value = _decode_journal(raw_journal, migration_id)
        lease = self.lock_manager.claim(
            ResourceKind.CONFIG,
            CONFIG_RESOURCE_ID,
            owner,
        )
        try:
            backup_path = self._bound_backup_path(journal)
            backup = read_owner_file(backup_path)
            if backup is None or hashlib.sha256(backup).hexdigest() != journal.backup_sha256:
                raise integrity_error("CONFIG_ROLLBACK_BACKUP_MISMATCH")
            source = self.repository.inspect(self.paths.legacy_config)
            if source.fingerprint != journal.source_fingerprint:
                raise migration_conflict("CONFIG_ROLLBACK_SOURCE_CHANGED")
            canonical = self.repository.inspect(self.paths.canonical_config)
            revision = None
            if journal.decision is ConflictDecision.USE_CANONICAL:
                if canonical.fingerprint != journal.target_fingerprint:
                    raise migration_conflict("CONFIG_ROLLBACK_TARGET_CHANGED")
                restored = canonical.fingerprint
            elif journal.target_fingerprint.exists:
                if hashlib.sha256(backup).hexdigest() != journal.target_fingerprint.content_sha256:
                    raise integrity_error("CONFIG_ROLLBACK_BACKUP_MISMATCH")
                revision = self.repository.commit_canonical(
                    lease,
                    expected=canonical.fingerprint,
                    payload=backup,
                )
                restored = self.repository.inspect(
                    self.paths.canonical_config
                ).fingerprint
            else:
                if canonical.fingerprint.exists and (
                    not canonical.fingerprint.valid
                    or canonical.fingerprint.semantic_sha256
                    != journal.source_fingerprint.semantic_sha256
                ):
                    raise migration_conflict("CONFIG_ROLLBACK_TARGET_CHANGED")
                _remove_owner_file(self.paths.canonical_config)
                restored = source.fingerprint

            receipt = RollbackReceiptV1(
                receipt_id=f"{migration_id}:rollback",
                migration_id=migration_id,
                restored_fingerprint=restored,
                backup_sha256=journal.backup_sha256 or "",
                canonical_revision=revision,
                rolled_back_at=recovered_at,
            )
            journal_value["journal"]["state"] = "rolled_back"
            journal_value["receipt"] = {
                "receipt_id": receipt.receipt_id,
                "migration_id": receipt.migration_id,
                "restored_fingerprint": asdict(receipt.restored_fingerprint),
                "backup_sha256": receipt.backup_sha256,
                "canonical_revision": (
                    asdict(receipt.canonical_revision)
                    if receipt.canonical_revision is not None
                    else None
                ),
                "rolled_back_at": receipt.rolled_back_at,
            }
            atomic_write_owner_file(journal_path, _canonical_bytes(journal_value))
            return receipt
        finally:
            self.lock_manager.release(lease)

    def _bound_backup_path(self, journal: MigrationJournalV1) -> Path:
        if (
            type(journal.backup_path) is not str
            or type(journal.backup_sha256) is not str
            or len(journal.backup_sha256) != 64
        ):
            raise integrity_error("CONFIG_ROLLBACK_BACKUP_BINDING_INVALID")
        backup_path = Path(journal.backup_path)
        expected = self.paths.backup_path(
            journal.migration_id,
            journal.backup_sha256,
        )
        if backup_path != expected or backup_path.parent != self.paths.backup_directory:
            raise integrity_error("CONFIG_ROLLBACK_BACKUP_BINDING_INVALID")
        return backup_path
