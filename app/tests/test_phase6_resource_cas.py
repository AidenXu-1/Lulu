"""Phase 6 revision-and-content-hash CAS persistence tests."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest


ALPHA_SHA256 = "04583d2e020b0803cac58a764da8a4368a0ac64857da359a736beb16bfa23b15"
BETA_SHA256 = "249798473e4417ca7f4ccd900846ecc3eaeed073e1ce36a11dd4473c57cf1329"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class Phase6ResourceCASTests(unittest.TestCase):
    def test_cas_requires_resource_lease_and_exact_revision_hash(self) -> None:
        contracts = importlib.import_module("src.core.resource_control.contracts")
        locks = importlib.import_module("src.core.resource_control.locks")
        try:
            cas = importlib.import_module("src.core.resource_control.cas")
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 6 resource CAS seam is unavailable: {exc}")

        with TemporaryDirectory() as temporary_directory:
            resource_root = Path(temporary_directory).resolve() / "resources"
            lock_manager = locks.ResourceLockManager(resource_root)
            store = cas.ResourceCASStore(resource_root, lock_manager)
            owner = contracts.LockOwner(
                pid=303,
                process_start="fake-start:303",
                installation_id="installation-test",
                operation_id="gui:config-write",
            )
            lease = lock_manager.claim(
                contracts.ResourceKind.CONFIG,
                "settings",
                owner,
            )
            missing = contracts.CASRevision(0, EMPTY_SHA256)
            first = store.write(
                lease,
                expected=missing,
                content=b"phase6-alpha-record",
            )
            self.assertEqual(
                first.revision,
                contracts.CASRevision(1, ALPHA_SHA256),
            )
            self.assertEqual(first.content, b"phase6-alpha-record")
            self.assertEqual(
                stat.S_IMODE(store.record_directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE(first.record_path.stat().st_mode), 0o600)
            first_inode = first.record_path.stat().st_ino

            second = store.write(
                lease,
                expected=first.revision,
                content=b"phase6-beta-record",
            )
            self.assertEqual(
                second.revision,
                contracts.CASRevision(2, BETA_SHA256),
            )
            self.assertEqual(second.content, b"phase6-beta-record")
            self.assertNotEqual(second.record_path.stat().st_ino, first_inode)
            self.assertEqual(store.read(contracts.ResourceKind.CONFIG, "settings"), second)

            stable_bytes = second.record_path.read_bytes()
            stable_inode = second.record_path.stat().st_ino
            for stale in (
                contracts.CASRevision(1, ALPHA_SHA256),
                contracts.CASRevision(2, ALPHA_SHA256),
            ):
                with self.subTest(stale=stale):
                    with self.assertRaises(contracts.ResourceControlError) as conflict:
                        store.write(
                            lease,
                            expected=stale,
                            content=b"forbidden-overwrite",
                        )
                    self.assertEqual(conflict.exception.code, "CONFLICT")
                    self.assertEqual(conflict.exception.exit_code, 21)
                    self.assertEqual(second.record_path.read_bytes(), stable_bytes)
                    self.assertEqual(second.record_path.stat().st_ino, stable_inode)

            lock_manager.release(lease)
            with self.assertRaises(contracts.ResourceControlError) as released:
                store.write(
                    lease,
                    expected=second.revision,
                    content=b"forbidden-with-released-lease",
                )
            self.assertEqual(released.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(second.record_path.read_bytes(), stable_bytes)

            for fault in ("mode", "schema", "symlink"):
                with self.subTest(fault=fault):
                    fault_id = f"record-{fault}"
                    fault_lease = lock_manager.claim(
                        contracts.ResourceKind.VOICE,
                        fault_id,
                        owner,
                    )
                    record_path = store.record_path(
                        contracts.ResourceKind.VOICE,
                        fault_id,
                    )
                    if fault == "symlink":
                        outside = resource_root / f"outside-{fault}.json"
                        outside.write_bytes(b"outside")
                        record_path.symlink_to(outside)
                    else:
                        created = store.write(
                            fault_lease,
                            expected=missing,
                            content=b"phase6-alpha-record",
                        )
                        if fault == "mode":
                            os.chmod(created.record_path, 0o644)
                        else:
                            created.record_path.write_bytes(b"{}\n")
                            os.chmod(created.record_path, 0o600)
                    with self.assertRaises(contracts.ResourceControlError) as invalid:
                        store.write(
                            fault_lease,
                            expected=missing,
                            content=b"phase6-beta-record",
                        )
                    self.assertEqual(invalid.exception.code, "INTEGRITY_ERROR")
                    self.assertEqual(invalid.exception.exit_code, 60)
                    if fault == "symlink":
                        self.assertEqual(outside.read_bytes(), b"outside")
                    lock_manager.release(fault_lease)


if __name__ == "__main__":
    unittest.main()
