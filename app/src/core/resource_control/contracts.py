"""Closed, Qt-free contracts for Phase 6 resource control."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ResourceKind(str, Enum):
    CONFIG = "config"
    TRANSCRIPT_FOLDER = "transcript-folder"
    VOICE = "voice"
    JOB = "job"
    MODEL = "model"
    TTS_ENGINE = "tts-engine"
    FEISHU_TARGET = "feishu-target"
    UPDATE = "update"


class ResourceControlError(RuntimeError):
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


def integrity_error(reason_code: str) -> ResourceControlError:
    return ResourceControlError(
        "INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )


def resource_busy(reason_code: str) -> ResourceControlError:
    return ResourceControlError(
        "RESOURCE_BUSY",
        exit_code=22,
        reason_code=reason_code,
    )


def conflict(reason_code: str) -> ResourceControlError:
    return ResourceControlError(
        "CONFLICT",
        exit_code=21,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class LockOwner:
    pid: int
    process_start: str
    installation_id: str
    operation_id: str


@dataclass(frozen=True)
class LeaseToken:
    job_id: str
    lease_epoch: int
    worker_instance_id: str


@dataclass(frozen=True)
class CASRevision:
    revision: int
    content_sha256: str


@dataclass(frozen=True)
class ResourceLease:
    resource_kind: ResourceKind
    resource_id: str
    owner: LockOwner
    lock_path: Path
    claimed_record_sha256: str


@dataclass(frozen=True)
class LockInspection:
    resource_kind: ResourceKind
    resource_id_sha256: str
    state: str
    owner: LockOwner
    lock_path: Path
    record_sha256: str
    recovery_audit_id: str | None


@dataclass(frozen=True)
class RecoveryAuditV1:
    audit_id: str
    resource_kind: ResourceKind
    resource_id_sha256: str
    orphaned_owner: LockOwner
    orphaned_record_sha256: str
    candidate_revision: CASRevision
    operation_receipt_id: str
    audited_at: str
