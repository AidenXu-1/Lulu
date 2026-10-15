"""Isolated signed Apps, in-memory secrets and real temporary task ledgers."""
import json
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import unittest

import test_standalone_install_source as fixtures
from test_generation9_formal_install_public import MemoryKeys
from test_core_storage_binding import CoreMemoryKeys
from test_phase5_job_ledger import _create_arguments


class StandaloneHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()/'generation'; self.root.mkdir(mode=0o700)
        self.keys = CoreMemoryKeys()
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        self.store = prepare_core_task_store(self.root, 'product-installation', self.keys, legacy_storage=False)


    def test_verified_completed_live_ledger_without_result_remains_read_only(self):
        from src.core.job_ledger.contracts import JobState
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        self.store.create_job(**dict(_create_arguments(), installation_id='product-installation',
                                     job_kind='live_recording'))
        self.store.transition('job-001', expected_revision=1, new_state=JobState.QUEUED,
                              changed_at='2026-08-20T01:00:01Z')
        self.store.claim('job-001', expected_revision=2, worker_instance_id='fixture',
                         operation_receipt=None, changed_at='2026-08-20T01:00:02Z')
        self.store.transition('job-001', expected_revision=3, new_state=JobState.SUCCEEDED,
                              changed_at='2026-08-20T01:00:03Z', result_ref=None)
        from src.core.agent_entry.product_transaction import snapshot_digest
        before, writes = snapshot_digest(self.root), list(self.keys.writes)
        row, = verified_terminal_rows(self.root, 'product-installation', self.keys)
        self.assertEqual((row['job_kind'], row['state'], row['result_ref']),
                         ('live_recording', 'succeeded', None))
        self.assertEqual(snapshot_digest(self.root), before)
        self.assertEqual(writes, self.keys.writes)


    def test_empty_generation_with_orphaned_key_still_blocks_upgrade(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        from src.core.job_ledger.storage_binding import KEY_PREFIX, ANCHOR_PREFIX
        for prefix in (KEY_PREFIX, ANCHOR_PREFIX):
            with self.subTest(prefix=prefix):
                empty = Path(self.tmp.name).resolve()/('orphan-'+prefix.split('_')[3])
                empty.mkdir(mode=0o700, exist_ok=True)
                keys = CoreMemoryKeys()
                keys.values[prefix+'core-product-installation'] = 'preserve this record'
                before = dict(keys.values)
                with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
                    verified_terminal_rows(empty, 'product-installation', keys)
                self.assertEqual(keys.values, before)
                self.assertEqual(keys.writes, [])


    def test_generation_created_during_secret_checks_is_not_discarded(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        from src.core.job_ledger.storage_binding import KEY_PREFIX
        empty = Path(self.tmp.name).resolve()/'concurrent-generation'
        empty.mkdir(mode=0o700)
        keys = CoreMemoryKeys()
        read = keys.get_text
        def read_with_new_file(name):
            if name.startswith(KEY_PREFIX):
                (empty/'new-history').write_bytes(b'preserve concurrent history')
            return read(name)
        with patch.object(keys, 'get_text', side_effect=read_with_new_file):
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
                verified_terminal_rows(empty, 'product-installation', keys)
        self.assertEqual((empty/'new-history').read_bytes(), b'preserve concurrent history')
        self.assertEqual(keys.writes, [])


class StandaloneFlowTests(unittest.TestCase):
    def setUp(self):
        fixtures.StandaloneInstallSourceTests.setUp(self)
        fixtures.StandaloneInstallSourceTests.prepare(self)
        self.home = self.root/'home'; self.home.mkdir()
        self.product = self.home/'Library/Application Support/Lulu'; self.product.mkdir(parents=True)
        self.keys = MemoryKeys()

    def prepare(self):
        from src.core.agent_entry.standalone_state import prepare_install_state
        return prepare_install_state(self.app, product_root=self.product, keys=self.keys)


    def test_interrupted_first_authorization_empty_generation_can_handover(self):
        from src.core.agent_entry.standalone_state import prepare_install_state
        from src.core.agent_entry.standalone_source import DESCRIPTOR
        from src.core.agent_entry.standalone_history import capture_history
        from src.core.agent_entry.installed_materials import MATERIALS_DIR, _private_json
        from src.core.agent_entry.task_handover import FILENAME, open_handover
        from src.core.agent_entry.product_transaction import snapshot_digest
        import shutil
        import subprocess
        previous = self.prepare()
        old_owner = _private_json(previous/'ownership.json')
        backup = self.root/'prior/Lulu.app'
        backup.parent.mkdir()
        shutil.copytree(self.app, backup, symlinks=True)
        empty = self.product/'task-generations'/old_owner['core_cdhash']
        empty.mkdir(parents=True, mode=0o700)
        before = snapshot_digest(previous)
        old_keys = CoreMemoryKeys()
        descriptor = self.app/DESCRIPTOR
        value = json.loads(descriptor.read_text())
        value['capability_closed_set'].append('zz-empty-generation-upgrade')
        descriptor.write_text(json.dumps(value))
        shutil.copyfile('/usr/bin/false', self.app/'Contents/MacOS/lulu-agent-core')
        subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(self.app)],
                       check=True, capture_output=True)
        def capture(state, owner):
            return capture_history(state, owner, product_root=self.product,
                                   keys=self.keys, backup_app=backup,
                                   key_reader=lambda *_: old_keys)
        current = prepare_install_state(self.app, product_root=self.product, keys=self.keys,
                                        previous_state=previous, handover_loader=capture)
        owner = _private_json(current/'ownership.json')
        self.assertNotEqual(current, previous)
        self.assertNotEqual(owner['core_cdhash'], old_owner['core_cdhash'])
        self.assertEqual(open_handover(_private_json(current/MATERIALS_DIR/FILENAME),
                                      keys=self.keys, owner=owner, product_root=self.product), [])
        self.assertEqual(snapshot_digest(previous), before)
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(old_keys.writes, [])
