"""Qt-free control service over the authoritative Phase 5 job ledger."""

from __future__ import annotations

import hashlib
import json

from .contracts import (
    ExternalOperationAssertionsV1,
    EventV1,
    JobState,
    JobV1,
    LeaseToken,
    OperationReceiptV1,
    TERMINAL_STATES,
    conflict,
    integrity_error,
)
from .store import JobLedgerStore


class JobControlService:
    def __init__(
        self,
        store: JobLedgerStore,
        *,
        enforce_fencing: bool = False,
    ) -> None:
        if not isinstance(store, JobLedgerStore):
            raise TypeError("JobControlService requires JobLedgerStore")
        if type(enforce_fencing) is not bool:
            raise TypeError("enforce_fencing must be bool")
        self._store = store
        if enforce_fencing:
            store.enable_lease_fencing()

    def create_gui_task(
        self,
        *,
        task: dict[str, object],
        job_id: str,
        attempt_id: str,
        lineage_id: str,
        installation_id: str,
        created_at: str,
    ) -> JobV1:
        """Create one GUI task inside the same authoritative Core ledger."""
        expected = {
            "title",
            "source",
            "kind",
            "folder_id",
            "stable_key",
            "execution_policy",
        }
        if not isinstance(task, dict) or set(task) != expected:
            raise integrity_error("GUI_TASK_PAYLOAD_INVALID")
        title = task["title"]
        source = task["source"]
        kind = task["kind"]
        folder_id = task["folder_id"]
        stable_key = task["stable_key"]
        if (
            not isinstance(title, str)
            or not title
            or len(title) > 500
            or not isinstance(source, str)
            or not source
            or len(source) > 16 * 1024
            or kind not in {"file", "link"}
            or (folder_id is not None and not isinstance(folder_id, str))
            or not isinstance(stable_key, str)
            or len(stable_key) > 500
            or (task["execution_policy"] is not None and not isinstance(task["execution_policy"], dict))
        ):
            raise integrity_error("GUI_TASK_PAYLOAD_INVALID")
        gui_task = {
            "title": title,
            "source": source,
            "kind": kind,
            "folder_id": folder_id,
            "stable_key": stable_key,
            "execution_policy": task["execution_policy"],
        }
        intent = {
            "installation_id": installation_id,
            "job_id": job_id,
            "gui_task": gui_task,
        }
        digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return self._store.create_job(
            job_id=job_id,
            attempt_id=attempt_id,
            lineage_id=lineage_id,
            job_kind="gui_transcription",
            input_ref=f"gui-source:sha256:{source_digest}",
            frozen_policy={"gui_task": gui_task},
            created_by="gui",
            installation_id=installation_id,
            canonical_operation_id="OP_GUI_TRANSCRIPTION_V1",
            idempotency_key=f"gui-task:{digest}",
            intent_payload_digest=digest,
            created_at=created_at,
            effect_fingerprint="effect:local-transcription-result",
            account_ref="account:none",
            target_ref="target:shared-lulu-jobs-results",
            uncertain_response_state="none",
        )

    def transition_gui_task(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_state: JobState,
        changed_at: str,
        result_ref: str | None = None,
        error_message: str | None = None,
        execution_policy: dict | None = None,
    ) -> JobV1:
        """Move a GUI worker through the existing Core job state machine."""
        job = self.show(job_id)
        if job.job_revision != expected_revision:
            raise conflict("JOB_REVISION_CONFLICT")
        if execution_policy is not None:
            if new_state not in {JobState.PREPARED, JobState.QUEUED, JobState.RUNNING}:
                raise conflict("GUI_POLICY_STATE_INVALID")
            job = self._store.freeze_gui_policy(job_id, expected_revision=expected_revision,
                policy=execution_policy, changed_at=changed_at)
            expected_revision = job.job_revision
        if job.state is new_state:
            return job
        if new_state is JobState.QUEUED and job.created_by == "gui":
            if job.state in {JobState.NEEDS_INPUT, JobState.RECOVERY_REQUIRED}:
                return self.resume(job_id, expected_revision=expected_revision, changed_at=changed_at)
            if job.state in {JobState.FAILED, JobState.CANCELLED}:
                from uuid import uuid4
                return self.retry(job_id, expected_revision=expected_revision,
                    new_attempt_id=f"gui-attempt-{uuid4().hex}", changed_at=changed_at)
        if new_state is JobState.PREPARED and job.created_by == "gui" and job.state in {JobState.NEEDS_INPUT, JobState.RECOVERY_REQUIRED}:
            cancelled = self.transition_gui_task(job_id, expected_revision=expected_revision,
                new_state=JobState.CANCELLED, changed_at=changed_at)
            return self.transition_gui_task(job_id, expected_revision=cancelled.job_revision,
                new_state=JobState.PREPARED, changed_at=changed_at)
        if new_state is JobState.PREPARED and job.state in {
            JobState.FAILED, JobState.CANCELLED,
        } and job.created_by == "gui":
            from uuid import uuid4
            return self._store.retry_attempt(
                job_id, expected_revision=expected_revision,
                new_attempt_id=f"gui-attempt-{uuid4().hex}",
                changed_at=changed_at, enqueue=False,
            )
        if new_state is JobState.QUEUED and job.state is JobState.PREPARED:
            return self._store.transition(
                job_id,
                expected_revision=expected_revision,
                new_state=new_state,
                changed_at=changed_at,
            )
        if new_state is JobState.RUNNING and job.state is JobState.QUEUED:
            running, _lease = self.claim_worker(
                job_id,
                expected_revision=expected_revision,
                worker_instance_id=f"gui:{job_id}",
                operation_receipt=None,
                changed_at=changed_at,
            )
            return running
        if new_state is JobState.NEEDS_INPUT and job.state is JobState.RUNNING:
            return self._store.transition(
                job_id,
                expected_revision=expected_revision,
                new_state=new_state,
                changed_at=changed_at,
                lease_token=self._store.current_lease_token(job_id),
            )
        if new_state in TERMINAL_STATES:
            if new_state is JobState.CANCELLED:
                requested = self.cancel(
                    job_id,
                    expected_revision=expected_revision,
                    changed_at=changed_at,
                )
                if requested.state is JobState.CANCELLED:
                    return requested
                lease = (
                    self._store.current_lease_token(job_id)
                    if job.state is JobState.RUNNING
                    else None
                )
                return self.settle_cancelled(
                    job_id,
                    expected_revision=requested.job_revision,
                    changed_at=changed_at,
                    lease_token=lease,
                )
            if new_state in {JobState.SUCCEEDED, JobState.PARTIAL} and job.state in {
                JobState.PREPARED,
                JobState.NEEDS_INPUT,
                JobState.RECOVERY_REQUIRED,
            }:
                if job.state is JobState.PREPARED:
                    job = self._store.transition(
                        job_id,
                        expected_revision=job.job_revision,
                        new_state=JobState.QUEUED,
                        changed_at=changed_at,
                    )
                else:
                    job = self.resume(
                        job_id,
                        expected_revision=job.job_revision,
                        changed_at=changed_at,
                    )
                job, _lease = self.claim_worker(
                    job_id,
                    expected_revision=job.job_revision,
                    worker_instance_id=f"gui:{job_id}",
                    operation_receipt=None,
                    changed_at=changed_at,
                )
                expected_revision = job.job_revision
            lease = (
                self._store.current_lease_token(job_id)
                if job.state is JobState.RUNNING
                else None
            )
            return self._store.transition(
                job_id,
                expected_revision=expected_revision,
                new_state=new_state,
                result_ref=(
                    result_ref
                    if new_state in {JobState.SUCCEEDED, JobState.PARTIAL}
                    else None
                ),
                changed_at=changed_at,
                lease_token=lease,
                partial_failures=({"item_ref": job_id, "code": "GUI_TRANSCRIPTION_FAILED",
                    "retry_hint": error_message[:8000]},) if new_state is JobState.FAILED and error_message else (),
            )
        raise conflict("JOB_STATE_TRANSITION_INVALID")

    def claim_worker(
        self,
        job_id: str,
        *,
        expected_revision: int,
        worker_instance_id: str,
        operation_receipt: OperationReceiptV1 | None,
        approved_external_claim: object | None = None,
        changed_at: str,
    ) -> tuple[JobV1, LeaseToken]:
        job = self._store.claim(
            job_id,
            expected_revision=expected_revision,
            worker_instance_id=worker_instance_id,
            operation_receipt=operation_receipt,
            approved_external_claim=approved_external_claim,
            changed_at=changed_at,
        )
        return job, self._store.current_lease_token(job_id)

    def append_worker_event(
        self,
        job_id: str,
        *,
        expected_revision: int,
        lease_token: LeaseToken,
        event_type: str,
        changed_at: str,
    ) -> JobV1:
        return self._store.append_worker_event(
            job_id,
            expected_revision=expected_revision,
            lease_token=lease_token,
            event_type=event_type,
            changed_at=changed_at,
        )

    def verify_worker_lease(
        self,
        job_id: str,
        *,
        lease_token: LeaseToken,
        mutation_kind: str,
    ) -> None:
        self._store.verify_worker_lease(
            job_id,
            lease_token,
            mutation_kind=mutation_kind,
        )

    def complete_worker(
        self,
        job_id: str,
        *,
        expected_revision: int,
        lease_token: LeaseToken,
        new_state: JobState,
        result_ref: str | None,
        changed_at: str,
    ) -> JobV1:
        if new_state not in TERMINAL_STATES:
            raise conflict("JOB_WORKER_RESULT_STATE_INVALID")
        return self._store.transition(
            job_id,
            expected_revision=expected_revision,
            new_state=new_state,
            result_ref=result_ref,
            changed_at=changed_at,
            lease_token=lease_token,
        )

    def await_input_worker(
        self,
        job_id: str,
        *,
        expected_revision: int,
        lease_token: LeaseToken,
        result_ref: str,
        changed_at: str,
    ) -> JobV1:
        """Persist a worker result that must wait for a user action."""
        if not isinstance(result_ref, str) or not result_ref:
            raise integrity_error("JOB_RESULT_REFERENCE_INVALID")
        job = self.show(job_id)
        if job.job_revision != expected_revision:
            raise conflict("JOB_REVISION_CONFLICT")
        if job.state is not JobState.RUNNING:
            raise conflict("JOB_STATE_TRANSITION_INVALID")
        return self._store.transition(
            job_id,
            expected_revision=expected_revision,
            new_state=JobState.NEEDS_INPUT,
            result_ref=result_ref,
            changed_at=changed_at,
            lease_token=lease_token,
        )

    def list(self) -> tuple[JobV1, ...]:
        return self._store.list_jobs()

    def show(self, job_id: str) -> JobV1:
        return self._store.get_job(job_id)

    def wait(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[EventV1, ...]:
        return self._store.events(job_id, after_sequence=after_sequence)

    def cancel(
        self,
        job_id: str,
        *,
        expected_revision: int,
        changed_at: str,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.job_revision != expected_revision:
            raise conflict("JOB_REVISION_CONFLICT")
        if job.state is JobState.CANCEL_REQUESTED:
            return job
        if job.state in TERMINAL_STATES:
            raise conflict("TERMINAL_STATE_IMMUTABLE")
        new_state = (
            JobState.CANCEL_REQUESTED
            if job.state in {JobState.QUEUED, JobState.RUNNING}
            else JobState.CANCELLED
        )
        return self._store.transition(
            job_id,
            expected_revision=expected_revision,
            new_state=new_state,
            changed_at=changed_at,
            reason_code="USER_CANCEL_REQUESTED",
        )

    def settle_cancelled(
        self,
        job_id: str,
        *,
        expected_revision: int,
        changed_at: str,
        lease_token: LeaseToken | None = None,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.state is not JobState.CANCEL_REQUESTED:
            raise conflict("JOB_CANCEL_NOT_SETTLED")
        return self._store.transition(
            job_id,
            expected_revision=expected_revision,
            new_state=JobState.CANCELLED,
            changed_at=changed_at,
            reason_code="WORKER_CANCELLED_AT_BOUNDARY",
            lease_token=lease_token,
        )

    def retry(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_attempt_id: str,
        changed_at: str,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.attempts[-1].operation_receipt is not None:
            raise integrity_error("EXTERNAL_RETRY_REQUIRES_RECEIPT_ASSERTIONS")
        return self._store.retry_attempt(
            job_id,
            expected_revision=expected_revision,
            new_attempt_id=new_attempt_id,
            changed_at=changed_at,
        )

    def resume(
        self,
        job_id: str,
        *,
        expected_revision: int,
        changed_at: str,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.state not in {JobState.NEEDS_INPUT, JobState.RECOVERY_REQUIRED}:
            raise conflict("JOB_NOT_RESUMABLE")
        return self._store.transition(
            job_id,
            expected_revision=expected_revision,
            new_state=JobState.QUEUED,
            changed_at=changed_at,
            reason_code="EXPLICIT_RESUME",
        )

    @staticmethod
    def _verify_external_assertions(
        job: JobV1,
        assertions: ExternalOperationAssertionsV1,
    ) -> None:
        if not isinstance(assertions, ExternalOperationAssertionsV1):
            raise integrity_error("EXTERNAL_OPERATION_ASSERTIONS_INVALID")
        values = assertions.as_dict()
        string_fields = tuple(field for field in values if field != "expected_job_revision")
        if any(
            type(values[field]) is not str or not values[field]
            for field in string_fields
        ):
            raise integrity_error("EXTERNAL_OPERATION_EVIDENCE_INCOMPLETE")
        if (
            type(assertions.expected_job_revision) is not int
            or assertions.expected_job_revision < 1
        ):
            raise integrity_error("EXTERNAL_OPERATION_EVIDENCE_INCOMPLETE")
        attempt = job.attempts[-1]
        receipt = attempt.operation_receipt
        if (
            receipt is None
            or attempt.effect_fingerprint is None
            or attempt.account_ref is None
            or attempt.target_ref is None
            or attempt.uncertain_response_state is None
        ):
            raise integrity_error("EXTERNAL_OPERATION_EVIDENCE_INCOMPLETE")
        comparisons = (
            assertions.operation_receipt_id == receipt.operation_receipt_id,
            assertions.expected_attempt_id == receipt.attempt_id == attempt.attempt_id,
            assertions.expected_lineage_id == receipt.lineage_id == attempt.lineage_id,
            assertions.expected_installation_id
            == receipt.installation_id
            == job.installation_id,
            assertions.expected_canonical_operation_id
            == receipt.canonical_operation_id
            == job.canonical_operation_id,
            assertions.expected_effect_profile == receipt.effect_profile,
            assertions.expected_job_profile == receipt.job_profile,
            assertions.expected_approval_profile == receipt.approval_profile,
            assertions.expected_idempotency_key
            == receipt.idempotency_key
            == job.idempotency_key,
            receipt.intent_payload_digest == job.intent_payload_digest,
            assertions.expected_effect_fingerprint == attempt.effect_fingerprint,
            assertions.expected_account_ref == attempt.account_ref,
            assertions.expected_target_ref == attempt.target_ref,
            assertions.expected_uncertain_response_state
            == attempt.uncertain_response_state,
            assertions.expected_job_revision == job.job_revision,
        )
        if not all(comparisons):
            raise integrity_error("EXTERNAL_OPERATION_BINDING_MISMATCH")

    def resume_external(
        self,
        job_id: str,
        *,
        assertions: ExternalOperationAssertionsV1,
        changed_at: str,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.state is not JobState.RECOVERY_REQUIRED:
            raise conflict("JOB_NOT_RESUMABLE")
        self._verify_external_assertions(job, assertions)
        return self._store.transition(
            job_id,
            expected_revision=job.job_revision,
            new_state=JobState.QUEUED,
            changed_at=changed_at,
            reason_code="EXPLICIT_EXTERNAL_RESUME",
        )

    def retry_external(
        self,
        job_id: str,
        *,
        assertions: ExternalOperationAssertionsV1,
        new_attempt_id: str,
        changed_at: str,
    ) -> JobV1:
        job = self._store.get_job(job_id)
        if job.state not in TERMINAL_STATES:
            raise conflict("JOB_RETRY_REQUIRES_TERMINAL_ATTEMPT")
        self._verify_external_assertions(job, assertions)
        return self._store.retry_attempt(
            job_id,
            expected_revision=job.job_revision,
            new_attempt_id=new_attempt_id,
            changed_at=changed_at,
        )
