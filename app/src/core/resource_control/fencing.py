"""Pure validation of monotonic job worker lease fences."""

from __future__ import annotations

from ..job_ledger.contracts import (
    JobState,
    JobV1,
    LeaseToken,
    conflict,
    integrity_error,
)


WORKER_MUTATION_KINDS = frozenset(
    {"event", "output", "result", "effect_receipt", "file"}
)


class JobLeaseFencer:
    @staticmethod
    def verify(
        job: JobV1,
        lease_token: LeaseToken,
        *,
        mutation_kind: str,
    ) -> None:
        if not isinstance(job, JobV1):
            raise integrity_error("JOB_LEASE_AUTHORITY_INVALID")
        if mutation_kind not in WORKER_MUTATION_KINDS:
            raise integrity_error("JOB_MUTATION_KIND_INVALID")
        if (
            not isinstance(lease_token, LeaseToken)
            or type(lease_token.job_id) is not str
            or not lease_token.job_id
            or type(lease_token.lease_epoch) is not int
            or lease_token.lease_epoch < 1
            or type(lease_token.worker_instance_id) is not str
            or not lease_token.worker_instance_id
        ):
            raise integrity_error("JOB_LEASE_TOKEN_INVALID")
        attempt = job.attempts[-1]
        allowed_states = (
            {JobState.RUNNING, JobState.CANCEL_REQUESTED}
            if mutation_kind in {"event", "result"}
            else {JobState.RUNNING}
        )
        if job.state not in allowed_states:
            raise conflict("JOB_LEASE_STATE_REJECTED")
        if (
            lease_token.job_id != job.job_id
            or lease_token.lease_epoch != attempt.lease_epoch
            or lease_token.worker_instance_id != attempt.worker_instance_id
        ):
            raise conflict("JOB_LEASE_FENCE_REJECTED")
