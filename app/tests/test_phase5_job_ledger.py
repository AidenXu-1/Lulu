"""Phase 5 authoritative job-ledger persistence tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import fcntl
import importlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from src.core.job_ledger.contracts import (
    JobLedgerError,
    JobState,
    MigrationReceiptV1,
    OperationReceiptV1,
)


class FakeIntegrityAnchorStore:
    def __init__(self) -> None:
        self.state: dict[str, object] | None = None
        self.mutation_count = 0

    def hmac_key(self) -> bytes:
        return b"phase5-job-ledger-test-hmac-key!"

    def load(self) -> dict[str, object] | None:
        return deepcopy(self.state)

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None:
        if self.state != expected:
            raise RuntimeError("anchor changed")
        self.state = deepcopy(replacement)
        self.mutation_count += 1


class RejectNextIntegrityAnchorStore(FakeIntegrityAnchorStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_next = False

    def compare_and_set(self, expected, replacement) -> None:
        if self.reject_next:
            self.reject_next = False
            raise RuntimeError("simulated anchor CAS rejection")
        super().compare_and_set(expected, replacement)


def _create_arguments() -> dict[str, object]:
    return {
        "job_id": "job-001",
        "attempt_id": "attempt-001",
        "lineage_id": "lineage-001",
        "job_kind": "transcription",
        "input_ref": "source:sha256:" + "a" * 64,
        "frozen_policy": {"engine": "local_sensevoice", "mode": "offline"},
        "created_by": "phase5-test",
        "installation_id": "installation-001",
        "canonical_operation_id": "OP_TRANSCRIBE_LOCAL_V1",
        "idempotency_key": "idem-001",
        "intent_payload_digest": "b" * 64,
        "created_at": "2026-08-20T01:00:00Z",
        "effect_fingerprint": "effect:local-read",
        "account_ref": "account:none",
        "target_ref": "target:transcript:test",
        "uncertain_response_state": "none",
    }


def _operation_receipt(receipt_id: str) -> OperationReceiptV1:
    return OperationReceiptV1(
        operation_receipt_id=receipt_id,
        job_id="job-001",
        attempt_id="attempt-001",
        lineage_id="lineage-001",
        installation_id="installation-001",
        canonical_operation_id="OP_TRANSCRIBE_LOCAL_V1",
        effect_profile="EFFECT_LOCAL_READ",
        job_profile="JOB_LOCAL",
        approval_profile="APPROVAL_NONE",
        idempotency_key="idem-001",
        intent_payload_digest="b" * 64,
        created_at="2026-08-20T01:00:02Z",
    )


class Phase5JobLedgerTests(unittest.TestCase):
    def test_tampered_pending_fails_closed_without_mutation(self) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            real_replace = os.replace

            def fail_journal_replace(source, destination) -> None:
                if (
                    Path(source) == store.pending_path
                    and Path(destination) == store.journal_path
                ):
                    raise OSError("simulated crash before journal replace")
                real_replace(source, destination)

            with (
                patch.object(os, "replace", side_effect=fail_journal_replace),
                self.assertRaises(JobLedgerError),
            ):
                store.create_job(**_create_arguments())
            pending = store.pending_path.read_bytes()
            damaged = pending.replace(b"job-001", b"job-999", 1)
            self.assertNotEqual(damaged, pending)
            store.pending_path.write_bytes(damaged)
            os.chmod(store.pending_path, 0o600)
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor = anchor.load()
            stable_anchor_mutations = anchor.mutation_count

            with self.assertRaises(JobLedgerError) as rejected:
                store_module.JobLedgerStore(data_root, anchor)

            self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(store.pending_path.read_bytes(), damaged)
            self.assertEqual(anchor.load(), stable_anchor)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_reopen_recovers_commit_after_post_replace_directory_fsync_failure(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            real_replace = os.replace
            real_fsync = os.fsync
            journal_replaced = False

            def track_journal_replace(source, destination) -> None:
                nonlocal journal_replaced
                real_replace(source, destination)
                if (
                    Path(source) == store.pending_path
                    and Path(destination) == store.journal_path
                ):
                    journal_replaced = True

            def fail_after_journal_replace(descriptor) -> None:
                if journal_replaced and stat.S_ISDIR(
                    os.fstat(descriptor).st_mode
                ):
                    raise OSError("simulated crash after journal replace")
                real_fsync(descriptor)

            with (
                patch.object(os, "replace", side_effect=track_journal_replace),
                patch.object(os, "fsync", side_effect=fail_after_journal_replace),
                self.assertRaises(JobLedgerError) as interrupted,
            ):
                store.create_job(**_create_arguments())

            self.assertEqual(interrupted.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(
                interrupted.exception.reason_code,
                "JOB_LEDGER_JOURNAL_COMMIT_FAILED",
            )
            self.assertFalse(store.pending_path.exists())
            self.assertEqual(anchor.load()["sequence"], 1)
            reopened = store_module.JobLedgerStore(data_root, anchor)
            self.assertEqual(reopened.get_job("job-001").job_revision, 1)
            self.assertEqual(reopened.head, anchor.load())

    def test_reopen_completes_exact_pending_commit_after_replace_failure(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            stable_journal = store.journal_path.read_bytes()
            real_replace = os.replace

            def fail_journal_replace(source, destination) -> None:
                if (
                    Path(source) == store.pending_path
                    and Path(destination) == store.journal_path
                ):
                    raise OSError("simulated crash before journal replace")
                real_replace(source, destination)

            with (
                patch.object(os, "replace", side_effect=fail_journal_replace),
                self.assertRaises(JobLedgerError) as interrupted,
            ):
                store.create_job(**_create_arguments())

            self.assertEqual(interrupted.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(
                interrupted.exception.reason_code,
                "JOB_LEDGER_JOURNAL_COMMIT_FAILED",
            )
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertTrue(store.pending_path.is_file())
            self.assertEqual(
                stat.S_IMODE(store.pending_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(anchor.load()["sequence"], 1)

            reopened = store_module.JobLedgerStore(data_root, anchor)

            self.assertFalse(reopened.pending_path.exists())
            self.assertEqual(reopened.head, anchor.load())
            self.assertEqual(reopened.get_job("job-001").job_revision, 1)
            self.assertEqual(
                store_module.JobLedgerStore(data_root, anchor).get_job(
                    "job-001"
                ),
                reopened.get_job("job-001"),
            )

    def test_fixed_lock_is_nonblocking_and_stale_instance_never_writes_pending(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            first = store_module.JobLedgerStore(data_root, anchor)
            stale = store_module.JobLedgerStore(data_root, anchor)
            lock_path = first.directory / "transaction.lock"
            pending_path = first.directory / "journal.pending"
            self.assertTrue(lock_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

            stable_head = first.head
            stable_journal = first.journal_path.read_bytes()
            stable_snapshot = first.snapshot_path.read_bytes()
            stable_anchor = anchor.load()
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(JobLedgerError) as busy:
                    first.create_job(**_create_arguments())
                self.assertEqual(busy.exception.code, "RESOURCE_BUSY")
                self.assertEqual(
                    busy.exception.reason_code,
                    "JOB_LEDGER_TRANSACTION_BUSY",
                )
                self.assertFalse(pending_path.exists())
                self.assertEqual(first.head, stable_head)
                self.assertEqual(
                    first.journal_path.read_bytes(), stable_journal
                )
                self.assertEqual(
                    first.snapshot_path.read_bytes(), stable_snapshot
                )
                self.assertEqual(anchor.load(), stable_anchor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

            committed = first.create_job(**_create_arguments())
            self.assertEqual(committed.job_revision, 1)
            stable_journal = first.journal_path.read_bytes()
            stable_snapshot = first.snapshot_path.read_bytes()
            stable_anchor = anchor.load()
            stable_anchor_mutations = anchor.mutation_count

            with self.assertRaises(JobLedgerError) as rejected:
                stale.create_job(**_create_arguments())

            self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(
                rejected.exception.reason_code,
                "JOB_LEDGER_STALE_INSTANCE",
            )
            self.assertFalse(pending_path.exists())
            self.assertEqual(stale.list_jobs(), ())
            self.assertEqual(stale.head, stable_head)
            self.assertEqual(first.journal_path.read_bytes(), stable_journal)
            self.assertEqual(first.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.load(), stable_anchor)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_anchor_cas_failure_leaves_authoritative_store_unchanged(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            anchor = RejectNextIntegrityAnchorStore()
            store = store_module.JobLedgerStore(
                Path(temporary_directory).resolve() / "isolated-data",
                anchor,
            )
            pending_path = store.directory / "journal.pending"
            stable_jobs = store.list_jobs()
            stable_head = store.head
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor = anchor.load()
            stable_anchor_mutations = anchor.mutation_count
            anchor.reject_next = True

            with self.assertRaises(JobLedgerError) as rejected:
                store.create_job(**_create_arguments())

            self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(
                rejected.exception.reason_code,
                "JOB_LEDGER_HIGH_WATER_WRITE_FAILED",
            )
            self.assertFalse(pending_path.exists())
            self.assertEqual(store.list_jobs(), stable_jobs)
            self.assertEqual(store.head, stable_head)
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.load(), stable_anchor)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_migration_commit_rejects_unsafe_frozen_policy_before_write(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source_store = store_module.JobLedgerStore(
                root / "source-data",
                FakeIntegrityAnchorStore(),
            )
            valid_job = source_store.create_job(**_create_arguments())
            forged_job = replace(
                valid_job,
                frozen_policy={"api_key": "must-never-persist"},
            )
            event = source_store.events(valid_job.job_id)[0]
            receipt = MigrationReceiptV1(
                receipt_id="migration-direct-unsafe",
                source_path="task-queue/pending.json",
                source_sha256="f" * 64,
                source_schema=2,
                imported_job_ids=(forged_job.job_id,),
                committed_at="2026-08-20T01:05:00Z",
            )
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(root / "target-data", anchor)
            stable_head = store.head
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor_mutations = anchor.mutation_count

            with self.assertRaises(JobLedgerError) as rejected:
                store.commit_migration(
                    jobs=(forged_job,),
                    events=(event,),
                    receipt=receipt,
                )

            self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(store.head, stable_head)
            self.assertEqual(store.list_jobs(), ())
            self.assertIsNone(store.migration_receipt)
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_partial_failure_payload_rejects_secret_or_body_fields_before_write(
        self,
    ) -> None:
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(
                Path(temporary_directory).resolve() / "isolated-data",
                anchor,
            )
            store.create_job(**_create_arguments())
            store.transition(
                "job-001",
                expected_revision=1,
                new_state=JobState.QUEUED,
                changed_at="2026-08-20T01:10:01Z",
            )
            store.claim(
                "job-001",
                expected_revision=2,
                worker_instance_id="worker-secret-probe",
                operation_receipt=None,
                changed_at="2026-08-20T01:10:02Z",
            )
            stable_head = store.head
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor_mutations = anchor.mutation_count

            with self.assertRaises(JobLedgerError) as rejected:
                store.transition(
                    "job-001",
                    expected_revision=3,
                    new_state=JobState.PARTIAL,
                    changed_at="2026-08-20T01:10:03Z",
                    result_ref="result:partial:secret-probe",
                    partial_failures=(
                        {
                            "item_ref": "item:1",
                            "code": "REMOTE_UNKNOWN",
                            "transcript": "must-never-persist",
                        },
                    ),
                )

            self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(store.get_job("job-001").state, JobState.RUNNING)
            self.assertEqual(store.head, stable_head)
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_secure_authoritative_store_enforces_revision_idempotency_and_fencing(
        self,
    ) -> None:
        try:
            store_module = importlib.import_module("src.core.job_ledger.store")
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 5 job store is unavailable: {exc}")

        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            self.assertEqual(stat.S_IMODE(data_root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(store.directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(store.journal_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(store.snapshot_path.stat().st_mode),
                0o600,
            )

            created = store.create_job(**_create_arguments())
            self.assertEqual(created.state, JobState.PREPARED)
            self.assertEqual(created.job_revision, 1)
            self.assertEqual(created.event_sequence, 1)
            stable_head = store.head

            reused = store.create_job(**_create_arguments())
            self.assertEqual(reused, created)
            self.assertEqual(store.head, stable_head)

            conflicting = _create_arguments()
            conflicting["intent_payload_digest"] = "c" * 64
            with self.assertRaises(JobLedgerError) as conflict:
                store.create_job(**conflicting)
            self.assertEqual(conflict.exception.code, "CONFLICT")
            self.assertEqual(
                conflict.exception.reason_code,
                "IDEMPOTENCY_CONFLICT",
            )
            self.assertEqual(store.head, stable_head)

            queued = store.transition(
                "job-001",
                expected_revision=1,
                new_state=JobState.QUEUED,
                changed_at="2026-08-20T01:00:01Z",
            )
            self.assertEqual((queued.job_revision, queued.event_sequence), (2, 2))

            barrier = Barrier(3)
            successes: list[str] = []
            failures: list[str] = []

            def claim(worker: str) -> None:
                barrier.wait()
                try:
                    store.claim(
                        "job-001",
                        expected_revision=2,
                        worker_instance_id=worker,
                        operation_receipt=None,
                        changed_at="2026-08-20T01:00:02Z",
                    )
                except JobLedgerError as exc:
                    failures.append(exc.code)
                else:
                    successes.append(worker)

            threads = [
                Thread(target=claim, args=("worker-a",)),
                Thread(target=claim, args=("worker-b",)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["CONFLICT"])
            running = store.get_job("job-001")
            self.assertEqual(running.state, JobState.RUNNING)
            self.assertEqual(running.job_revision, 3)
            self.assertEqual(running.attempts[0].lease_epoch, 1)
            self.assertIn(
                running.attempts[0].worker_instance_id,
                {"worker-a", "worker-b"},
            )

            succeeded = store.transition(
                "job-001",
                expected_revision=3,
                new_state=JobState.SUCCEEDED,
                changed_at="2026-08-20T01:00:03Z",
                result_ref="result:sha256:" + "d" * 64,
            )
            self.assertEqual((succeeded.job_revision, succeeded.event_sequence), (4, 4))
            terminal_head = store.head
            with self.assertRaises(JobLedgerError) as terminal:
                store.transition(
                    "job-001",
                    expected_revision=4,
                    new_state=JobState.FAILED,
                    changed_at="2026-08-20T01:00:04Z",
                )
            self.assertEqual(terminal.exception.code, "CONFLICT")
            self.assertEqual(terminal.exception.reason_code, "TERMINAL_STATE_IMMUTABLE")
            self.assertEqual(store.head, terminal_head)
            self.assertEqual(
                [event.sequence for event in store.events("job-001")],
                [1, 2, 3, 4],
            )

            reopened = store_module.JobLedgerStore(data_root, anchor)
            self.assertEqual(reopened.get_job("job-001"), succeeded)
            self.assertEqual(reopened.head, terminal_head)

        with self.subTest(fault="symlink_data_root"):
            with TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory).resolve()
                outside = root / "outside"
                outside.mkdir(mode=0o700)
                linked = root / "linked-data"
                linked.symlink_to(outside, target_is_directory=True)
                anchor = FakeIntegrityAnchorStore()
                with self.assertRaises(JobLedgerError) as unsafe:
                    store_module.JobLedgerStore(linked, anchor)
                self.assertEqual(unsafe.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(anchor.mutation_count, 0)
                self.assertEqual(list(outside.iterdir()), [])

        with self.subTest(fault="journal_hmac"):
            with TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory).resolve() / "isolated-data"
                anchor = FakeIntegrityAnchorStore()
                store = store_module.JobLedgerStore(data_root, anchor)
                store.create_job(**_create_arguments())
                original = store.journal_path.read_bytes()
                damaged = original.replace(b"job-001", b"job-999", 1)
                self.assertNotEqual(damaged, original)
                store.journal_path.write_bytes(damaged)
                os.chmod(store.journal_path, 0o600)
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(JobLedgerError) as integrity:
                    store_module.JobLedgerStore(data_root, anchor)
                self.assertEqual(integrity.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)
                self.assertEqual(store.journal_path.read_bytes(), damaged)


if __name__ == "__main__":
    unittest.main()
