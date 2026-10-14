"""Only canonical Core accounts can reach the verified native implementation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from src.core.cli_runtime.core_task_keychain import CoreTaskKeyringStore, CORE


class CoreTaskKeychainGateTests(unittest.TestCase):
    def test_core_registry_key_and_anchor_reads_use_verified_native_path(self):
        store = CoreTaskKeyringStore()
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(CORE)), \
             patch.object(store, '_keyring', return_value=object()), \
             patch('src.core.cli_runtime.core_task_keychain.NativeCoreTaskStore') as native:
            native.return_value.read.return_value='verified'
            for key in ('lulu_core_job_registry_v1','lulu_core_job_key_v1:scope','lulu_core_job_anchor_v1:scope'):
                self.assertEqual(store.get_text(key),'verified')
                native.return_value.read.assert_called_with(key)

    def test_license_read_keeps_legacy_backend(self):
        from unittest.mock import Mock
        backend=Mock()
        backend.get_password.return_value='legacy'
        store=CoreTaskKeyringStore()
        with patch.object(store,'_keyring',return_value=backend), \
             patch('src.core.cli_runtime.core_task_keychain.NativeCoreTaskStore',side_effect=AssertionError('wrong route')):
            self.assertEqual(store.get_text('license_cache_v1'),'legacy')

    def test_canonical_core_routes_writes_to_the_verified_native_implementation(self):
        store = CoreTaskKeyringStore()
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(CORE)), \
             patch.object(store, "_keyring", return_value=object()), \
             patch('src.core.cli_runtime.core_task_keychain.NativeCoreTaskStore') as native:
            store.create_core_task_material("lulu_core_job_registry_v1", "synthetic")
            native.return_value.create.assert_called_once_with("lulu_core_job_registry_v1", "synthetic")
            store.replace_core_task_material("lulu_core_job_registry_v1", "before", "after")
            native.return_value.replace.assert_called_once_with("lulu_core_job_registry_v1", "before", "after")

    def test_canonical_core_rejects_foreign_or_invalid_accounts_before_backend(self):
        store=CoreTaskKeyringStore()
        with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(CORE)), \
             patch.object(store,'_keyring',side_effect=AssertionError('OS access forbidden')):
            for key in ('license_cache_v1','lulu_core_job_key_v1:../bad','lulu_core_job_anchor_v1:'):
                with self.subTest(key=key),self.assertRaisesRegex(Exception,'JOB_CORE_ACCOUNT_INVALID'):
                    store.create_core_task_material(key,'synthetic')

    def test_native_creation_failure_remains_visible_and_never_falls_back(self):
        from src.core.license.store import SecureStoreError
        store=CoreTaskKeyringStore()
        with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(CORE)), \
             patch.object(store,'_keyring',return_value=object()), \
             patch.object(store,'write_new',side_effect=AssertionError('fallback forbidden')), \
             patch('src.core.cli_runtime.core_task_keychain.NativeCoreTaskStore') as native:
            native.return_value.create.side_effect=SecureStoreError('JOB_CORE_MATERIAL_CREATED_UNVERIFIED')
            with self.assertRaisesRegex(SecureStoreError,'JOB_CORE_MATERIAL_CREATED_UNVERIFIED'):
                store.create_core_task_material('lulu_core_job_registry_v1','synthetic')
            native.return_value.create.assert_called_once()

    def test_gui_and_cli_cannot_enter_core_writer(self):
        for executable in ("Lulu", "lulu-agent-cli"):
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(CORE.parent / executable)), \
                 patch.object(CoreTaskKeyringStore, "_keyring", side_effect=AssertionError("OS access forbidden")):
                with self.assertRaisesRegex(Exception, "JOB_CORE_IDENTITY_REQUIRED"):
                    CoreTaskKeyringStore().create_core_task_material("lulu_core_job_registry_v1", "synthetic")
