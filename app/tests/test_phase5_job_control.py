"""Phase 5 job list/show/wait/cancel/retry/resume behavior."""

from __future__ import annotations

import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.core.job_ledger.contracts import JobLedgerError, JobState
from tests.test_phase5_job_ledger import (
    FakeIntegrityAnchorStore,
    _create_arguments,
    _operation_receipt,
)


class Phase5JobControlTests(unittest.TestCase):
    def test_first_caller_receipt_is_unavailable_while_local_claim_remains(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        service_module = importlib.import_module("src.core.job_ledger.service")
        for entrypoint in ("store", "service"):
            with self.subTest(entrypoint=entrypoint), TemporaryDirectory() as temporary:
                anchor = FakeIntegrityAnchorStore()
                store = store_module.JobLedgerStore(
                    Path(temporary).resolve() / "isolated-data",
                    anchor,
                )
                created = store.create_job(**_create_arguments())
                queued = store.transition(
                    created.job_id,
                    expected_revision=created.job_revision,
                    new_state=JobState.QUEUED,
                    changed_at="2026-08-20T06:00:01Z",
                )
                stable_job = store.get_job(created.job_id)
                stable_head = store.head
                stable_journal = store.journal_path.read_bytes()
                stable_snapshot = store.snapshot_path.read_bytes()
                stable_anchor = anchor.load()
                stable_anchor_mutations = anchor.mutation_count

                with self.assertRaises(JobLedgerError) as rejected:
                    if entrypoint == "store":
                        store.claim(
                            created.job_id,
                            expected_revision=queued.job_revision,
                            worker_instance_id="caller-external",
                            operation_receipt=_operation_receipt(
                                "caller-created-receipt"
                            ),
                            changed_at="2026-08-20T06:00:02Z",
                        )
                    else:
                        service_module.JobControlService(store).claim_worker(
                            created.job_id,
                            expected_revision=queued.job_revision,
                            worker_instance_id="caller-external",
                            operation_receipt=_operation_receipt(
                                "caller-created-receipt"
                            ),
                            changed_at="2026-08-20T06:00:02Z",
                        )

                self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(
                    rejected.exception.reason_code,
                    "AUTHORITATIVE_OPERATION_RECEIPT_ISSUANCE_UNAVAILABLE",
                )
                self.assertEqual(store.get_job(created.job_id), stable_job)
                self.assertEqual(store.head, stable_head)
                self.assertEqual(store.journal_path.read_bytes(), stable_journal)
                self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
                self.assertEqual(anchor.load(), stable_anchor)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

                local = store.claim(
                    created.job_id,
                    expected_revision=queued.job_revision,
                    worker_instance_id="local-worker",
                    operation_receipt=None,
                    changed_at="2026-08-20T06:00:03Z",
                )
                self.assertEqual(local.state, JobState.RUNNING)
                self.assertIsNone(local.attempts[-1].operation_receipt)

    def test_control_service_preserves_cancel_retry_resume_and_event_semantics(
        self,
    ) -> None:
        try:
            service_module = importlib.import_module("src.core.job_ledger.service")
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 5 job control service is unavailable: {exc}")
        store_module = importlib.import_module("src.core.job_ledger.store")

        with TemporaryDirectory() as temporary_directory:
            store = store_module.JobLedgerStore(
                Path(temporary_directory).resolve() / "isolated-data",
                FakeIntegrityAnchorStore(),
            )
            created = store.create_job(**_create_arguments())
            store.transition(
                created.job_id,
                expected_revision=1,
                new_state=JobState.QUEUED,
                changed_at="2026-08-20T02:00:01Z",
            )
            service = service_module.JobControlService(store)

            self.assertEqual([job.job_id for job in service.list()], ["job-001"])
            self.assertEqual(service.show("job-001").job_revision, 2)
            self.assertEqual(
                [event.sequence for event in service.wait("job-001", after_sequence=0)],
                [1, 2],
            )

            requested = service.cancel(
                "job-001",
                expected_revision=2,
                changed_at="2026-08-20T02:00:02Z",
            )
            self.assertEqual(requested.state, JobState.CANCEL_REQUESTED)
            self.assertEqual(requested.job_revision, 3)
            stable_head = store.head
            repeated = service.cancel(
                "job-001",
                expected_revision=3,
                changed_at="2026-08-20T02:00:03Z",
            )
            self.assertEqual(repeated, requested)
            self.assertEqual(store.head, stable_head)

            cancelled = service.settle_cancelled(
                "job-001",
                expected_revision=3,
                changed_at="2026-08-20T02:00:04Z",
            )
            self.assertEqual(cancelled.state, JobState.CANCELLED)

            retried = service.retry(
                "job-001",
                expected_revision=4,
                new_attempt_id="attempt-002",
                changed_at="2026-08-20T02:00:05Z",
            )
            self.assertEqual(retried.state, JobState.QUEUED)
            self.assertEqual(retried.current_attempt_id, "attempt-002")
            self.assertEqual(retried.lineage_id, "lineage-001")
            self.assertEqual(retried.idempotency_key, "idem-001")
            self.assertEqual(len(retried.attempts), 2)
            self.assertEqual(retried.attempts[0].state, JobState.CANCELLED)
            self.assertEqual(retried.attempts[1].attempt_number, 2)

            running = store.claim(
                "job-001",
                expected_revision=5,
                worker_instance_id="worker-retry",
                operation_receipt=None,
                changed_at="2026-08-20T02:00:06Z",
            )
            recovery = store.transition(
                "job-001",
                expected_revision=running.job_revision,
                new_state=JobState.RECOVERY_REQUIRED,
                changed_at="2026-08-20T02:00:07Z",
                reason_code="OWNER_LOST",
            )
            resumed = service.resume(
                "job-001",
                expected_revision=recovery.job_revision,
                changed_at="2026-08-20T02:00:08Z",
            )
            self.assertEqual(resumed.state, JobState.QUEUED)
            self.assertEqual(resumed.current_attempt_id, "attempt-002")
            self.assertEqual(len(resumed.attempts), 2)
            self.assertEqual(
                [event.sequence for event in service.wait("job-001", after_sequence=3)],
                [4, 5, 6, 7, 8],
            )
            failed = store.transition(
                "job-001",
                expected_revision=resumed.job_revision,
                new_state=JobState.FAILED,
                changed_at="2026-08-20T02:00:09Z",
                reason_code="DETERMINED_FAILURE",
            )
            self.assertEqual(failed.state, JobState.FAILED)
            self.assertEqual(failed.job_revision, 9)

            stable_head = store.head
            with self.assertRaises(JobLedgerError):
                service.show("job-missing")
            self.assertEqual(store.head, stable_head)


if __name__ == "__main__":
    unittest.main()
