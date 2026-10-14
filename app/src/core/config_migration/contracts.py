"""Closed, Qt-free contracts for Phase 7 configuration migration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class ConflictDecision(str, Enum):
    USE_LEGACY = "use_legacy"
    USE_CANONICAL = "use_canonical"
    EXPORT_BOTH_AND_ABORT = "export_both_and_abort"


class ConfigMigrationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        exit_code: int,
        reason_code: str,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.reason_code = reason_code
        self.details = {"reason_code": reason_code}


def integrity_error(reason_code: str) -> ConfigMigrationError:
    return ConfigMigrationError(
        "INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )


def migration_conflict(reason_code: str) -> ConfigMigrationError:
    return ConfigMigrationError(
        "CONFIG_MIGRATION_CONFLICT",
        exit_code=21,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class ConfigRoots:
    home_root: Path
    application_support_root: Path
    resource_root: Path


@dataclass(frozen=True)
class ConfigFingerprint:
    exists: bool
    valid: bool
    content_sha256: str | None
    semantic_sha256: str | None
    size_bytes: int
    schema_version: int | None


@dataclass(frozen=True)
class ConfigRevision:
    revision: int
    content_sha256: str
    semantic_sha256: str


@dataclass(frozen=True)
class SharedDataReferences:
    transcripts: Path
    voices: Path
    models: Path
    jobs: Path


@dataclass(frozen=True)
class ConfigSnapshot:
    revision: ConfigRevision
    values: Mapping[str, object]


@dataclass(frozen=True)
class MigrationJournalV1:
    migration_id: str
    state: str
    source_fingerprint: ConfigFingerprint
    target_fingerprint: ConfigFingerprint
    decision: ConflictDecision | None
    backup_path: str | None
    backup_sha256: str | None
    canonical_revision: ConfigRevision | None
    started_at: str
    compatibility_version: int


@dataclass(frozen=True)
class MigrationReceiptV1:
    receipt_id: str
    outcome: str
    source_fingerprint: ConfigFingerprint
    target_fingerprint: ConfigFingerprint
    canonical_revision: ConfigRevision | None
    journal_path: str
    backup_path: str | None


@dataclass(frozen=True)
class RollbackReceiptV1:
    receipt_id: str
    migration_id: str
    restored_fingerprint: ConfigFingerprint
    backup_sha256: str
    canonical_revision: ConfigRevision | None
    rolled_back_at: str
