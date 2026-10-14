"""Existing ledger validation never constructs a writable store or repairs files."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments
from src.core.job_ledger import store as ledger


class JobLedgerReadOnlyVerificationTests(unittest.TestCase):
    def test_stable_scope_preserves_old_and_new_jobs_across_reopen_and_upgrade(self):
        """Storage primitive evidence; does not stand in for installer binding."""
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "jobs"
            keys = FakeIntegrityAnchorStore()
            first = ledger.JobLedgerStore(root, keys)
            first.create_job(**_create_arguments())
            old_job = first.list_jobs()[0]
            old_prefix = first.journal_path.read_bytes()
            ledger.verify_existing_ledger(root, keys)
            arguments = dict(_create_arguments(), job_id="job-002",
                attempt_id="attempt-002", lineage_id="lineage-002",
                idempotency_key="idem-002", installation_id="installation-002")
            first.create_job(**arguments)
            expected = first.list_jobs()
            del first
            for generation in ("restart", "next-upgrade"):
                with self.subTest(generation=generation):
                    proof = ledger.verify_existing_ledger(root, keys)
                    self.assertEqual(proof["installation_ids"],
                        ["installation-001", "installation-002"])
                    reopened = ledger.JobLedgerStore(root, keys)
                    self.assertEqual(reopened.list_jobs(), expected)
                    self.assertEqual(reopened.list_jobs()[0], old_job)
                    self.assertTrue(reopened.journal_path.read_bytes().startswith(old_prefix))
                    self.assertEqual(proof["job_count"], 2)
                    del reopened

    def test_existing_authenticated_ledger_is_verified_without_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "jobs"
            keys = FakeIntegrityAnchorStore()
            store = ledger.JobLedgerStore(root, keys)
            store.create_job(**_create_arguments())
            before = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode)
                for p in root.rglob("*") if p.is_file()}
            writes = keys.mutation_count
            with patch.object(ledger.JobLedgerStore, "__init__", side_effect=AssertionError("writable store forbidden")):
                proof = ledger.verify_existing_ledger(root, keys)
            self.assertEqual(proof["installation_ids"], ["installation-001"])
            self.assertEqual(proof["job_count"], 1)
            self.assertEqual(proof["head"], store.head)
            self.assertEqual(keys.mutation_count, writes)
            self.assertEqual(before, {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode)
                for p in root.rglob("*") if p.is_file()})

    def test_invalid_or_incomplete_existing_records_are_never_repaired(self):
        for fault in ("wrong_key", "rollback", "pending", "missing_snapshot", "tampered_snapshot"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory).resolve() / "jobs"
                keys = FakeIntegrityAnchorStore()
                store = ledger.JobLedgerStore(root, keys)
                store.create_job(**_create_arguments())
                if fault == "wrong_key":
                    keys.hmac_key = lambda: b"unrelated-key-value-only-for-tests"
                elif fault == "rollback":
                    keys.state["sequence"] += 1
                elif fault == "pending":
                    store.pending_path.write_bytes(b"unsettled")
                    store.pending_path.chmod(0o600)
                elif fault == "missing_snapshot":
                    store.snapshot_path.unlink()
                else:
                    store.snapshot_path.write_bytes(b"{}\n")
                before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                writes = keys.mutation_count
                with self.assertRaises(Exception):
                    ledger.verify_existing_ledger(root, keys)
                self.assertEqual(writes, keys.mutation_count)
                self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
