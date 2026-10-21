"""Signed tiny temporary Apps, fake old keys; no real users, OS secrets or launchctl."""
import json
import subprocess
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_standalone_install_source as fixture
from test_generation9_formal_install_public import MemoryKeys
from test_core_storage_binding import CoreMemoryKeys
from test_phase5_job_ledger import _create_arguments
from src.core.agent_entry.standalone_state import prepare_install_state, install_state_root
from src.core.agent_entry.standalone_source import DESCRIPTOR, prepare_install_descriptor
from src.core.agent_entry.local_task_storage import (prepare_local_app, migrate_legacy_tasks,
    migration_status)
from src.core.local_integrity import product_local_store, TASK_DIRECTORY
from src.core.job_ledger.storage_binding import prepare_core_task_store
from src.core.agent_entry.product_transaction import snapshot_digest
from src.core.job_ledger.contracts import JobState
from src.core.gui_task_view import GuiTaskViewStore


class LegacyKeys(CoreMemoryKeys, MemoryKeys):
    pass


class LocalTaskMigrationTests(unittest.TestCase):
    def setUp(self):
        fixture.StandaloneInstallSourceTests.setUp(self)
        fixture.StandaloneInstallSourceTests.prepare(self)
        self.product = self.root/'home/Library/Application Support/Lulu'
        self.product.mkdir(parents=True, mode=0o700)
        self.legacy_keys = LegacyKeys()
        self.old_state = prepare_install_state(self.app, product_root=self.product, keys=self.legacy_keys)
        self.old_owner = json.loads((self.old_state/'ownership.json').read_text())
        self.generation = self.old_owner['core_cdhash']
        self.old_root = self.product/'task-generations'/self.generation
        self.old_root.parent.mkdir(mode=0o700); self.old_root.mkdir(mode=0o700)
        self.old_keys = CoreMemoryKeys()
        self.old_store = prepare_core_task_store(self.old_root, self.old_owner['installation_id'], self.old_keys, legacy_storage=False)
        self.old_store.create_job(**dict(_create_arguments(), installation_id=self.old_owner['installation_id']))
        self.set_version('4.2.1', '20261013.2')
        # All tests fail immediately if a real credential backend is requested.
        for guard in (
                patch('src.core.license.store.StrictKeyringStore._keyring', side_effect=AssertionError('OS credentials forbidden')),
                patch('src.core.cli_runtime.core_task_keychain.CoreTaskKeyringStore', side_effect=AssertionError('OS credentials forbidden'))):
            guard.start()
            self.addCleanup(guard.stop)

    def set_version(self, version, build):
        path = self.app/'Contents/Resources/build_profile.json'
        profile = json.loads(path.read_text()); profile.update(version=version, build=build)
        path.write_text(json.dumps(profile))
        path = self.app/'Contents/Resources/skills/lulu-agent/manifest.json'
        skill = json.loads(path.read_text()); skill['product_identity'].update(product_version=version, product_build=build)
        path.write_text(json.dumps(skill))
        (self.app/DESCRIPTOR).unlink()
        prepare_install_descriptor(self.app, product_version=version, product_build=build,
            source_revision=self.manifest['source_revision'], capability_closed_set=('doctor', 'transcribe', 'version'))
        subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(self.app)], check=True, capture_output=True)

    def prepare(self):
        return prepare_local_app(self.app, self.product)

    def current_store(self):
        keys = product_local_store(self.product)
        owner = json.loads((install_state_root(self.app, self.product)/'ownership.json').read_text())
        root = self.product/TASK_DIRECTORY
        root.mkdir(mode=0o700, exist_ok=True)
        return prepare_core_task_store(root, owner['installation_id'], keys, legacy_storage=False)

    def migrate(self, reader=None):
        return migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader or (lambda *_: self.old_keys))

    def originals(self):
        return {name: snapshot_digest(self.product/name) for name in ('task-generations', 'AgentEntry/app-generations')}

    def local_successor(self):
        import shutil
        core = self.app/'Contents/MacOS/lulu-agent-core'
        shutil.copyfile('/usr/bin/false', core)
        core.chmod(0o755)
        self.set_version('4.2.1', '20261014.3')
        previous = self.prepare()
        successor = json.loads((previous/'ownership.json').read_text())['core_cdhash']
        shutil.copyfile('/usr/bin/printf', core)
        core.chmod(0o755)
        self.set_version('4.2.1', '20261014.5')
        self.prepare()
        return previous, successor




    def unmapped_history(self, *, forwarded):
        """An old archive has no retained code identity; only its sealed card may transfer."""
        generation = 'a'*40 if self.generation != 'a'*40 else 'b'*40
        root = self.product/'task-generations'/generation
        root.mkdir(mode=0o700)
        keys = CoreMemoryKeys()
        store = prepare_core_task_store(root, self.old_owner['installation_id'], keys, legacy_storage=False)
        from src.core.job_ledger.service import JobControlService
        from test_shared_core_gui_task_ledger import _runtime
        runtime = _runtime(JobControlService(store), ['archived-gui', 'archived-attempt', 'archived-lineage'])
        runtime.gui_jobs_create({'title':'旧版保留文案', 'source':'/isolated/archived.wav', 'kind':'file',
            'folder_id':None, 'stable_key':'archived-source', 'execution_policy':None})
        if forwarded:
            store.transition('archived-gui', expected_revision=1, new_state=JobState.CANCELLED,
                changed_at='2026-10-13T00:00:01Z')
            from src.core.agent_entry.standalone_history import verified_task_snapshot, rows_from_jobs
            from src.core.agent_entry.task_handover_context import capture_context
            from src.core.agent_entry.task_handover import seal_handover, FILENAME
            from src.core.agent_entry.installed_materials import MATERIALS_DIR
            rows = rows_from_jobs(verified_task_snapshot(root, self.old_owner['installation_id'], keys)['jobs'])
            context = capture_context(self.product, self.app, rows, generation=generation)
            value = seal_handover(rows, keys=self.legacy_keys, owner=self.old_owner,
                previous_manifest=None, product_root=self.product, gui_context=context)
            (self.old_state/MATERIALS_DIR/FILENAME).write_text(json.dumps(value))
        return root, store






    def advance_old_anchor(self):
        anchor = self.old_store._anchor_store
        head = anchor.load()
        replacement = dict(head, sequence=head['sequence']+1, head_hmac='e'*64)
        anchor.compare_and_set(head, replacement)














    def _check_launch_inheritance(self, *, updating):
        before = self.originals()
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        import shutil
        backup = self.root/'rollback/Lulu.app'
        if updating: shutil.copytree(self.app, backup)
        with patch.object(Path, 'home', return_value=self.root/'home'), \
                patch('src.core.agent_entry.standalone_runtime.frozen_app', return_value=self.app):
            self.assertEqual(prepare_current_app(backup_app=backup if updating else None),
                install_state_root(self.app, self.product))
        reader = unittest.mock.Mock(return_value=self.old_keys)
        self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader, automatic=True), 1)
        self.assertEqual(migration_status(self.product), 'complete')
        self.assertEqual(self.current_store().get_job('job-001'), self.old_store.get_job('job-001'))
        self.assertEqual(self.originals(), before)



























