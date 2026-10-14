"""Closed, Qt-free DTOs for general job ledger v1."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Mapping

from ..resource_control.contracts import LeaseToken


JOB_LEDGER_SCHEMA = "lulu.job-ledger/v1"
JOB_LEDGER_SCHEMA_VERSION = 1
JOB_EVENT_SCHEMA = "lulu.job-event/v1"
JOB_RESULT_SCHEMA = "lulu.job-result/v1"
OPERATION_RECEIPT_SCHEMA = "lulu.operation-receipt/v1"
MIGRATION_RECEIPT_SCHEMA = "lulu.pending-migration-receipt/v1"


class JobState(str, Enum):
    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    CANCEL_REQUESTED = "cancel_requested"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.PARTIAL,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)

ALLOWED_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.PREPARED: frozenset(
        {JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.QUEUED: frozenset(
        {JobState.RUNNING, JobState.CANCEL_REQUESTED, JobState.FAILED}
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.NEEDS_INPUT,
            JobState.CANCEL_REQUESTED,
            JobState.RECOVERY_REQUIRED,
            JobState.SUCCEEDED,
            JobState.PARTIAL,
            JobState.FAILED,
        }
    ),
    JobState.NEEDS_INPUT: frozenset(
        {JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.CANCEL_REQUESTED: frozenset(
        {JobState.CANCELLED, JobState.RECOVERY_REQUIRED, JobState.FAILED}
    ),
    JobState.RECOVERY_REQUIRED: frozenset(
        {JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.PARTIAL: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return deepcopy(value)


class JobLedgerError(RuntimeError):
    """Stable fail-closed error emitted by Phase 5 Core seams."""

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


def integrity_error(reason_code: str) -> JobLedgerError:
    return JobLedgerError(
        "INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )


def conflict(reason_code: str) -> JobLedgerError:
    return JobLedgerError("CONFLICT", exit_code=21, reason_code=reason_code)


def resource_busy(reason_code: str) -> JobLedgerError:
    return JobLedgerError(
        "RESOURCE_BUSY",
        exit_code=22,
        reason_code=reason_code,
    )


def migration_error(reason_code: str) -> JobLedgerError:
    return JobLedgerError(
        "MIGRATION_INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class OperationReceiptV1:
    operation_receipt_id: str
    job_id: str
    attempt_id: str
    lineage_id: str
    installation_id: str
    canonical_operation_id: str
    effect_profile: str
    job_profile: str
    approval_profile: str
    idempotency_key: str
    intent_payload_digest: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class ReceiptInheritanceV1:
    predecessor_operation_receipt_id: str
    predecessor_operation_receipt_digest: str
    predecessor_attempt_id: str
    job_id: str
    new_attempt_id: str
    lineage_id: str
    installation_id: str
    canonical_operation_id: str
    effect_profile: str
    job_profile: str
    approval_profile: str
    idempotency_key: str
    intent_payload_digest: str
    effect_fingerprint: str
    account_ref: str
    target_ref: str
    uncertain_response_state: str

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class AttemptV1:
    attempt_id: str
    lineage_id: str
    attempt_number: int
    state: JobState
    lease_epoch: int
    worker_instance_id: str | None
    operation_receipt: OperationReceiptV1 | None
    effect_fingerprint: str | None
    account_ref: str | None
    target_ref: str | None
    uncertain_response_state: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class JobV1:
    job_id: str
    job_kind: str
    input_ref: str
    frozen_policy: dict[str, object]
    created_by: str
    installation_id: str
    canonical_operation_id: str
    idempotency_key: str
    intent_payload_digest: str
    state: JobState
    job_revision: int
    event_sequence: int
    current_attempt_id: str
    lineage_id: str
    created_at: str
    updated_at: str
    result_ref: str | None
    partial_failures: tuple[dict[str, object], ...]
    attempts: tuple[AttemptV1, ...]

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EventV1:
    event_id: str
    job_id: str
    attempt_id: str
    sequence: int
    job_revision: int
    event_type: str
    state: JobState
    created_at: str
    result_ref: str | None = None
    partial_failures: tuple[dict[str, object], ...] = ()
    reason_code: str | None = None
    lease_epoch: int | None = None
    worker_instance_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class ResultV1:
    result_id: str
    job_id: str
    attempt_id: str
    status: JobState
    result_ref: str | None
    item_failures: tuple[dict[str, object], ...]
    created_at: str
    lease_epoch: int | None = None
    worker_instance_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class MigrationReceiptV1:
    receipt_id: str
    source_path: str
    source_sha256: str
    source_schema: int
    imported_job_ids: tuple[str, ...]
    committed_at: str

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class ExternalOperationAssertionsV1:
    operation_receipt_id: str
    expected_attempt_id: str
    expected_lineage_id: str
    expected_installation_id: str
    expected_canonical_operation_id: str
    expected_effect_profile: str
    expected_job_profile: str
    expected_approval_profile: str
    expected_idempotency_key: str
    expected_effect_fingerprint: str
    expected_account_ref: str
    expected_target_ref: str
    expected_uncertain_response_state: str
    expected_job_revision: int

    def as_dict(self) -> dict[str, object]:
        return _json_value(asdict(self))
