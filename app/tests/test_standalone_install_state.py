"""Generation state is isolated from older receipts and user documents."""
import json
from pathlib import Path
from unittest.mock import Mock
import unittest

import test_standalone_install_source as source_fixture
from test_generation9_formal_install_public import MemoryKeys


class StandaloneInstallStateTests(unittest.TestCase):
    def setUp(self):
        source_fixture.StandaloneInstallSourceTests.setUp(self)
        source_fixture.StandaloneInstallSourceTests.prepare(self)
        self.keys = MemoryKeys()
        self.product = self.root/'home/Library/Application Support/Lulu'
        self.product.mkdir(parents=True)

    def test_fresh_prepare_is_idempotent_and_preserves_documents(self):
        from src.core.agent_entry.standalone_state import prepare_install_state, verify_install_state
        document = self.product/'document.txt'; document.write_text('keep')
        first = prepare_install_state(self.app, product_root=self.product, keys=self.keys)
        second = prepare_install_state(self.app, product_root=self.product, keys=self.keys)
        self.assertEqual(first, second)
        owner, _ = verify_install_state(self.app, state_root=first, keys=self.keys)
        self.assertEqual(owner['source_kind'], 'signed_app')
        self.assertNotIn('release_dmg_sha256', owner)
        self.assertEqual(document.read_text(), 'keep')

    def test_existing_tasks_without_handover_are_not_reset(self):
        from src.core.agent_entry.standalone_state import prepare_install_state
        jobs = self.product/'task-generations/old/jobs'; jobs.mkdir(parents=True)
        marker = jobs/'keep'; marker.write_text('history')
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_REQUIRED'):
            prepare_install_state(self.app, product_root=self.product, keys=self.keys)
        self.assertEqual(marker.read_text(), 'history')

    def test_new_generation_leaves_old_receipts_usable(self):
        from src.core.agent_entry.standalone_state import prepare_install_state, verify_install_state
        old = prepare_install_state(self.app, product_root=self.product, keys=self.keys)
        before = {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()}
        # Simulate a separate, still signed App generation with changed metadata.
        from src.core.agent_entry.standalone_source import DESCRIPTOR
        descriptor = self.app/DESCRIPTOR
        value = json.loads(descriptor.read_text()); value['capability_closed_set'].append('zz-extra')
        descriptor.write_text(json.dumps(value))
        import subprocess
        subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(self.app)],check=True,capture_output=True)
        loader = Mock(return_value=([], None))
        new = prepare_install_state(self.app, product_root=self.product, keys=self.keys,
                                    previous_state=old, handover_loader=loader)
        self.assertNotEqual(new, old)
        self.assertEqual(before, {p.name:p.read_bytes() for p in old.iterdir() if p.is_file()})
        owner, _ = verify_install_state(self.app, state_root=new, keys=self.keys)
        previous = json.loads((old/'ownership.json').read_text())
        self.assertEqual(owner['installation_id'], previous['installation_id'])
        loader.assert_called_once()

    def test_handover_failure_never_commits_new_materials(self):
        from src.core.agent_entry.standalone_state import prepare_install_state, install_state_root
        old = prepare_install_state(self.app, product_root=self.product, keys=self.keys)
        # A distinct App location gets a distinct generation directory.
        import shutil
        newapp = self.root/'new/Lulu.app'; newapp.parent.mkdir(); shutil.copytree(self.app,newapp,symlinks=True)
        loader = Mock(side_effect=RuntimeError('fixture migration failure'))
        with self.assertRaisesRegex(RuntimeError, 'fixture migration failure'):
            prepare_install_state(newapp, product_root=self.product, keys=self.keys,
                                  previous_state=old, handover_loader=loader)
        self.assertFalse(install_state_root(newapp, self.product).exists())
        self.assertTrue((old/'current.json').is_file())
