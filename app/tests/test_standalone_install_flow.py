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

    def test_verified_empty_history_does_not_write_keys_or_ledger(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        from src.core.agent_entry.product_transaction import snapshot_digest
        before, writes = snapshot_digest(self.root), list(self.keys.writes)
        self.assertEqual(verified_terminal_rows(self.root, 'product-installation', self.keys), [])
        self.assertEqual(snapshot_digest(self.root), before)
        self.assertEqual(self.keys.writes, writes)

    def test_unfinished_history_blocks_instead_of_silently_dropping_tasks(self):
        self.store.create_job(**dict(_create_arguments(), installation_id='product-installation'))
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'):
            verified_terminal_rows(self.root, 'product-installation', self.keys)

    def test_terminal_history_preserves_revision_and_result_without_writes(self):
        from src.core.job_ledger.contracts import JobState
        self.store.create_job(**dict(_create_arguments(), installation_id='product-installation'))
        self.store.transition('job-001', expected_revision=1, new_state=JobState.QUEUED,
                              changed_at='2026-08-20T01:00:01Z')
        self.store.claim('job-001', expected_revision=2, worker_instance_id='fixture',
                         operation_receipt=None, changed_at='2026-08-20T01:00:02Z')
        result = 'lulu-result:job-001:sha256:'+'a'*64
        self.store.transition('job-001', expected_revision=3, new_state=JobState.SUCCEEDED,
                              changed_at='2026-08-20T01:00:03Z', result_ref=result)
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        writes = list(self.keys.writes)
        row, = verified_terminal_rows(self.root, 'product-installation', self.keys)
        self.assertEqual((row['job_revision'], row['last_sequence'], row['lease_epoch']), (4, 4, 1))
        self.assertEqual(row['result_ref'], result)
        self.assertEqual(writes, self.keys.writes)

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

    def test_old_core_exit_preserves_registration_for_rollback(self):
        from src.core.agent_entry.effects import CurrentUserLaunchctlService
        from subprocess import CompletedProcess
        service = CurrentUserLaunchctlService()
        replies = [CompletedProcess([], 0, '  pid = 123\n'),
                   CompletedProcess([], 0, ''), CompletedProcess([], 0, 'state = waiting\n')]
        with patch.object(service, '_run', side_effect=replies) as run:
            service.terminate_registered_process()
            self.assertEqual([call.args[0][0] for call in run.call_args_list], ['print', 'kill', 'print'])
            self.assertEqual(run.call_args_list[1].args[0][1], 'SIGTERM')

    def test_service_name_rejects_unrelated_or_malformed_labels(self):
        from src.core.agent_entry.effects import CurrentUserLaunchctlService
        for name in ('com.other.app', 'com.lulu.transcribe.agent-core.app-../foreign'):
            with self.assertRaises(ValueError):
                CurrentUserLaunchctlService(label=name)

    def test_snapshot_edit_and_missing_registry_fail_closed(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        snapshot = self.root/'jobs/job-ledger-v1/snapshot.json'
        snapshot.write_bytes(snapshot.read_bytes().replace(b'"jobs":[]', b'"jobs":[{}]'))
        with self.assertRaises(Exception):
            verified_terminal_rows(self.root, 'product-installation', self.keys)
        self.keys.values.clear()
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
            verified_terminal_rows(self.root, 'product-installation', self.keys)

    def test_never_initialized_empty_generation_does_not_block_upgrade(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        empty = Path(self.tmp.name).resolve()/'not-yet-initialized'
        empty.mkdir(mode=0o700)
        keys = CoreMemoryKeys()
        self.assertEqual(verified_terminal_rows(empty, 'product-installation', keys), [])
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(keys.writes, [])

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

    def test_missing_registry_with_any_existing_file_preserves_it_and_blocks(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        for name in ('snapshot.json', '.DS_Store', 'unrecognized'):
            with self.subTest(name=name):
                root = Path(self.tmp.name).resolve()/('partial-'+name)
                root.mkdir(mode=0o700)
                original = root/name
                original.write_bytes(b'keep original bytes')
                with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
                    verified_terminal_rows(root, 'product-installation', CoreMemoryKeys())
                self.assertEqual(original.read_bytes(), b'keep original bytes')

    def test_uninitialized_generation_symlink_is_not_treated_as_empty(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        target = Path(self.tmp.name).resolve()/'empty-target'
        target.mkdir(mode=0o700)
        link = Path(self.tmp.name).resolve()/'linked-generation'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(Exception):
            verified_terminal_rows(link, 'product-installation', CoreMemoryKeys())
        self.assertTrue(link.is_symlink())

    def test_uninitialized_generation_requires_private_permissions(self):
        from src.core.agent_entry.standalone_history import verified_terminal_rows
        empty = Path(self.tmp.name).resolve()/'non-private-generation'
        empty.mkdir(mode=0o755)
        with self.assertRaises(Exception):
            verified_terminal_rows(empty, 'product-installation', CoreMemoryKeys())
        self.assertEqual(empty.stat().st_mode & 0o777, 0o755)

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

    def test_corrupt_owner_or_registration_is_not_reprepared_over_old_state(self):
        from src.core.agent_entry.standalone_state import verify_install_state
        state = self.prepare()
        original = (state/'ownership.json').read_bytes()
        (state/'core.plist').write_bytes(b'foreign')
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual((state/'ownership.json').read_bytes(), original)

    def test_old_receipts_require_explicit_predecessor_even_without_task_files(self):
        (self.product/'AgentEntry').mkdir()
        (self.product/'AgentEntry/ownership.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_REQUIRED'):
            self.prepare()

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

    def test_path_and_service_policy_agree_for_both_installed_locations(self):
        from src.core.agent_entry.standalone_paths import installed_app, service_for_app
        import shutil
        target = self.home/'Applications/Lulu.app'; target.parent.mkdir()
        shutil.copytree(self.app, target, symlinks=True)
        with patch.object(Path, 'home', return_value=self.home):
            self.assertEqual(installed_app(target), target)
            self.assertTrue(service_for_app(target).startswith('com.lulu.transcribe.agent-core.app-'))
            with self.assertRaisesRegex(RuntimeError, 'INSTALLATION_TARGET_AUTHORITY_REQUIRED'):
                installed_app(self.app)

    def test_drag_payload_has_only_app_and_applications_link(self):
        module = runpy.run_path(str(Path(__file__).parents[1]/'packaging/create_drag_delivery.py'))
        payload = self.root/'drag'
        source = module['prepare_payload'](self.app, payload)
        self.assertEqual({p.name for p in payload.iterdir()}, {'Lulu.app', 'Applications'})
        self.assertEqual(os.readlink(payload/'Applications'), '/Applications')
        from src.core.agent_entry.standalone_source import inspect_install_source
        self.assertEqual(inspect_install_source(payload/'Lulu.app'), source)

    def test_update_zip_copy_preserves_the_same_app_without_resigning(self):
        module = runpy.run_path(str(Path(__file__).parents[1]/'packaging/make_update_release.py'))
        target = self.root/'update/Lulu.app'; target.parent.mkdir()
        with patch.object(module['subprocess'], 'run', wraps=module['subprocess'].run) as run:
            module['prepare_update_app'](self.app, target)
            self.assertFalse(any('--sign' in call.args[0] for call in run.call_args_list))
        from src.core.updater.archive import app_file_manifest
        self.assertEqual(app_file_manifest(self.app), app_file_manifest(target))

    def test_optional_skill_uses_new_receipt_and_does_not_change_app_ready_state(self):
        from src.core.agent_entry.standalone_runtime import StandaloneInstallation
        with patch.object(Path, 'home', return_value=self.home):
            state = self.prepare()
            before = (state/'current.json').read_bytes()
            installation = StandaloneInstallation(self.app, self.keys)
            self.assertEqual(installation.skill_status(), 'not_installed')
            self.assertEqual(installation.install(transaction_id='fixture-skill')['state'], 'ready')
            self.assertEqual(installation.skill_status(), 'ready')
            self.assertEqual(installation.install(transaction_id='fixture-again')['effect_count'], 0)
            self.assertEqual((state/'current.json').read_bytes(), before)
            self.assertIn(str(self.app).encode(), installation.paths.launcher_target.read_bytes())

    def test_service_registration_is_idempotent_and_never_stops_old_service(self):
        from src.core.agent_entry.standalone_runtime import ensure_service
        with patch.object(Path, 'home', return_value=self.home):
            state = self.prepare()
            with patch('src.core.agent_entry.effects.CurrentUserLaunchctlService') as controller:
                controller.return_value.is_loaded.return_value = False
                ensure_service(self.app)
                controller.return_value.start.assert_called_once_with(state/'core.plist')
                controller.return_value.stop.assert_not_called()
                controller.return_value.is_loaded.return_value = True
                ensure_service(self.app)
                self.assertEqual(controller.return_value.start.call_count, 1)
