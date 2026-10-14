"""Phase 6 owner-loss recovery audit and explicit reclaim tests."""

from __future__ import annotations

from dataclasses import replace
import importlib
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ALPHA_SHA256 = "04583d2e020b0803cac58a764da8a4368a0ac64857da359a736beb16bfa23b15"


def _claim_then_exit(
    resource_root: str,
    result_queue,
) -> None:
    contracts = importlib.import_module("src.core.resource_control.contracts")
    locks = importlib.import_module("src.core.resource_control.locks")
    manager = locks.ResourceLockManager(Path(resource_root))
    lease = manager.claim(
        contracts.ResourceKind.MODEL,
        "model-install:sensevoice-small",
        contracts.LockOwner(
            pid=404,
            process_start="fake-start:404",
            installation_id="installation-test",
            operation_id="gui:model-install",
        ),
    )
    result_queue.put((str(lease.lock_path), lease.claimed_record_sha256))


class FakeProcessIdentityProbe:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.observed = []

    def is_alive(self, owner) -> bool:
        self.observed.append(owner)
        return self.alive


class Phase6ResourceRecoveryTests(unittest.TestCase):
    def test_owner_loss_requires_durable_audit_before_explicit_reclaim(self) -> None:
        contracts = importlib.import_module("src.core.resource_control.contracts")
        locks = importlib.import_module("src.core.resource_control.locks")
        try:
            service_module = importlib.import_module(
                "src.core.resource_control.service"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 6 resource recovery seam is unavailable: {exc}")

        with TemporaryDirectory() as temporary_directory:
            resource_root = Path(temporary_directory).resolve() / "resources"
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            owner_process = context.Process(
                target=_claim_then_exit,
                args=(str(resource_root), result_queue),
            )
            owner_process.start()
            owner_process.join(timeout=10)
            self.assertFalse(owner_process.is_alive())
            self.assertEqual(owner_process.exitcode, 0)
            lock_path_value, claimed_sha256 = result_queue.get(timeout=2)
            lock_path = Path(lock_path_value)
            self.assertTrue(lock_path.exists())
            claimed_bytes = lock_path.read_bytes()

            manager = locks.ResourceLockManager(resource_root)
            process_probe = FakeProcessIdentityProbe(alive=True)
            service = service_module.ResourceControlService(
                manager,
                process_probe=process_probe,
            )
            candidate = contracts.CASRevision(7, ALPHA_SHA256)
            with self.assertRaises(contracts.ResourceControlError) as still_alive:
                service.audit_recovery(
                    contracts.ResourceKind.MODEL,
                    "model-install:sensevoice-small",
                    audit_id="audit-001",
                    candidate_revision=candidate,
                    operation_receipt_id="operation-receipt-001",
                    audited_at="2026-08-20T06:00:00Z",
                )
            self.assertEqual(still_alive.exception.code, "RESOURCE_BUSY")
            self.assertEqual(still_alive.exception.exit_code, 22)
            self.assertEqual(lock_path.read_bytes(), claimed_bytes)

            process_probe.alive = False
            audit = service.audit_recovery(
                contracts.ResourceKind.MODEL,
                "model-install:sensevoice-small",
                audit_id="audit-001",
                candidate_revision=candidate,
                operation_receipt_id="operation-receipt-001",
                audited_at="2026-08-20T06:00:01Z",
            )
            self.assertEqual(audit.orphaned_record_sha256, claimed_sha256)
            self.assertEqual(audit.candidate_revision, candidate)
            self.assertEqual(audit.operation_receipt_id, "operation-receipt-001")
            audited_bytes = lock_path.read_bytes()
            self.assertNotEqual(audited_bytes, claimed_bytes)
            self.assertEqual(len(audited_bytes), locks.LOCK_RECORD_BYTES)
            inspection = manager.inspect(
                contracts.ResourceKind.MODEL,
                "model-install:sensevoice-small",
            )
            self.assertEqual(inspection.state, "recovery_required")
            self.assertEqual(inspection.recovery_audit_id, "audit-001")

            new_owner = contracts.LockOwner(
                pid=505,
                process_start="fake-start:505",
                installation_id="installation-test",
                operation_id="cli:model-install:reclaim",
            )
            for hostile_audit, hostile_candidate, hostile_receipt in (
                (replace(audit, operation_receipt_id="forged"), candidate, "forged"),
                (audit, contracts.CASRevision(8, ALPHA_SHA256), "operation-receipt-001"),
            ):
                with self.subTest(
                    hostile_candidate=hostile_candidate,
                    hostile_receipt=hostile_receipt,
                ):
                    stable_bytes = lock_path.read_bytes()
                    with self.assertRaises(contracts.ResourceControlError) as rejected:
                        service.reclaim(
                            contracts.ResourceKind.MODEL,
                            "model-install:sensevoice-small",
                            audit=hostile_audit,
                            candidate_revision=hostile_candidate,
                            operation_receipt_id=hostile_receipt,
                            new_owner=new_owner,
                        )
                    self.assertIn(rejected.exception.code, {"CONFLICT", "INTEGRITY_ERROR"})
                    self.assertEqual(lock_path.read_bytes(), stable_bytes)

            reclaimed = service.reclaim(
                contracts.ResourceKind.MODEL,
                "model-install:sensevoice-small",
                audit=audit,
                candidate_revision=candidate,
                operation_receipt_id="operation-receipt-001",
                new_owner=new_owner,
            )
            self.assertEqual(reclaimed.owner, new_owner)
            self.assertTrue(lock_path.exists())
            service.release(reclaimed)
            self.assertTrue(lock_path.exists())
            released = manager.inspect(
                contracts.ResourceKind.MODEL,
                "model-install:sensevoice-small",
            )
            self.assertEqual(released.state, "released")
            self.assertEqual(released.recovery_audit_id, "audit-001")
            self.assertGreaterEqual(len(process_probe.observed), 3)


if __name__ == "__main__":
    unittest.main()
