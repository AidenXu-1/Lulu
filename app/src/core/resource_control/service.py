"""Resource claim, release and owner-loss recovery audit service."""

from __future__ import annotations

import hashlib
from typing import Protocol

from ..job_ledger.service import JobControlService
from .cas import CASRecord, ResourceCASStore
from .contracts import (
    CASRevision,
    LeaseToken,
    LockOwner,
    RecoveryAuditV1,
    ResourceKind,
    ResourceLease,
    conflict,
    integrity_error,
    resource_busy,
)
from .locks import ResourceLockManager


class ProcessIdentityProbe(Protocol):
    def is_alive(self, owner: LockOwner) -> bool: ...


def _nonempty(value: object, reason_code: str) -> str:
    if type(value) is not str or not value or len(value) > 16 * 1024:
        raise integrity_error(reason_code)
    return value


def _validate_candidate(candidate: CASRevision) -> None:
    if (
        not isinstance(candidate, CASRevision)
        or type(candidate.revision) is not int
        or candidate.revision < 0
        or type(candidate.content_sha256) is not str
        or len(candidate.content_sha256) != 64
    ):
        raise integrity_error("RESOURCE_RECOVERY_CANDIDATE_INVALID")
    try:
        bytes.fromhex(candidate.content_sha256)
    except ValueError as exc:
        raise integrity_error("RESOURCE_RECOVERY_CANDIDATE_INVALID") from exc


class ResourceControlService:
    def __init__(
        self,
        lock_manager: ResourceLockManager,
        *,
        process_probe: ProcessIdentityProbe,
        cas_store: ResourceCASStore | None = None,
        job_service: JobControlService | None = None,
    ) -> None:
        if not isinstance(lock_manager, ResourceLockManager):
            raise TypeError("ResourceControlService requires ResourceLockManager")
        if process_probe is None or not callable(
            getattr(process_probe, "is_alive", None)
        ):
            raise TypeError("ResourceControlService requires process identity probe")
        self._locks = lock_manager
        self._process_probe = process_probe
        if cas_store is not None and (
            not isinstance(cas_store, ResourceCASStore)
            or cas_store.lock_manager is not lock_manager
        ):
            raise TypeError("CAS store must share ResourceLockManager")
        if job_service is not None and not isinstance(job_service, JobControlService):
            raise TypeError("job_service must be JobControlService")
        self._cas = cas_store
        self._jobs = job_service

    def claim(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        owner: LockOwner,
    ) -> ResourceLease:
        return self._locks.claim(resource_kind, resource_id, owner)

    def release(self, lease: ResourceLease) -> None:
        self._locks.release(lease)

    def commit_worker_file(
        self,
        lease: ResourceLease,
        *,
        lease_token: LeaseToken,
        expected: CASRevision,
        content: bytes,
    ) -> CASRecord:
        if self._cas is None or self._jobs is None:
            raise integrity_error("RESOURCE_ARBITRATION_NOT_CONFIGURED")
        if (
            not isinstance(lease, ResourceLease)
            or lease.resource_kind is not ResourceKind.JOB
            or not isinstance(lease_token, LeaseToken)
            or lease.resource_id != lease_token.job_id
        ):
            raise integrity_error("RESOURCE_JOB_LEASE_BINDING_INVALID")
        self._jobs.verify_worker_lease(
            lease_token.job_id,
            lease_token=lease_token,
            mutation_kind="file",
        )
        return self._cas.write(
            lease,
            expected=expected,
            content=content,
        )

    def verify_worker_effect(
        self,
        job_id: str,
        *,
        lease_token: LeaseToken,
    ) -> None:
        if self._jobs is None:
            raise integrity_error("RESOURCE_ARBITRATION_NOT_CONFIGURED")
        self._jobs.verify_worker_lease(
            job_id,
            lease_token=lease_token,
            mutation_kind="effect_receipt",
        )

    def _owner_is_alive(self, owner: LockOwner) -> bool:
        try:
            alive = self._process_probe.is_alive(owner)
        except Exception as exc:  # noqa: BLE001 - injected platform seam
            raise integrity_error("RESOURCE_OWNER_PROBE_UNAVAILABLE") from exc
        if type(alive) is not bool:
            raise integrity_error("RESOURCE_OWNER_PROBE_INVALID")
        return alive

    def audit_recovery(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        *,
        audit_id: str,
        candidate_revision: CASRevision,
        operation_receipt_id: str,
        audited_at: str,
    ) -> RecoveryAuditV1:
        _nonempty(audit_id, "RESOURCE_RECOVERY_AUDIT_ID_INVALID")
        _nonempty(
            operation_receipt_id,
            "RESOURCE_RECOVERY_RECEIPT_INVALID",
        )
        _nonempty(audited_at, "RESOURCE_RECOVERY_TIME_INVALID")
        _validate_candidate(candidate_revision)
        inspection = self._locks.inspect(resource_kind, resource_id)
        if inspection.state != "claimed":
            raise conflict("RESOURCE_NOT_ORPHANED_CLAIM")
        if self._owner_is_alive(inspection.owner):
            raise resource_busy("RESOURCE_OWNER_STILL_LIVE")
        audit = RecoveryAuditV1(
            audit_id=audit_id,
            resource_kind=resource_kind,
            resource_id_sha256=hashlib.sha256(
                resource_id.encode("utf-8")
            ).hexdigest(),
            orphaned_owner=inspection.owner,
            orphaned_record_sha256=inspection.record_sha256,
            candidate_revision=candidate_revision,
            operation_receipt_id=operation_receipt_id,
            audited_at=audited_at,
        )
        self._locks.mark_recovery_required(
            resource_kind,
            resource_id,
            audit=audit,
        )
        return audit

    def reclaim(
        self,
        resource_kind: ResourceKind,
        resource_id: str,
        *,
        audit: RecoveryAuditV1,
        candidate_revision: CASRevision,
        operation_receipt_id: str,
        new_owner: LockOwner,
    ) -> ResourceLease:
        if not isinstance(audit, RecoveryAuditV1):
            raise integrity_error("RESOURCE_RECOVERY_AUDIT_INVALID")
        _validate_candidate(candidate_revision)
        _nonempty(
            operation_receipt_id,
            "RESOURCE_RECOVERY_RECEIPT_INVALID",
        )
        resource_digest = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
        if (
            audit.resource_kind is not resource_kind
            or audit.resource_id_sha256 != resource_digest
            or audit.candidate_revision != candidate_revision
            or audit.operation_receipt_id != operation_receipt_id
        ):
            raise conflict("RESOURCE_RECOVERY_BINDING_MISMATCH")
        if self._owner_is_alive(audit.orphaned_owner):
            raise resource_busy("RESOURCE_OWNER_STILL_LIVE")
        return self._locks.reclaim(
            resource_kind,
            resource_id,
            audit=audit,
            new_owner=new_owner,
        )
