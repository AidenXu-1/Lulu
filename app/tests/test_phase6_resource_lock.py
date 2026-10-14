"""Phase 6 owner-bound, non-blocking cross-process resource locks."""

from __future__ import annotations

from dataclasses import fields, replace
import importlib
import multiprocessing
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest


def _contend_for_lock(
    resource_root: str,
    resource_kind: str,
    resource_id: str,
    owner_values: dict[str, object],
    result_queue,
) -> None:
    contracts = importlib.import_module("src.core.resource_control.contracts")
    locks = importlib.import_module("src.core.resource_control.locks")
    manager = locks.ResourceLockManager(Path(resource_root))
    try:
        lease = manager.claim(
            contracts.ResourceKind(resource_kind),
            resource_id,
            contracts.LockOwner(**owner_values),
        )
    except contracts.ResourceControlError as exc:
        result_queue.put((exc.code, exc.exit_code, exc.reason_code))
    else:
        manager.release(lease)
        result_queue.put(("ACQUIRED", 0, "unexpected"))


class Phase6ResourceLockTests(unittest.TestCase):
    def test_owner_bound_lock_is_nonblocking_fixed_and_fail_closed(self) -> None:
        try:
            contracts = importlib.import_module(
                "src.core.resource_control.contracts"
            )
            locks = importlib.import_module("src.core.resource_control.locks")
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 6 resource lock seam is unavailable: {exc}")

        self.assertEqual(
            {kind.value for kind in contracts.ResourceKind},
            {
                "config",
                "transcript-folder",
                "voice",
                "job",
                "model",
                "tts-engine",
                "feishu-target",
                "update",
            },
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.LockOwner)),
            ("pid", "process_start", "installation_id", "operation_id"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.LeaseToken)),
            ("job_id", "lease_epoch", "worker_instance_id"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.CASRevision)),
            ("revision", "content_sha256"),
        )

        with TemporaryDirectory() as temporary_directory:
            resource_root = Path(temporary_directory).resolve() / "resources"
            manager = locks.ResourceLockManager(resource_root)
            owner_gui = contracts.LockOwner(
                pid=101,
                process_start="fake-start:101",
                installation_id="installation-test",
                operation_id="gui:model-install",
            )
            owner_cli = contracts.LockOwner(
                pid=202,
                process_start="fake-start:202",
                installation_id="installation-test",
                operation_id="cli:model-install",
            )
            lease = manager.claim(
                contracts.ResourceKind.MODEL,
                "sensevoice-small",
                owner_gui,
            )
            self.assertEqual(stat.S_IMODE(resource_root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(manager.lock_directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE(lease.lock_path.stat().st_mode), 0o600)
            self.assertEqual(lease.lock_path.stat().st_size, locks.LOCK_RECORD_BYTES)

            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            contender = context.Process(
                target=_contend_for_lock,
                args=(
                    str(resource_root),
                    contracts.ResourceKind.MODEL.value,
                    "sensevoice-small",
                    {
                        "pid": owner_cli.pid,
                        "process_start": owner_cli.process_start,
                        "installation_id": owner_cli.installation_id,
                        "operation_id": owner_cli.operation_id,
                    },
                    result_queue,
                ),
            )
            contender.start()
            contender.join(timeout=10)
            self.assertFalse(contender.is_alive())
            self.assertEqual(contender.exitcode, 0)
            self.assertEqual(
                result_queue.get(timeout=2),
                ("RESOURCE_BUSY", 22, "RESOURCE_LOCK_HELD"),
            )

            with self.assertRaises(contracts.ResourceControlError) as forged:
                manager.release(replace(lease, owner=owner_cli))
            self.assertEqual(forged.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(lease.lock_path.stat().st_size, locks.LOCK_RECORD_BYTES)
            manager.release(lease)
            self.assertTrue(lease.lock_path.exists())

            release_manager = locks.ResourceLockManager(resource_root)
            acquired_after_release = release_manager.claim(
                contracts.ResourceKind.MODEL,
                "sensevoice-small",
                owner_cli,
            )
            release_manager.release(acquired_after_release)

            for fault in ("mode", "size", "schema"):
                with self.subTest(fault=fault):
                    fault_id = f"fault-{fault}"
                    fault_manager = locks.ResourceLockManager(resource_root)
                    fault_lease = fault_manager.claim(
                        contracts.ResourceKind.CONFIG,
                        fault_id,
                        owner_gui,
                    )
                    fault_manager.release(fault_lease)
                    if fault == "mode":
                        os.chmod(fault_lease.lock_path, 0o644)
                    elif fault == "size":
                        fault_lease.lock_path.write_bytes(b"{}")
                        os.chmod(fault_lease.lock_path, 0o600)
                    else:
                        invalid = b"{}\n"
                        fault_lease.lock_path.write_bytes(
                            invalid
                            + b" " * (locks.LOCK_RECORD_BYTES - len(invalid))
                        )
                        os.chmod(fault_lease.lock_path, 0o600)
                    with self.assertRaises(contracts.ResourceControlError) as invalid:
                        locks.ResourceLockManager(resource_root).claim(
                            contracts.ResourceKind.CONFIG,
                            fault_id,
                            owner_cli,
                        )
                    self.assertEqual(invalid.exception.code, "INTEGRITY_ERROR")
                    self.assertEqual(invalid.exception.exit_code, 60)

            symlink_manager = locks.ResourceLockManager(resource_root)
            symlink_path = symlink_manager.lock_path(
                contracts.ResourceKind.UPDATE,
                "symlink-probe",
            )
            outside = resource_root / "outside.lock"
            outside.write_bytes(b"outside")
            symlink_path.symlink_to(outside)
            with self.assertRaises(contracts.ResourceControlError) as symlinked:
                symlink_manager.claim(
                    contracts.ResourceKind.UPDATE,
                    "symlink-probe",
                    owner_cli,
                )
            self.assertEqual(symlinked.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(outside.read_bytes(), b"outside")


if __name__ == "__main__":
    unittest.main()
