"""Temporary private files only; no system credentials or user data."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from src.core.local_integrity import LocalIntegrityStore
from src.core.job_ledger.storage_binding import prepare_core_task_store, REGISTRY_KEY
from test_phase5_job_ledger import _create_arguments


class LocalIntegrityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'Lulu'
        self.root.mkdir(mode=0o700)
        self.keys = LocalIntegrityStore(self.root / 'local-integrity-v1')

    def test_task_reopen_preserves_every_field_without_system_backend(self):
        (self.root / 'task-storage-v2').mkdir(mode=0o700)
        with patch('src.core.license.store.StrictKeyringStore._keyring', side_effect=AssertionError('system storage forbidden')):
            first = prepare_core_task_store(self.root / 'task-storage-v2', 'installation-001', self.keys, legacy_storage=False)
            first.create_job(**_create_arguments())
            before = first.list_jobs()
            reopened = prepare_core_task_store(self.root / 'task-storage-v2', 'installation-001', LocalIntegrityStore(self.keys.root), legacy_storage=False)
            self.assertEqual(reopened.list_jobs(), before)

    def test_atomic_compare_and_set_conflict_does_not_overwrite(self):
        self.keys.create_core_task_material(REGISTRY_KEY, 'original')
        with self.assertRaises(Exception):
            self.keys.replace_core_task_material(REGISTRY_KEY, 'other', 'replacement')
        self.assertEqual(LocalIntegrityStore(self.keys.root).get_text(REGISTRY_KEY), 'original')

    def test_credentials_and_legacy_ticket_are_rejected(self):
        for name in ('openai', 'douyin-live', 'feishu_wiki', 'license_cache_v1', 'lulu_agent_cli_installation_ticket_v1'):
            with self.subTest(name=name), self.assertRaises(Exception):
                self.keys.write_new(name, 'must not become a plain credential')

    def test_symlink_and_hardlink_cannot_redirect_material(self):
        self.keys.write_new(REGISTRY_KEY, 'original')
        path = self.keys.path_for(REGISTRY_KEY)
        outside = self.root / 'outside'; outside.write_text('protected')
        path.unlink(); path.symlink_to(outside)
        with self.assertRaises(Exception): self.keys.get_text(REGISTRY_KEY)
        with self.assertRaises(Exception): self.keys.set_text(REGISTRY_KEY, 'bad')
        self.assertEqual(outside.read_text(), 'protected')
        path.unlink(); os.link(outside, path)
        with self.assertRaises(Exception): self.keys.get_text(REGISTRY_KEY)

    def test_disk_full_preserves_previous_material(self):
        self.keys.write_new(REGISTRY_KEY, 'original')
        with patch('src.core.local_integrity.os.replace', side_effect=OSError(28, 'disk full')):
            with self.assertRaises(Exception): self.keys.set_text(REGISTRY_KEY, 'replacement')
        self.assertEqual(self.keys.get_text(REGISTRY_KEY), 'original')

    def test_private_modes_and_missing_existing_key_are_not_repaired(self):
        self.keys.write_new(REGISTRY_KEY, 'original')
        path = self.keys.path_for(REGISTRY_KEY)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.keys.root.stat().st_mode & 0o777, 0o700)
        path.chmod(0o644)
        with self.assertRaises(Exception): self.keys.get_text(REGISTRY_KEY)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_authenticated_pending_commit_recovers_with_local_anchor(self):
        root = self.root/'task-storage-v2'; root.mkdir(mode=0o700)
        store = prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False)
        real_replace = os.replace
        def interrupted(source, target, **kwargs):
            if source == store.pending_path and target == store.journal_path:
                raise OSError(28, 'interrupted journal replacement')
            return real_replace(source, target, **kwargs)
        with patch('src.core.job_ledger.store.os.replace', side_effect=interrupted), self.assertRaises(Exception):
            store.create_job(**_create_arguments())
        self.assertTrue(store.pending_path.exists())
        reopened = prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False)
        self.assertEqual([j.job_id for j in reopened.list_jobs()], ['job-001'])
        self.assertFalse(reopened.pending_path.exists())
        before = reopened.journal_path.read_bytes()
        reopened.snapshot_path.unlink()
        with self.assertRaises(Exception): prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False)
        self.assertFalse(reopened.snapshot_path.exists())
        self.assertEqual(reopened.journal_path.read_bytes(), before)

    def test_uncertain_anchor_write_keeps_pending_for_confirmed_commit_recovery(self):
        root = self.root/'task-storage-v2'; root.mkdir(mode=0o700)
        store = prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False)
        replace = self.keys.replace_core_task_material
        def uncertain(*args):
            replace(*args)
            raise OSError(28, 'directory fsync failed after replacement')
        with patch.object(self.keys, 'replace_core_task_material', side_effect=uncertain), self.assertRaises(Exception):
            store.create_job(**_create_arguments())
        self.assertTrue(store.pending_path.exists())
        self.assertEqual([j.job_id for j in prepare_core_task_store(root, 'installation-001', self.keys,
            legacy_storage=False).list_jobs()], ['job-001'])

    def test_incomplete_owned_pending_write_is_cleaned_without_changing_jobs(self):
        root = self.root/'task-storage-v2'; root.mkdir(mode=0o700)
        store = prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False)
        before = store.journal_path.read_bytes()
        with patch('src.core.job_ledger.store.os.write', side_effect=OSError(28, 'disk full')):
            with self.assertRaises(Exception): store.create_job(**_create_arguments())
        self.assertFalse(store.pending_path.exists())
        self.assertEqual(store.journal_path.read_bytes(), before)
        self.assertEqual(prepare_core_task_store(root, 'installation-001', self.keys, legacy_storage=False).list_jobs(), ())
