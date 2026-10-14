"""Phase 6 end-to-end resource arbitration and worker fencing."""

from __future__ import annotations

import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.core.job_ledger.contracts import JobLedgerError, JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments
from tests.test_phase6_resource_recovery import FakeProcessIdentityProbe


ALPHA_SHA256 = "04583d2e020b0803cac58a764da8a4368a0ac64857da359a736beb16bfa23b15"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class Phase6ResourceArbitrationTests(unittest.TestCase):
    def test_gui_cli_and_reclaimed_workers_share_one_arbitration_boundary(self) -> None:
        contracts = importlib.import_module("src.core.resource_control.contracts")
        locks = importlib.import_module("src.core.resource_control.locks")
        cas = importlib.import_module("src.core.resource_control.cas")
        service_module = importlib.import_module("src.core.resource_control.service")
        self.assertTrue(
            hasattr(service_module.ResourceControlService, "commit_worker_file"),
            "Phase 6 unified arbitration seam is unavailable",
        )

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            resource_root = root / "resources"
            lock_manager = locks.ResourceLockManager(resource_root)
            cas_store = cas.ResourceCASStore(resource_root, lock_manager)
            job_store = JobLedgerStore(
                root / "jobs",
                FakeIntegrityAnchorStore(),
            )
            created = job_store.create_job(**_create_arguments())
            queued = job_store.transition(
                created.job_id,
                expected_revision=created.job_revision,
                new_state=JobState.QUEUED,
                changed_at="2026-08-20T07:00:01Z",
            )
            job_service = JobControlService(job_store, enforce_fencing=True)
            service = service_module.ResourceControlService(
                lock_manager,
                process_probe=FakeProcessIdentityProbe(alive=False),
                cas_store=cas_store,
                job_service=job_service,
            )
            gui_owner = contracts.LockOwner(
                pid=601,
                process_start="fake-start:601",
                installation_id="installation-test",
                operation_id="gui:model-install",
            )
            cli_owner = contracts.LockOwner(
                pid=602,
                process_start="fake-start:602",
                installation_id="installation-test",
                operation_id="cli:model-install",
            )

            model_lease = service.claim(
                contracts.ResourceKind.MODEL,
                "sensevoice-small",
                gui_owner,
            )
            with self.assertRaises(contracts.ResourceControlError) as busy:
                service.claim(
                    contracts.ResourceKind.MODEL,
                    "sensevoice-small",
                    cli_owner,
                )
            self.assertEqual((busy.exception.code, busy.exception.exit_code), ("RESOURCE_BUSY", 22))
            tts_lease = service.claim(
                contracts.ResourceKind.TTS_ENGINE,
                "indextts2",
                cli_owner,
            )
            service.release(tts_lease)
            service.release(model_lease)

            running_one, token_one = job_service.claim_worker(
                queued.job_id,
                expected_revision=queued.job_revision,
                worker_instance_id="worker-one",
                operation_receipt=None,
                changed_at="2026-08-20T07:00:02Z",
            )
            worker_one_owner = contracts.LockOwner(
                pid=603,
                process_start="fake-start:603",
                installation_id="installation-test",
                operation_id="worker-one:job-001",
            )
            job_lease_one = service.claim(
                contracts.ResourceKind.JOB,
                running_one.job_id,
                worker_one_owner,
            )
            first_file = service.commit_worker_file(
                job_lease_one,
                lease_token=token_one,
                expected=contracts.CASRevision(0, EMPTY_SHA256),
                content=b"phase6-alpha-record",
            )
            self.assertEqual(
                first_file.revision,
                contracts.CASRevision(1, ALPHA_SHA256),
            )
            service.release(job_lease_one)

            recovered = job_store.recover_orphaned_running(
                changed_at="2026-08-20T07:00:03Z"
            )[0]
            resumed = job_service.resume(
                recovered.job_id,
                expected_revision=recovered.job_revision,
                changed_at="2026-08-20T07:00:04Z",
            )
            running_two, token_two = job_service.claim_worker(
                resumed.job_id,
                expected_revision=resumed.job_revision,
                worker_instance_id="worker-two",
                operation_receipt=None,
                changed_at="2026-08-20T07:00:05Z",
            )
            worker_two_owner = contracts.LockOwner(
                pid=604,
                process_start="fake-start:604",
                installation_id="installation-test",
                operation_id="worker-two:job-001",
            )
            job_lease_two = service.claim(
                contracts.ResourceKind.JOB,
                running_two.job_id,
                worker_two_owner,
            )
            stable_job_head = job_store.head
            stable_job_journal = job_store.journal_path.read_bytes()
            stable_file_bytes = first_file.record_path.read_bytes()
            for mutation in ("file", "event", "result", "effect_receipt"):
                with self.subTest(stale_worker_mutation=mutation):
                    try:
                        if mutation == "file":
                            service.commit_worker_file(
                                job_lease_two,
                                lease_token=token_one,
                                expected=first_file.revision,
                                content=b"forbidden-stale-file",
                            )
                        elif mutation == "event":
                            job_service.append_worker_event(
                                running_two.job_id,
                                expected_revision=running_two.job_revision,
                                lease_token=token_one,
                                event_type="stale-event",
                                changed_at="2026-08-20T07:00:06Z",
                            )
                        elif mutation == "result":
                            job_service.complete_worker(
                                running_two.job_id,
                                expected_revision=running_two.job_revision,
                                lease_token=token_one,
                                new_state=JobState.SUCCEEDED,
                                result_ref="result:stale",
                                changed_at="2026-08-20T07:00:06Z",
                            )
                        else:
                            service.verify_worker_effect(
                                running_two.job_id,
                                lease_token=token_one,
                            )
                    except JobLedgerError as exc:
                        self.assertEqual((exc.code, exc.exit_code), ("CONFLICT", 21))
                    else:
                        self.fail(f"stale worker {mutation} was accepted")
                    self.assertEqual(job_store.head, stable_job_head)
                    self.assertEqual(job_store.journal_path.read_bytes(), stable_job_journal)
                    self.assertEqual(first_file.record_path.read_bytes(), stable_file_bytes)

            second_file = service.commit_worker_file(
                job_lease_two,
                lease_token=token_two,
                expected=first_file.revision,
                content=b"phase6-beta-record",
            )
            self.assertEqual(second_file.revision.revision, 2)
            service.verify_worker_effect(running_two.job_id, lease_token=token_two)
            succeeded = job_service.complete_worker(
                running_two.job_id,
                expected_revision=running_two.job_revision,
                lease_token=token_two,
                new_state=JobState.SUCCEEDED,
                result_ref="result:sha256:" + "e" * 64,
                changed_at="2026-08-20T07:00:07Z",
            )
            terminal_file_bytes = second_file.record_path.read_bytes()
            terminal_job_head = job_store.head
            for terminal_mutation in ("file", "effect_receipt"):
                with self.subTest(terminal_mutation=terminal_mutation):
                    with self.assertRaises(JobLedgerError) as terminal_rejected:
                        if terminal_mutation == "file":
                            service.commit_worker_file(
                                job_lease_two,
                                lease_token=token_two,
                                expected=second_file.revision,
                                content=b"forbidden-after-terminal",
                            )
                        else:
                            service.verify_worker_effect(
                                succeeded.job_id,
                                lease_token=token_two,
                            )
                    self.assertEqual(
                        (terminal_rejected.exception.code, terminal_rejected.exception.exit_code),
                        ("CONFLICT", 21),
                    )
                    self.assertEqual(second_file.record_path.read_bytes(), terminal_file_bytes)
                    self.assertEqual(job_store.head, terminal_job_head)
            service.release(job_lease_two)


if __name__ == "__main__":
    unittest.main()
