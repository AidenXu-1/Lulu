"""Phase 5 external-effect recovery and receipt inheritance tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.request

import src.core.job_ledger.contracts as contracts
from src.core.job_ledger.contracts import (
    JobLedgerError,
    JobState,
    OperationReceiptV1,
)
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


def _external_create_arguments(job_id: str) -> dict[str, object]:
    suffix = job_id.rsplit("-", 1)[-1]
    return {
        "job_id": job_id,
        "attempt_id": f"attempt-{suffix}",
        "lineage_id": f"lineage-{suffix}",
        "job_kind": "external_export",
        "input_ref": "artifact:sha256:" + suffix * 64,
        "frozen_policy": {"operation": "external_export"},
        "created_by": "phase5-test",
        "installation_id": "installation-001",
        "canonical_operation_id": "OP_EXTERNAL_EXPORT_V1",
        "idempotency_key": f"idem-{suffix}",
        "intent_payload_digest": suffix * 64,
        "created_at": "2026-08-20T04:00:00Z",
        "effect_fingerprint": "effect:external-write:vault",
        "account_ref": "account:tenant-001",
        "target_ref": "target:vault-001",
        "uncertain_response_state": "unknown_response",
    }


def _external_receipt(job_id: str) -> OperationReceiptV1:
    suffix = job_id.rsplit("-", 1)[-1]
    return OperationReceiptV1(
        operation_receipt_id=f"receipt-{suffix}",
        job_id=job_id,
        attempt_id=f"attempt-{suffix}",
        lineage_id=f"lineage-{suffix}",
        installation_id="installation-001",
        canonical_operation_id="OP_EXTERNAL_EXPORT_V1",
        effect_profile="EFFECT_EXTERNAL_WRITE",
        job_profile="JOB_EXTERNAL",
        approval_profile="APPROVAL_HUMAN_RECEIPT",
        idempotency_key=f"idem-{suffix}",
        intent_payload_digest=suffix * 64,
        created_at="2026-08-20T04:00:02Z",
    )


def _external_assertions(
    job_id: str,
    *,
    expected_job_revision: int,
):
    suffix = job_id.rsplit("-", 1)[-1]
    return contracts.ExternalOperationAssertionsV1(
        operation_receipt_id=f"receipt-{suffix}",
        expected_attempt_id=f"attempt-{suffix}",
        expected_lineage_id=f"lineage-{suffix}",
        expected_installation_id="installation-001",
        expected_canonical_operation_id="OP_EXTERNAL_EXPORT_V1",
        expected_effect_profile="EFFECT_EXTERNAL_WRITE",
        expected_job_profile="JOB_EXTERNAL",
        expected_approval_profile="APPROVAL_HUMAN_RECEIPT",
        expected_idempotency_key=f"idem-{suffix}",
        expected_effect_fingerprint="effect:external-write:vault",
        expected_account_ref="account:tenant-001",
        expected_target_ref="target:vault-001",
        expected_uncertain_response_state="unknown_response",
        expected_job_revision=expected_job_revision,
    )


def _retry_receipt(job_id: str) -> OperationReceiptV1:
    original = _external_receipt(job_id)
    return replace(
        original,
        operation_receipt_id=f"{original.operation_receipt_id}-retry",
        attempt_id=f"{original.attempt_id}-retry",
        created_at="2026-08-20T04:30:04Z",
    )


def _managed_external_history(
    store: JobLedgerStore,
    entries: tuple[tuple[str, JobState], ...],
) -> dict[str, contracts.JobV1]:
    jobs: list[contracts.JobV1] = []
    events: list[contracts.EventV1] = []
    for job_id, state in entries:
        arguments = _external_create_arguments(job_id)
        attempt_id = str(arguments["attempt_id"])
        lineage_id = str(arguments["lineage_id"])
        worker = "managed-worker" if state is JobState.RUNNING else None
        result_ref = (
            f"result:managed:{job_id}" if state is JobState.PARTIAL else None
        )
        partial_failures = (
            ({"item_ref": f"item:{job_id}", "code": "REMOTE_UNKNOWN"},)
            if state is JobState.PARTIAL
            else ()
        )
        attempt = contracts.AttemptV1(
            attempt_id=attempt_id,
            lineage_id=lineage_id,
            attempt_number=1,
            state=state,
            lease_epoch=1,
            worker_instance_id=worker,
            operation_receipt=_external_receipt(job_id),
            effect_fingerprint=str(arguments["effect_fingerprint"]),
            account_ref=str(arguments["account_ref"]),
            target_ref=str(arguments["target_ref"]),
            uncertain_response_state=str(
                arguments["uncertain_response_state"]
            ),
            created_at="2026-08-20T04:00:00Z",
            updated_at="2026-08-20T04:00:03Z",
        )
        job = contracts.JobV1(
            job_id=job_id,
            job_kind=str(arguments["job_kind"]),
            input_ref=str(arguments["input_ref"]),
            frozen_policy=dict(arguments["frozen_policy"]),
            created_by="managed-phase5-migration",
            installation_id=str(arguments["installation_id"]),
            canonical_operation_id=str(
                arguments["canonical_operation_id"]
            ),
            idempotency_key=str(arguments["idempotency_key"]),
            intent_payload_digest=str(arguments["intent_payload_digest"]),
            state=state,
            job_revision=1,
            event_sequence=1,
            current_attempt_id=attempt_id,
            lineage_id=lineage_id,
            created_at="2026-08-20T04:00:00Z",
            updated_at="2026-08-20T04:00:03Z",
            result_ref=result_ref,
            partial_failures=partial_failures,
            attempts=(attempt,),
        )
        jobs.append(job)
        events.append(
            contracts.EventV1(
                event_id=f"{job_id}:managed:1",
                job_id=job_id,
                attempt_id=attempt_id,
                sequence=1,
                job_revision=1,
                event_type="managed_receipt_migration",
                state=state,
                created_at="2026-08-20T04:00:03Z",
                result_ref=result_ref,
                partial_failures=partial_failures,
                lease_epoch=1 if worker is not None else None,
                worker_instance_id=worker,
            )
        )
    store.commit_migration(
        jobs=tuple(jobs),
        events=tuple(events),
        receipt=contracts.MigrationReceiptV1(
            receipt_id="managed-operation-receipts-v1",
            source_path="managed-authority/operation-receipts.json",
            source_sha256="9" * 64,
            source_schema=2,
            imported_job_ids=tuple(job.job_id for job in jobs),
            committed_at="2026-08-20T04:00:04Z",
        ),
    )
    return {job.job_id: job for job in jobs}


class Phase5ExternalEffectRecoveryTests(unittest.TestCase):
    def test_retry_persists_closed_receipt_inheritance_and_binds_reclaims(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = JobLedgerStore(data_root, anchor)
            _managed_external_history(store, (("job-2", JobState.PARTIAL),))
            store = JobLedgerStore(data_root, anchor)
            terminal = store.get_job("job-2")
            retried = JobControlService(store).retry_external(
                "job-2",
                assertions=_external_assertions(
                    "job-2",
                    expected_job_revision=terminal.job_revision,
                ),
                new_attempt_id="attempt-2-retry",
                changed_at="2026-08-20T04:30:04Z",
            )

            inheritance_type = getattr(contracts, "ReceiptInheritanceV1", None)
            self.assertIsNotNone(
                inheritance_type,
                "closed ReceiptInheritanceV1 contract is unavailable",
            )
            inheritance = store.receipt_inheritance(
                "job-2", "attempt-2-retry"
            )
            self.assertIsInstance(inheritance, inheritance_type)
            self.assertEqual(
                inheritance.as_dict(),
                {
                    "predecessor_operation_receipt_id": "receipt-2",
                    "predecessor_operation_receipt_digest": (
                        "afb756329237b653fe66684e09fcc275"
                        "66e1c2eb1b459aa8e4c23fece845159d"
                    ),
                    "predecessor_attempt_id": "attempt-2",
                    "job_id": "job-2",
                    "new_attempt_id": "attempt-2-retry",
                    "lineage_id": "lineage-2",
                    "installation_id": "installation-001",
                    "canonical_operation_id": "OP_EXTERNAL_EXPORT_V1",
                    "effect_profile": "EFFECT_EXTERNAL_WRITE",
                    "job_profile": "JOB_EXTERNAL",
                    "approval_profile": "APPROVAL_HUMAN_RECEIPT",
                    "idempotency_key": "idem-2",
                    "intent_payload_digest": "2" * 64,
                    "effect_fingerprint": "effect:external-write:vault",
                    "account_ref": "account:tenant-001",
                    "target_ref": "target:vault-001",
                    "uncertain_response_state": "unknown_response",
                },
            )
            reopened = JobLedgerStore(data_root, anchor)
            self.assertEqual(
                reopened.receipt_inheritance("job-2", "attempt-2-retry"),
                inheritance,
            )

            stable_job = reopened.get_job("job-2")
            stable_head = reopened.head
            stable_journal = reopened.journal_path.read_bytes()
            stable_snapshot = reopened.snapshot_path.read_bytes()
            stable_anchor = anchor.load()
            stable_anchor_mutations = anchor.mutation_count
            with self.assertRaises(JobLedgerError) as missing_receipt:
                reopened.claim(
                    "job-2",
                    expected_revision=retried.job_revision,
                    worker_instance_id="worker-retry",
                    operation_receipt=None,
                    changed_at="2026-08-20T04:30:05Z",
                )
            self.assertEqual(missing_receipt.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(reopened.get_job("job-2"), stable_job)
            self.assertEqual(reopened.head, stable_head)
            self.assertEqual(reopened.journal_path.read_bytes(), stable_journal)
            self.assertEqual(reopened.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.load(), stable_anchor)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

            inherited_receipt_mutations = {
                "job_id": "job-attacker",
                "attempt_id": "attempt-attacker",
                "lineage_id": "lineage-attacker",
                "installation_id": "installation-attacker",
                "canonical_operation_id": "OP_ATTACKER_V1",
                "effect_profile": "EFFECT_EXTERNAL_DELETE",
                "job_profile": "JOB_ATTACKER",
                "approval_profile": "APPROVAL_NONE",
                "idempotency_key": "idem-attacker",
                "intent_payload_digest": "f" * 64,
            }
            for field, hostile_value in inherited_receipt_mutations.items():
                with self.subTest(first_claim_field=field):
                    stable_job = reopened.get_job("job-2")
                    stable_head = reopened.head
                    stable_journal = reopened.journal_path.read_bytes()
                    stable_snapshot = reopened.snapshot_path.read_bytes()
                    stable_anchor = anchor.load()
                    stable_anchor_mutations = anchor.mutation_count
                    with self.assertRaises(JobLedgerError) as rejected:
                        reopened.claim(
                            "job-2",
                            expected_revision=retried.job_revision,
                            worker_instance_id="worker-retry",
                            operation_receipt=replace(
                                _retry_receipt("job-2"),
                                **{field: hostile_value},
                            ),
                            changed_at="2026-08-20T04:30:05Z",
                        )
                    self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
                    self.assertEqual(reopened.get_job("job-2"), stable_job)
                    self.assertEqual(reopened.head, stable_head)
                    self.assertEqual(
                        reopened.journal_path.read_bytes(), stable_journal
                    )
                    self.assertEqual(
                        reopened.snapshot_path.read_bytes(), stable_snapshot
                    )
                    self.assertEqual(anchor.load(), stable_anchor)
                    self.assertEqual(
                        anchor.mutation_count, stable_anchor_mutations
                    )

            claimed = reopened.claim(
                "job-2",
                expected_revision=retried.job_revision,
                worker_instance_id="worker-retry",
                operation_receipt=_retry_receipt("job-2"),
                changed_at="2026-08-20T04:30:05Z",
            )
            self.assertEqual(
                claimed.attempts[-1].operation_receipt,
                _retry_receipt("job-2"),
            )
            self.assertEqual(
                reopened.receipt_inheritance("job-2", "attempt-2-retry"),
                inheritance,
            )
            recovery = reopened.transition(
                "job-2",
                expected_revision=claimed.job_revision,
                new_state=JobState.RECOVERY_REQUIRED,
                changed_at="2026-08-20T04:30:06Z",
            )
            resumed = JobControlService(reopened).resume_external(
                "job-2",
                assertions=replace(
                    _external_assertions(
                        "job-2",
                        expected_job_revision=recovery.job_revision,
                    ),
                    operation_receipt_id="receipt-2-retry",
                    expected_attempt_id="attempt-2-retry",
                ),
                changed_at="2026-08-20T04:30:07Z",
            )
            replacement_mutations = {
                "operation_receipt_id": "receipt-attacker",
                **inherited_receipt_mutations,
                "created_at": "2099-01-01T00:00:00Z",
            }
            for field, hostile_value in replacement_mutations.items():
                with self.subTest(reclaim_field=field):
                    stable_job = reopened.get_job("job-2")
                    stable_head = reopened.head
                    stable_journal = reopened.journal_path.read_bytes()
                    stable_snapshot = reopened.snapshot_path.read_bytes()
                    stable_anchor = anchor.load()
                    stable_anchor_mutations = anchor.mutation_count
                    with self.assertRaises(JobLedgerError):
                        reopened.claim(
                            "job-2",
                            expected_revision=resumed.job_revision,
                            worker_instance_id="worker-reclaim",
                            operation_receipt=replace(
                                _retry_receipt("job-2"),
                                **{field: hostile_value},
                            ),
                            changed_at="2026-08-20T04:30:08Z",
                        )
                    self.assertEqual(reopened.get_job("job-2"), stable_job)
                    self.assertEqual(reopened.head, stable_head)
                    self.assertEqual(
                        reopened.journal_path.read_bytes(), stable_journal
                    )
                    self.assertEqual(
                        reopened.snapshot_path.read_bytes(), stable_snapshot
                    )
                    self.assertEqual(anchor.load(), stable_anchor)
                    self.assertEqual(
                        anchor.mutation_count, stable_anchor_mutations
                    )

            reclaimed = reopened.claim(
                "job-2",
                expected_revision=resumed.job_revision,
                worker_instance_id="worker-reclaim",
                operation_receipt=None,
                changed_at="2026-08-20T04:30:08Z",
            )
            self.assertEqual(
                reclaimed.attempts[-1].operation_receipt,
                _retry_receipt("job-2"),
            )
            self.assertEqual(
                reopened.receipt_inheritance("job-2", "attempt-2-retry"),
                inheritance,
            )

    def test_resume_reclaim_preserves_and_cannot_replace_operation_receipt(
        self,
    ) -> None:
        receipt_mutations = {
            "operation_receipt_id": "receipt-attacker",
            "job_id": "job-attacker",
            "attempt_id": "attempt-attacker",
            "lineage_id": "lineage-attacker",
            "installation_id": "installation-attacker",
            "canonical_operation_id": "OP_ATTACKER_V1",
            "effect_profile": "EFFECT_EXTERNAL_DELETE",
            "job_profile": "JOB_ATTACKER",
            "approval_profile": "APPROVAL_NONE",
            "idempotency_key": "idem-attacker",
            "intent_payload_digest": "f" * 64,
            "created_at": "2099-01-01T00:00:00Z",
        }
        for field, hostile_value in receipt_mutations.items():
            with self.subTest(field=field), TemporaryDirectory() as temporary_directory:
                anchor = FakeIntegrityAnchorStore()
                store = JobLedgerStore(
                    Path(temporary_directory).resolve() / "isolated-data",
                    anchor,
                )
                _managed_external_history(
                    store,
                    (("job-1", JobState.RECOVERY_REQUIRED),),
                )
                store = JobLedgerStore(store.data_root, anchor)
                recovery = store.get_job("job-1")
                resumed = JobControlService(store).resume_external(
                    "job-1",
                    assertions=_external_assertions(
                        "job-1",
                        expected_job_revision=recovery.job_revision,
                    ),
                    changed_at="2026-08-20T04:20:04Z",
                )
                stable_job = store.get_job("job-1")
                stable_head = store.head
                stable_journal = store.journal_path.read_bytes()
                stable_snapshot = store.snapshot_path.read_bytes()
                stable_anchor = anchor.load()
                stable_anchor_mutations = anchor.mutation_count

                with self.assertRaises(JobLedgerError) as rejected:
                    store.claim(
                        "job-1",
                        expected_revision=resumed.job_revision,
                        worker_instance_id="worker-reclaim",
                        operation_receipt=replace(
                            _external_receipt("job-1"),
                            **{field: hostile_value},
                        ),
                        changed_at="2026-08-20T04:20:05Z",
                    )

                self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(store.get_job("job-1"), stable_job)
                self.assertEqual(store.head, stable_head)
                self.assertEqual(store.journal_path.read_bytes(), stable_journal)
                self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
                self.assertEqual(anchor.load(), stable_anchor)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with TemporaryDirectory() as temporary_directory:
            anchor = FakeIntegrityAnchorStore()
            store = JobLedgerStore(
                Path(temporary_directory).resolve() / "isolated-data",
                anchor,
            )
            _managed_external_history(
                store,
                (("job-1", JobState.RECOVERY_REQUIRED),),
            )
            store = JobLedgerStore(store.data_root, anchor)
            recovery = store.get_job("job-1")
            resumed = JobControlService(store).resume_external(
                "job-1",
                assertions=_external_assertions(
                    "job-1",
                    expected_job_revision=recovery.job_revision,
                ),
                changed_at="2026-08-20T04:21:04Z",
            )

            reclaimed = store.claim(
                "job-1",
                expected_revision=resumed.job_revision,
                worker_instance_id="worker-reclaim",
                operation_receipt=None,
                changed_at="2026-08-20T04:21:05Z",
            )

            self.assertEqual(
                reclaimed.attempts[-1].operation_receipt,
                _external_receipt("job-1"),
            )

    def test_recovery_and_retry_inherit_authoritative_external_operation_exactly(
        self,
    ) -> None:
        assertions_type = getattr(
            contracts,
            "ExternalOperationAssertionsV1",
            None,
        )
        self.assertIsNotNone(
            assertions_type,
            "Phase 5 external operation assertions are unavailable",
        )

        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            anchor = FakeIntegrityAnchorStore()
            store = JobLedgerStore(data_root, anchor)
            managed = _managed_external_history(
                store,
                (
                    ("job-1", JobState.RUNNING),
                    ("job-2", JobState.PARTIAL),
                ),
            )
            running = managed["job-1"]
            self.assertEqual(running.state, JobState.RUNNING)

            effect_attempts: list[str] = []

            def forbidden_effect(*args, **kwargs):
                del args, kwargs
                effect_attempts.append("effect")
                raise AssertionError("external effect must not run")

            with (
                patch.object(socket, "socket", side_effect=forbidden_effect),
                patch.object(urllib.request, "urlopen", side_effect=forbidden_effect),
            ):
                reopened = JobLedgerStore(
                    data_root,
                    anchor,
                    recovery_at="2026-08-20T04:01:00Z",
                )
            recovery = reopened.get_job("job-1")
            self.assertEqual(recovery.state, JobState.RECOVERY_REQUIRED)
            self.assertEqual(recovery.job_revision, 2)
            self.assertEqual(recovery.attempts[-1].operation_receipt, _external_receipt("job-1"))
            self.assertEqual(effect_attempts, [])

            assertions = assertions_type(
                operation_receipt_id="receipt-1",
                expected_attempt_id="attempt-1",
                expected_lineage_id="lineage-1",
                expected_installation_id="installation-001",
                expected_canonical_operation_id="OP_EXTERNAL_EXPORT_V1",
                expected_effect_profile="EFFECT_EXTERNAL_WRITE",
                expected_job_profile="JOB_EXTERNAL",
                expected_approval_profile="APPROVAL_HUMAN_RECEIPT",
                expected_idempotency_key="idem-1",
                expected_effect_fingerprint="effect:external-write:vault",
                expected_account_ref="account:tenant-001",
                expected_target_ref="target:vault-001",
                expected_uncertain_response_state="unknown_response",
                expected_job_revision=2,
            )
            service = JobControlService(reopened)
            for hostile in (
                replace(assertions, expected_target_ref="target:attacker"),
                replace(assertions, expected_account_ref=""),
                replace(assertions, operation_receipt_id="receipt-other-job"),
            ):
                stable_head = reopened.head
                stable_journal = reopened.journal_path.read_bytes()
                stable_snapshot = reopened.snapshot_path.read_bytes()
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(JobLedgerError) as rejected:
                    service.resume_external(
                        "job-1",
                        assertions=hostile,
                        changed_at="2026-08-20T04:02:00Z",
                    )
                self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(reopened.head, stable_head)
                self.assertEqual(reopened.journal_path.read_bytes(), stable_journal)
                self.assertEqual(reopened.snapshot_path.read_bytes(), stable_snapshot)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

            with (
                patch.object(socket, "socket", side_effect=forbidden_effect),
                patch.object(urllib.request, "urlopen", side_effect=forbidden_effect),
                patch.object(subprocess, "Popen", side_effect=forbidden_effect),
            ):
                resumed = service.resume_external(
                    "job-1",
                    assertions=assertions,
                    changed_at="2026-08-20T04:03:00Z",
                )
            self.assertEqual(resumed.state, JobState.QUEUED)
            self.assertEqual(resumed.current_attempt_id, "attempt-1")
            self.assertEqual(
                resumed.attempts[-1].operation_receipt,
                _external_receipt("job-1"),
            )
            self.assertEqual(resumed.idempotency_key, "idem-1")
            self.assertEqual(effect_attempts, [])

            partial = reopened.get_job("job-2")
            retry_assertions = replace(
                assertions,
                operation_receipt_id="receipt-2",
                expected_attempt_id="attempt-2",
                expected_lineage_id="lineage-2",
                expected_idempotency_key="idem-2",
                expected_job_revision=partial.job_revision,
            )
            stable_head = reopened.head
            with self.assertRaises(JobLedgerError):
                service.retry(
                    "job-2",
                    expected_revision=partial.job_revision,
                    new_attempt_id="attempt-2-retry",
                    changed_at="2026-08-20T04:11:00Z",
                )
            self.assertEqual(reopened.head, stable_head)

            with (
                patch.object(socket, "socket", side_effect=forbidden_effect),
                patch.object(urllib.request, "urlopen", side_effect=forbidden_effect),
                patch.object(subprocess, "Popen", side_effect=forbidden_effect),
            ):
                retried = service.retry_external(
                    "job-2",
                    assertions=retry_assertions,
                    new_attempt_id="attempt-2-retry",
                    changed_at="2026-08-20T04:12:00Z",
                )
            self.assertEqual(retried.state, JobState.QUEUED)
            self.assertEqual(retried.current_attempt_id, "attempt-2-retry")
            self.assertEqual(retried.lineage_id, "lineage-2")
            self.assertEqual(retried.idempotency_key, "idem-2")
            self.assertEqual(
                retried.attempts[-1].effect_fingerprint,
                "effect:external-write:vault",
            )
            self.assertEqual(retried.attempts[-1].account_ref, "account:tenant-001")
            self.assertEqual(retried.attempts[-1].target_ref, "target:vault-001")
            self.assertEqual(
                retried.attempts[-1].uncertain_response_state,
                "unknown_response",
            )
            self.assertEqual(
                retried.attempts[0].operation_receipt,
                _external_receipt("job-2"),
            )
            self.assertEqual(effect_attempts, [])


if __name__ == "__main__":
    unittest.main()
