"""Phase 6 monotonic job lease and stale-worker fencing tests."""

from __future__ import annotations

import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.core.job_ledger.contracts import JobLedgerError, JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments


class Phase6JobFencingTests(unittest.TestCase):
    def test_claim_reclaim_and_every_worker_mutation_reject_stale_lease(self) -> None:
        contracts = importlib.import_module("src.core.job_ledger.contracts")
        lease_token_type = getattr(contracts, "LeaseToken", None)
        self.assertIsNotNone(
            lease_token_type,
            "Phase 6 LeaseToken contract is unavailable",
        )
        with TemporaryDirectory() as temporary_directory:
            anchor = FakeIntegrityAnchorStore()
            store = JobLedgerStore(
                Path(temporary_directory).resolve() / "isolated-data",
                anchor,
            )
            created = store.create_job(**_create_arguments())
            queued = store.transition(
                created.job_id,
                expected_revision=created.job_revision,
                new_state=JobState.QUEUED,
                changed_at="2026-08-20T05:00:01Z",
            )
            service = JobControlService(store, enforce_fencing=True)

            running_one, lease_one = service.claim_worker(
                queued.job_id,
                expected_revision=queued.job_revision,
                worker_instance_id="worker-one",
                operation_receipt=None,
                changed_at="2026-08-20T05:00:02Z",
            )
            self.assertEqual(
                lease_one,
                lease_token_type("job-001", 1, "worker-one"),
            )
            self.assertEqual(running_one.attempts[-1].lease_epoch, 1)
            progress = service.append_worker_event(
                running_one.job_id,
                expected_revision=running_one.job_revision,
                lease_token=lease_one,
                event_type="progress",
                changed_at="2026-08-20T05:00:03Z",
            )
            progress_event = service.wait(progress.job_id)[-1]
            self.assertEqual(progress_event.lease_epoch, 1)
            self.assertEqual(progress_event.worker_instance_id, "worker-one")

            recovered = store.recover_orphaned_running(
                changed_at="2026-08-20T05:00:04Z"
            )[0]
            self.assertEqual(recovered.state, JobState.RECOVERY_REQUIRED)
            resumed = service.resume(
                recovered.job_id,
                expected_revision=recovered.job_revision,
                changed_at="2026-08-20T05:00:05Z",
            )
            running_two, lease_two = service.claim_worker(
                resumed.job_id,
                expected_revision=resumed.job_revision,
                worker_instance_id="worker-two",
                operation_receipt=None,
                changed_at="2026-08-20T05:00:06Z",
            )
            self.assertEqual(
                lease_two,
                lease_token_type("job-001", 2, "worker-two"),
            )

            stable_head = store.head
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor_mutations = anchor.mutation_count
            rejected_codes: list[str] = []
            for mutation in ("event", "result", "effect_receipt"):
                with self.subTest(mutation=mutation):
                    try:
                        if mutation == "event":
                            service.append_worker_event(
                                running_two.job_id,
                                expected_revision=running_two.job_revision,
                                lease_token=lease_one,
                                event_type="stale-progress",
                                changed_at="2026-08-20T05:00:07Z",
                            )
                        elif mutation == "result":
                            service.complete_worker(
                                running_two.job_id,
                                expected_revision=running_two.job_revision,
                                lease_token=lease_one,
                                new_state=JobState.SUCCEEDED,
                                result_ref="result:stale",
                                changed_at="2026-08-20T05:00:07Z",
                            )
                        else:
                            service.verify_worker_lease(
                                running_two.job_id,
                                lease_token=lease_one,
                                mutation_kind="effect_receipt",
                            )
                    except JobLedgerError as exc:
                        rejected_codes.append(exc.code)
                    else:
                        self.fail(f"stale {mutation} mutation was accepted")

            with self.assertRaises(JobLedgerError) as bypass:
                store.transition(
                    running_two.job_id,
                    expected_revision=running_two.job_revision,
                    new_state=JobState.SUCCEEDED,
                    result_ref="result:missing-lease",
                    changed_at="2026-08-20T05:00:07Z",
                )
            self.assertEqual(bypass.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(rejected_codes, ["CONFLICT", "CONFLICT", "CONFLICT"])
            self.assertEqual(store.head, stable_head)
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

            event_committed = service.append_worker_event(
                running_two.job_id,
                expected_revision=running_two.job_revision,
                lease_token=lease_two,
                event_type="output_committed",
                changed_at="2026-08-20T05:00:08Z",
            )
            succeeded = service.complete_worker(
                event_committed.job_id,
                expected_revision=event_committed.job_revision,
                lease_token=lease_two,
                new_state=JobState.SUCCEEDED,
                result_ref="result:sha256:" + "d" * 64,
                changed_at="2026-08-20T05:00:09Z",
            )
            result_event = service.wait(succeeded.job_id)[-1]
            self.assertEqual(result_event.lease_epoch, 2)
            self.assertEqual(result_event.worker_instance_id, "worker-two")

            retried = service.retry(
                succeeded.job_id,
                expected_revision=succeeded.job_revision,
                new_attempt_id="attempt-002",
                changed_at="2026-08-20T05:00:10Z",
            )
            self.assertEqual(retried.attempts[-1].lease_epoch, 2)
            running_three, lease_three = service.claim_worker(
                retried.job_id,
                expected_revision=retried.job_revision,
                worker_instance_id="worker-three",
                operation_receipt=None,
                changed_at="2026-08-20T05:00:11Z",
            )
            self.assertEqual(
                lease_three,
                lease_token_type("job-001", 3, "worker-three"),
            )
            with self.assertRaises(JobLedgerError):
                service.append_worker_event(
                    running_three.job_id,
                    expected_revision=running_three.job_revision,
                    lease_token=lease_two,
                    event_type="prior-attempt-stale",
                    changed_at="2026-08-20T05:00:12Z",
                )


if __name__ == "__main__":
    unittest.main()
