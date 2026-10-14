from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


class TrustedTestSafeE2EIsolationTests(unittest.TestCase):
    def test_run_root_and_every_declared_runtime_path_are_private_and_contained(self) -> None:
        from src.trusted_test_safe_e2e import create_private_run_root, isolated_environment

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "runs"
            base.mkdir(mode=0o700)
            os.chmod(base, 0o700)
            root = create_private_run_root(base)
            self.assertEqual(stat.S_IMODE(root.lstat().st_mode), 0o700)
            env = isolated_environment(root)
            for value in env.values():
                candidate = Path(value)
                self.assertTrue(candidate.is_relative_to(root), value)
            for child in ("authority", "credentials", "home", "cache", "tmp", "input", "logs", "receipts"):
                path = root / child
                self.assertTrue(path.is_dir())
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o700)

    def test_outside_write_target_is_rejected_before_parent_creation(self) -> None:
        from src.trusted_test_safe_e2e import SafeE2EError, assert_write_target

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            outside = Path(temp) / "outside" / "result.json"
            with self.assertRaisesRegex(SafeE2EError, "WRITE_OUTSIDE_ROOT"):
                assert_write_target(root, outside)
            self.assertFalse(outside.parent.exists())

    def test_credentials_are_dedicated_ticket_last_and_receipt_has_no_secret(self) -> None:
        from src.trusted_test_safe_e2e import (
            FileCredentialStore,
            SAFE_CREDENTIAL_ACCOUNTS,
            provision_test_credentials,
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            credentials = root / "credentials"
            credentials.mkdir(parents=True, mode=0o700)
            os.chmod(root, 0o700)
            os.chmod(credentials, 0o700)
            store = FileCredentialStore(credentials / "store.json")
            secrets = {
                "license": "SECRET-LICENSE-CACHE",
                "hmac": "SECRET-HMAC-KEY",
                "anchor": "SECRET-ANCHOR",
                "ticket": "SECRET-TEST-TICKET",
            }
            receipt = provision_test_credentials(store, secrets)

            self.assertEqual(tuple(store.write_order), SAFE_CREDENTIAL_ACCOUNTS)
            self.assertEqual(store.read(SAFE_CREDENTIAL_ACCOUNTS[-1]), secrets["ticket"])
            public = json.dumps(receipt, sort_keys=True)
            for secret in secrets.values():
                self.assertNotIn(secret, public)
            self.assertEqual(stat.S_IMODE(store.path.lstat().st_mode), 0o600)

    def test_store_refuses_unknown_or_formal_account_without_file_creation(self) -> None:
        from src.trusted_test_safe_e2e import FileCredentialStore, SafeE2EError

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            target = root / "credentials.json"
            store = FileCredentialStore(target)
            with self.assertRaisesRegex(SafeE2EError, "CREDENTIAL_ACCOUNT_FORBIDDEN"):
                store.write("license_cache_v1", "formal-value")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
