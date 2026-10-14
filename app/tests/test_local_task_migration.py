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

    def test_authenticated_local_successor_remains_an_allowed_historical_reader(self):
        previous, successor = self.local_successor()
        before = self.originals()
        with patch('src.core.agent_entry.standalone_history.historical_keys', return_value=self.old_keys) as reader:
            self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys), 1)
        self.assertIn(successor, reader.call_args.kwargs['known_cores'].values())
        self.assertNotEqual(successor, self.generation)
        self.assertEqual(reader.call_count, 1)  # A reader identity is not a legacy ledger source.
        self.assertEqual(self.originals(), before)
        self.assertEqual([j.job_id for j in self.current_store().list_jobs()], ['job-001'])

    def test_modified_local_successor_cannot_expand_historical_reader_identities(self):
        previous, _ = self.local_successor()
        path = previous/'ownership.json'
        value = json.loads(path.read_text()); value['core_cdhash'] = 'f'*40
        path.write_text(json.dumps(value))
        before = self.originals()
        with patch('src.core.agent_entry.standalone_history.historical_keys') as reader:
            with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID'):
                migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys)
        reader.assert_not_called()
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(self.originals(), before)

    def test_modified_local_successor_manifest_cannot_expand_reader_identities(self):
        previous, _ = self.local_successor()
        from src.core.agent_entry.delivery_identity import MANIFEST_NAME
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        path = previous/MATERIALS_DIR/MANIFEST_NAME
        path.write_bytes(path.read_bytes()+b' ')
        with patch('src.core.agent_entry.standalone_history.historical_keys') as reader:
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
                migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys)
        reader.assert_not_called()
        self.assertEqual(self.current_store().list_jobs(), ())

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

    def test_unmapped_archive_does_not_block_authenticated_history_or_known_jobs(self):
        root, archived = self.unmapped_history(forwarded=True)
        self.prepare(); before = self.originals()
        reader = unittest.mock.Mock(return_value=self.old_keys)
        self.assertEqual(self.migrate(reader), 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.originals(), before)
        self.assertEqual(migration_status(self.product), 'partial')
        from src.core.agent_entry.local_task_storage import read_local_history
        rows, context = read_local_history(product_local_store(self.product))
        self.assertEqual([row['job_id'] for row in rows], ['archived-gui'])
        self.assertEqual(rows[0]['state'], 'cancelled')
        self.assertEqual(context['tasks']['archived-gui']['title'], '旧版保留文案')
        self.assertEqual(self.current_store().get_job('job-001'), self.old_store.get_job('job-001'))
        self.assertNotIn('archived-gui', [job.job_id for job in self.current_store().list_jobs()])
        self.assertTrue(root.exists())
        self.assertEqual(self.migrate(reader), 0)
        self.assertEqual(read_local_history(product_local_store(self.product)), (rows, context))
        self.assertEqual(self.originals(), before)

    def test_unforwarded_unmapped_job_stays_in_its_archive_and_is_never_claimed_restored(self):
        root, archived = self.unmapped_history(forwarded=False)
        self.prepare(); before = self.originals()
        self.assertEqual(self.migrate(), 1)
        self.assertEqual(migration_status(self.product), 'partial')
        from src.core.agent_entry.local_task_storage import read_local_history
        self.assertEqual(read_local_history(product_local_store(self.product))[0], [])
        self.assertEqual([j.job_id for j in self.current_store().list_jobs()], ['job-001'])
        self.assertEqual(archived.get_job('archived-gui').state, JobState.PREPARED)
        self.assertEqual(self.originals(), before)

    def test_cancelled_raw_archive_read_keeps_authenticated_handover_and_stops_reading(self):
        from keyring.backends.macOS.api import KeychainDenied
        self.unmapped_history(forwarded=True)
        self.prepare(); before = self.originals()
        # A second known generation must not prompt after cancellation.
        from src.core.agent_entry.task_handover import FILENAME, seal_handover
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        path = self.old_state/MATERIALS_DIR/FILENAME
        value = json.loads(path.read_text())
        extra = 'c'*40 if self.generation != 'c'*40 else 'd'*40
        extra_root = self.product/'task-generations'/extra; extra_root.mkdir(mode=0o700)
        value = seal_handover(value['body']['jobs'], keys=self.legacy_keys, owner=self.old_owner,
            previous_manifest=value['body']['previous_manifest'], product_root=self.product,
            gui_context=value['body']['gui_context'], source_cores={'b'*64: extra})
        path.write_text(json.dumps(value)); before = self.originals()
        class CancelledKeys:
            def get_text(inner, _name): raise KeychainDenied(-128, 'Cancelled')
        reader = unittest.mock.Mock(return_value=CancelledKeys())
        self.assertEqual(self.migrate(reader), 0)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.originals(), before)
        self.assertEqual(migration_status(self.product), 'partial')
        from src.core.agent_entry.local_task_storage import read_local_history, automatic_migration_needed
        records, _ = read_local_history(product_local_store(self.product))
        self.assertEqual([r['job_id'] for r in records], ['archived-gui'])
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertFalse(automatic_migration_needed(self.product))

    def test_cancellation_before_handover_authentication_cannot_claim_partial_success(self):
        from keyring.backends.macOS.api import KeychainDenied
        self.prepare(); before = self.originals()
        with patch.object(self.legacy_keys, 'get_text', side_effect=KeychainDenied(-128, 'Cancelled')):
            with self.assertRaises(RuntimeError): self.migrate()
        self.assertEqual(self.originals(), before)
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.current_store().list_jobs(), ())

    def test_unmapped_archive_cannot_bypass_handover_authentication(self):
        self.unmapped_history(forwarded=True)
        from src.core.agent_entry.task_handover import FILENAME
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        path = self.old_state/MATERIALS_DIR/FILENAME
        value = json.loads(path.read_text()); value['body']['jobs'][0]['job_revision'] += 1
        path.write_text(json.dumps(value))
        self.prepare(); before = self.originals()
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'): self.migrate()
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(self.originals(), before)

    def advance_old_anchor(self):
        anchor = self.old_store._anchor_store
        head = anchor.load()
        replacement = dict(head, sequence=head['sequence']+1, head_hmac='e'*64)
        anchor.compare_and_set(head, replacement)

    def test_outdated_old_archive_cannot_block_independently_authenticated_history(self):
        self.unmapped_history(forwarded=True)
        self.advance_old_anchor()
        self.prepare(); before = self.originals()
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(migration_status(self.product), 'partial')
        from src.core.agent_entry.local_task_storage import read_local_history
        rows, context = read_local_history(product_local_store(self.product))
        self.assertEqual([r['job_id'] for r in rows], ['archived-gui'])
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(self.originals(), before)

    def test_outdated_archive_without_authenticated_handover_is_not_claimed_imported(self):
        self.advance_old_anchor()
        self.prepare(); before = self.originals()
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(migration_status(self.product), 'partial')
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(self.originals(), before)

    def test_outdated_archive_kept_while_another_authenticated_full_ledger_imports(self):
        self.unmapped_history(forwarded=True)
        other_generation = 'd'*40 if self.generation != 'd'*40 else 'e'*40
        other_root = self.product/'task-generations'/other_generation
        other_root.mkdir(mode=0o700)
        other_keys = CoreMemoryKeys()
        other = prepare_core_task_store(other_root, self.old_owner['installation_id'], other_keys, legacy_storage=False)
        other.create_job(**dict(_create_arguments(), job_id='valid-old-job', attempt_id='valid-old-a',
            idempotency_key='valid-old-i', installation_id=self.old_owner['installation_id']))
        from src.core.agent_entry.task_handover import seal_handover, FILENAME
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        path = self.old_state/MATERIALS_DIR/FILENAME
        body = json.loads(path.read_text())['body']
        path.write_text(json.dumps(seal_handover(body['jobs'], keys=self.legacy_keys, owner=self.old_owner,
            previous_manifest=body['previous_manifest'], product_root=self.product,
            gui_context=body['gui_context'], source_cores={'e'*64:other_generation})))
        self.advance_old_anchor(); self.prepare(); before = self.originals()
        self.assertEqual(self.migrate(lambda keys,core,generation:other_keys if generation==other_generation else self.old_keys), 1)
        self.assertEqual(self.current_store().get_job('valid-old-job'), other.get_job('valid-old-job'))
        self.assertNotIn('job-001',[j.job_id for j in self.current_store().list_jobs()])
        self.assertEqual(migration_status(self.product), 'partial')
        self.assertEqual(self.originals(), before)

    def test_current_store_highwater_mismatch_is_still_fatal_before_any_old_import(self):
        self.prepare(); current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='new-before-inherit',
            attempt_id='new-before-a', idempotency_key='new-before-i'))
        anchor = current._anchor_store; head = anchor.load()
        anchor.compare_and_set(head, dict(head, sequence=head['sequence']+1, head_hmac='f'*64))
        before = current.journal_path.read_bytes()
        from src.core.job_ledger.contracts import JobLedgerError
        with self.assertRaises(JobLedgerError) as failure: self.migrate()
        self.assertEqual(failure.exception.reason_code, 'JOB_LEDGER_HIGH_WATER_ROLLBACK')
        self.assertEqual(current.journal_path.read_bytes(), before)
        self.assertEqual(migration_status(self.product), 'failed')

    def test_unmapped_link_cannot_be_treated_as_an_opaque_safe_archive(self):
        path = self.product/'task-generations'/('c'*40)
        path.symlink_to(self.generation, target_is_directory=True)
        self.prepare(); before = self.originals()
        from src.core.job_ledger.contracts import JobLedgerError
        with self.assertRaises(JobLedgerError) as failure: self.migrate()
        self.assertEqual(failure.exception.reason_code, 'JOB_LEDGER_SYMLINK_FORBIDDEN')
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(self.originals(), before)

    def test_partial_history_appears_in_formal_gui_list_after_reopen_and_new_job_still_works(self):
        self.unmapped_history(forwarded=True)
        self.prepare(); self.migrate()
        before = self.originals()
        self.prepare()
        current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='new-after-restore',
            attempt_id='new-after-restore-a', idempotency_key='new-after-restore-i'))
        import sys
        with patch.object(Path, 'home', return_value=self.root/'home'), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(self.app/'Contents/MacOS/lulu-agent-core')), \
                patch('src.core.agent_entry.standalone_paths.installed_app', side_effect=lambda app: app), \
                patch('src.core.cli_runtime.core_task_native.measured_core', return_value=self.generation):
            from src.core.cli_runtime.factory import build_measured_core_runtime
            identity = {'adapter_id':'agent_cli', 'shared':{'profile':'external_candidate',
                'product_version':'4.2.1', 'product_build':'20261013.2',
                'source_commit':self.manifest['source_revision'], 'bundle_id':'com.lulu.transcribe'}}
            runtime = build_measured_core_runtime(product_identity=identity, resource_root=self.app/'Contents/Resources')
            row = next(row for row in runtime.gui_jobs_list()['jobs'] if row['job_id'] == 'archived-gui')
            self.assertEqual(row['gui_task']['title'], '旧版保留文案')
            self.assertEqual(row['created_by'], 'upgrade-history')
        self.assertEqual(migration_status(self.product), 'partial')
        self.assertIn('new-after-restore', [j.job_id for j in self.current_store().list_jobs()])
        self.assertEqual(self.originals(), before)

    def test_normal_prepare_new_task_and_next_version_never_request_system_secrets(self):
        before = self.originals()
        state = self.prepare()
        self.assertEqual(state.parent.name, 'local-app-generations')
        self.assertEqual(migration_status(self.product), 'pending')
        store = self.current_store()
        store.create_job(**dict(_create_arguments(), job_id='new-job', attempt_id='new-attempt', idempotency_key='new-idem'))
        expected = store.list_jobs()
        self.set_version('4.2.2', '20261013.3')
        self.assertNotEqual(self.prepare(), state)
        self.assertEqual(self.current_store().list_jobs(), expected)
        self.assertEqual(self.originals(), before)

    def test_prepared_and_completed_records_events_results_views_and_originals_preserved(self):
        store = self.old_store
        store.create_job(**dict(_create_arguments(), job_id='done', attempt_id='done-a', idempotency_key='done-i',
            installation_id=self.old_owner['installation_id']))
        store.transition('done', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        store.claim('done', expected_revision=2, worker_instance_id='fake-worker', operation_receipt=None, changed_at='2026-10-13T00:00:02Z')
        store.transition('done', expected_revision=3, new_state=JobState.SUCCEEDED, changed_at='2026-10-13T00:00:03Z',
            result_ref='lulu-result:done:sha256:'+'a'*64)
        view = GuiTaskViewStore(self.old_root/'jobs')
        view.save_view(groups=[{'id':1, 'name':'旧分组', 'folder_id':None, 'expanded':False,
            'members':[{'task_id':'job-001'}, {'task_id':'done'}]}], hidden_ids=['done'], expected_revision=0,
            known_ids={'job-001','done'})
        marker = self.product/'protected-recording'; marker.write_bytes(b'original media')
        before, old_keys = self.originals(), deepcopy(self.old_keys.values)
        self.prepare()
        current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='new', attempt_id='new-a', idempotency_key='new-i'))
        GuiTaskViewStore(current.data_root).save_view(groups=[{'id':1, 'name':'新分组', 'folder_id':None,
            'expanded':True, 'members':[{'task_id':'new'}]}], hidden_ids=[], expected_revision=0, known_ids={'new'})
        self.assertEqual(self.migrate(), 2)
        current = self.current_store()
        for job in store.list_jobs():
            self.assertEqual(current.get_job(job.job_id), job)
            self.assertEqual(current.events(job.job_id), store.events(job.job_id))
        view = GuiTaskViewStore(current.data_root).load()
        self.assertEqual([g['name'] for g in view['groups']], ['新分组', '旧分组'])
        self.assertEqual([g['id'] for g in view['groups']], [1,2])
        self.assertEqual(view['hidden_ids'], ['done'])
        self.assertEqual(self.originals(), before)
        self.assertEqual(self.old_keys.values, old_keys)
        self.assertEqual(marker.read_bytes(), b'original media')
        current.transition('job-001', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:04Z')
        updated = current.get_job('job-001')
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(self.current_store().get_job('job-001'), updated)

    def test_authorization_denial_keeps_old_files_and_new_task_usable(self):
        self.prepare(); before = self.originals()
        current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='new', attempt_id='new-a', idempotency_key='new-i'))
        with self.assertRaisesRegex(RuntimeError, 'denied'):
            self.migrate(lambda *_: (_ for _ in ()).throw(RuntimeError('denied')))
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.originals(), before)
        self.assertEqual([j.job_id for j in self.current_store().list_jobs()], ['new'])
        self.prepare()

    def test_authorization_wait_does_not_block_new_task_save(self):
        self.prepare(); before = self.originals()
        from src.core.updater.handover import handover_lock
        def during_authorization(*_):
            with handover_lock(self.product/'updates'):
                self.current_store().create_job(**dict(_create_arguments(), job_id='during-auth',
                    attempt_id='during-auth-a', idempotency_key='during-auth-i'))
            return self.old_keys
        self.migrate(during_authorization)
        self.assertEqual({j.job_id for j in self.current_store().list_jobs()}, {'job-001', 'during-auth'})
        self.assertEqual(self.originals(), before)

    def test_automatic_upgrade_runs_once_and_next_overwrite_reuses_data_without_old_keys(self):
        self.prepare(); before = self.originals()
        reader = unittest.mock.Mock(return_value=self.old_keys)
        self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader, automatic=True), 1)
        self.set_version('4.2.1', '20261014.3'); self.prepare()
        expected = self.current_store().list_jobs()
        self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader, automatic=True), 0)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.current_store().list_jobs(), expected)
        self.assertEqual(self.originals(), before)

    def test_denied_automatic_upgrade_is_not_retried_on_reopen(self):
        self.prepare(); before = self.originals()
        reader = unittest.mock.Mock(side_effect=RuntimeError('denied'))
        with self.assertRaisesRegex(RuntimeError, 'denied'):
            migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
                key_reader=reader, automatic=True)
        self.prepare()
        migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader, automatic=True)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.originals(), before)

    def test_clean_install_has_no_legacy_operation_or_system_backend(self):
        product = self.root/'clean/Library/Application Support/Lulu'
        prepare_local_app(self.app, product)
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        self.assertEqual(migration_status(product), 'none')
        self.assertFalse(automatic_migration_needed(product))
        self.assertEqual(migrate_legacy_tasks(self.app, product, automatic=True), 0)

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

    def test_drag_launch_preparation_then_automatic_inheritance(self):
        self._check_launch_inheritance(updating=False)

    def test_update_launch_preparation_then_automatic_inheritance(self):
        self._check_launch_inheritance(updating=True)

    def test_interrupted_authorization_is_visible_and_never_repeats_on_startup(self):
        self.prepare(); before = self.originals()
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        with self.assertRaises(KeyboardInterrupt):
            migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
                key_reader=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()), automatic=True)
        self.prepare()
        self.assertFalse(automatic_migration_needed(self.product))
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.originals(), before)

    def test_new_running_job_finishes_before_commit_without_second_authorization(self):
        self.prepare(); before = self.originals()
        from src.core.updater.handover import handover_lock
        writers = []
        def authorize(*_):
            current = self.current_store()
            writers.append(current)
            current.create_job(**dict(_create_arguments(), job_id='during-auth',
                attempt_id='during-auth-a', idempotency_key='during-auth-i'))
            current.transition('during-auth', expected_revision=1, new_state=JobState.QUEUED,
                changed_at='2026-10-14T00:00:01Z')
            current.claim('during-auth', expected_revision=2, worker_instance_id='active-new-worker',
                operation_receipt=None, changed_at='2026-10-14T00:00:02Z')
            return self.old_keys
        reader = unittest.mock.Mock(side_effect=authorize)
        def finish_task(_):
            # The idle wait must release the product barrier, so the active job
            # can finish its ordinary business write before inheritance commits.
            with handover_lock(self.product/'updates'):
                writers[0].transition('during-auth', expected_revision=3,
                    new_state=JobState.SUCCEEDED, changed_at='2026-10-14T00:00:03Z',
                    result_ref='lulu-result:during-auth:sha256:'+'b'*64)
        with patch('src.core.agent_entry.local_task_storage._idle_sleep', side_effect=finish_task) as wait:
            self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
                key_reader=reader, automatic=True, wait_for_idle=True), 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(self.current_store().get_job('during-auth').state, JobState.SUCCEEDED)
        self.assertEqual(self.originals(), before)

    def test_old_source_change_during_authorization_is_rejected_before_import(self):
        self.prepare()
        def reader(*_):
            self.old_store.create_job(**dict(_create_arguments(), job_id='old-concurrent',
                attempt_id='old-concurrent-a', idempotency_key='old-concurrent-i'))
            return self.old_keys
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASKS_CHANGED'):
            self.migrate(reader)
        self.assertEqual(self.current_store().list_jobs(), ())
        self.assertEqual(migration_status(self.product), 'failed')

    def test_deliberate_old_version_rollback_inherits_only_new_old_records(self):
        self.prepare()
        def automatic():
            return migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
                key_reader=lambda *_: self.old_keys, automatic=True)
        self.assertEqual(automatic(), 1)
        current = self.current_store()
        current.transition('job-001', expected_revision=1, new_state=JobState.QUEUED,
            changed_at='2026-10-14T00:00:01Z')
        changed = current.get_job('job-001')
        self.old_store.create_job(**dict(_create_arguments(), job_id='created-after-rollback',
            attempt_id='rollback-a', idempotency_key='rollback-i'))
        before = self.originals()
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        self.assertTrue(automatic_migration_needed(self.product))
        self.assertEqual(automatic(), 1)
        self.assertFalse(automatic_migration_needed(self.product))
        self.assertEqual(self.current_store().get_job('job-001'), changed)
        self.assertEqual(self.current_store().get_job('created-after-rollback'),
            self.old_store.get_job('created-after-rollback'))
        self.assertEqual(self.originals(), before)

    def test_partial_import_retries_without_duplicate_or_overwrite(self):
        self.old_store.create_job(**dict(_create_arguments(), job_id='second', attempt_id='second-a', idempotency_key='second-i'))
        self.prepare(); before = self.originals()
        from src.core.job_ledger.store import JobLedgerStore
        real = JobLedgerStore.import_verified_job
        def interrupted(store, **arguments):
            if arguments['job']['job_id'] == 'second': raise OSError(28, 'disk full')
            return real(store, **arguments)
        with patch.object(JobLedgerStore, 'import_verified_job', interrupted), self.assertRaises(OSError): self.migrate()
        self.assertEqual([j.job_id for j in self.current_store().list_jobs()], ['job-001'])
        self.assertEqual(self.migrate(), 1)
        self.assertEqual(self.originals(), before)
        self.assertEqual([j.job_id for j in self.current_store().list_jobs()], ['job-001', 'second'])

    def test_corrupt_or_missing_old_key_never_claims_empty_success(self):
        self.prepare(); before = self.originals()
        self.old_keys.values.clear()
        with self.assertRaises(Exception): self.migrate()
        self.assertEqual(migration_status(self.product), 'failed')
        self.assertEqual(self.originals(), before)
        self.assertEqual(self.current_store().list_jobs(), ())

    def test_conflicting_old_id_preserves_current_record_and_continues_other_jobs(self):
        self.old_store.create_job(**dict(_create_arguments(), job_id='other-old',
            attempt_id='other-old-a', idempotency_key='other-old-i'))
        self.prepare(); current = self.current_store()
        current.create_job(**dict(_create_arguments(), frozen_policy={'mode':'new-record'}))
        existing = current.get_job('job-001')
        before = self.originals()
        events = current.events('job-001')
        self.assertEqual(self.migrate(), 1)
        current = self.current_store()
        self.assertEqual(current.get_job('job-001'), existing)
        self.assertEqual(current.events('job-001'), events)
        self.assertEqual(current.get_job('other-old'), self.old_store.get_job('other-old'))
        self.assertEqual(self.originals(), before)
        self.assertEqual(migration_status(self.product), 'partial')
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(self.current_store().get_job('job-001'), existing)
        self.assertEqual(self.originals(), before)

    def test_unique_old_id_with_conflicting_idempotency_still_fails_without_overwrite(self):
        self.prepare(); current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='different-current',
            attempt_id='different-current-a', frozen_policy={'mode':'new-record'}))
        before = current.journal_path.read_bytes(); originals = self.originals()
        from src.core.job_ledger.contracts import JobLedgerError
        with self.assertRaises(JobLedgerError) as caught: self.migrate()
        self.assertEqual(caught.exception.reason_code, 'JOB_IMPORT_CONFLICT')
        self.assertEqual(self.current_store().journal_path.read_bytes(), before)
        self.assertEqual(self.originals(), originals)
        self.assertEqual(migration_status(self.product), 'failed')

    def test_unchanged_old_content_after_file_restore_does_not_authorize_again(self):
        import os
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        self.prepare()
        self.assertEqual(migrate_legacy_tasks(self.app, self.product,
            legacy_keys=self.legacy_keys, key_reader=lambda *_: self.old_keys, automatic=True), 1)
        before = self.originals()
        path = self.old_store.journal_path
        restored = path.with_name('restore.tmp')
        restored.write_bytes(path.read_bytes()); restored.chmod(path.stat().st_mode & 0o777)
        os.replace(restored, path)
        os.utime(path, (1000000000, 1000000000))
        self.assertEqual(self.originals(), before)
        self.assertFalse(automatic_migration_needed(self.product))
        reader = unittest.mock.Mock(side_effect=AssertionError('authorization must not repeat'))
        self.assertEqual(migrate_legacy_tasks(self.app, self.product,
            legacy_keys=self.legacy_keys, key_reader=reader, automatic=True), 0)
        reader.assert_not_called()

    def test_current_result_vault_does_not_trigger_legacy_inheritance_on_reopen(self):
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        self.prepare(); self.migrate()
        results = self.product/'jobs/results'; results.mkdir(parents=True, mode=0o700)
        (results/'new-current-result.json').write_text('{"schema_id":"lulu.transcription-result/v1"}')
        self.prepare()
        self.assertFalse(automatic_migration_needed(self.product))
        reader = unittest.mock.Mock(side_effect=AssertionError('ordinary results must not request old credentials'))
        self.assertEqual(migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
            key_reader=reader, automatic=True), 0)
        reader.assert_not_called()

    def test_fresh_product_with_only_current_results_has_no_legacy_tasks(self):
        from src.core.agent_entry.local_task_storage import automatic_migration_needed, legacy_present
        product = self.root/'new/Library/Application Support/Lulu'
        results = product/'jobs/results'; results.mkdir(parents=True, mode=0o700)
        (results/'new.json').write_text('{}')
        prepare_local_app(self.app, product)
        self.assertFalse(legacy_present(product))
        self.assertEqual(migration_status(product), 'none')
        self.assertFalse(automatic_migration_needed(product))

    def test_old_private_marker_upgrades_locally_without_reauthorizing(self):
        from src.core.local_integrity import AUTO_MIGRATION_KEY
        from src.core.agent_entry.local_task_storage import automatic_migration_needed
        self.prepare(); self.migrate()
        keys = product_local_store(self.product)
        keys.set_text(AUTO_MIGRATION_KEY, json.dumps({'sources':'a'*64}))
        before = self.originals()
        self.prepare()
        self.assertEqual(json.loads(keys.get_text(AUTO_MIGRATION_KEY))['kind'], 'legacy-task-sources-v2')
        self.assertFalse(automatic_migration_needed(self.product))
        self.assertEqual(migration_status(self.product), 'complete')
        self.assertEqual(self.originals(), before)

    def test_current_running_task_and_concurrent_request_block_import(self):
        self.prepare(); current = self.current_store()
        current.create_job(**dict(_create_arguments(), job_id='new', attempt_id='new-a', idempotency_key='new-i'))
        current.transition('new', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        current.claim('new', expected_revision=2, worker_instance_id='fake', operation_receipt=None, changed_at='2026-10-13T00:00:02Z')
        before = current.journal_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'): self.migrate()
        self.assertEqual(current.journal_path.read_bytes(), before)
        from src.core.updater.handover import handover_lock
        with handover_lock(self.product/'updates'), self.assertRaises(Exception): self.migrate()

    def test_missing_local_owner_material_is_not_regenerated_on_next_version(self):
        self.prepare()
        keys = product_local_store(self.product)
        from src.core.agent_entry.ownership import _key_name
        keys.delete_text(_key_name(self.old_owner['installation_id_digest']))
        self.set_version('4.2.2', '20261013.3')
        with self.assertRaisesRegex(RuntimeError, 'LOCAL_INTEGRITY_RECOVERY_REQUIRED'): self.prepare()

    def test_default_core_installer_and_runtime_use_local_store_and_show_migrated_gui_task(self):
        self.prepare()
        from src.core.job_ledger.service import JobControlService
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from test_shared_core_gui_task_ledger import _runtime
        legacy_runtime = _runtime(JobControlService(self.old_store), ['gui-job', 'gui-attempt', 'gui-lineage'])
        legacy_runtime.gui_jobs_create({'title':'保留输入', 'source':'/isolated/source.wav', 'kind':'file',
            'folder_id':None, 'stable_key':'fixture-source', 'execution_policy':None})
        self.old_store.transition('gui-job', expected_revision=1, new_state=JobState.CANCELLED, changed_at='2026-10-13T00:00:01Z')
        self.migrate()
        import sys
        with patch.object(Path, 'home', return_value=self.root/'home'), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(self.app/'Contents/MacOS/lulu-agent-core')), \
                patch('src.core.agent_entry.standalone_paths.installed_app', side_effect=lambda app: app), \
                patch('src.core.cli_runtime.core_task_native.measured_core', return_value=self.generation):
            from src.core.agent_entry.product_install import build_default_product_installer
            installer = build_default_product_installer()
            self.assertEqual(installer.keys.storage_scheme, 'local-integrity-v1')
            installer.verify_installed()
            from src.core.agent_entry.standalone_runtime import prepare_current_app
            self.assertEqual(prepare_current_app(), install_state_root(self.app, self.product))
            from src.core.cli_runtime.factory import build_measured_core_runtime
            identity = {'adapter_id':'agent_cli', 'shared':{'profile':'external_candidate',
                'product_version':'4.2.1', 'product_build':'20261013.2',
                'source_commit':self.manifest['source_revision'], 'bundle_id':'com.lulu.transcribe'}}
            runtime = build_measured_core_runtime(product_identity=identity, resource_root=self.app/'Contents/Resources')
            rows = runtime.gui_jobs_list()['jobs']
            self.assertTrue(any(row['gui_task'].get('title') == '保留输入' for row in rows))
            self.assertEqual(runtime._application._job_store.data_root, self.product/TASK_DIRECTORY/'jobs')
            runtime._job_control.retry('gui-job', expected_revision=2, new_attempt_id='retried-attempt',
                changed_at='2026-10-13T00:00:02Z')
            runtime = build_measured_core_runtime(product_identity=identity, resource_root=self.app/'Contents/Resources')
            self.assertTrue(any(row['job_id'] == 'gui-job' for row in runtime.gui_jobs_list()['jobs']))

    def test_running_legacy_job_is_fenced_without_replay_and_live_folder_is_retained(self):
        live_root = self.product/('recording-'+'a'*32)
        live_root.mkdir(); media = live_root/'segment.ts'; media.write_bytes(b'original recording')
        self.old_store.create_job(**dict(_create_arguments(), job_id='live', attempt_id='live-a', idempotency_key='live-i',
            job_kind='live_recording', created_by='gui', frozen_policy={'recording_id':'recording-'+'a'*32,
                'host_id':'host-'+'b'*32, 'directory':str(live_root)}))
        self.old_store.transition('live', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        self.old_store.claim('live', expected_revision=2, worker_instance_id='dead-worker', operation_receipt=None, changed_at='2026-10-13T00:00:02Z')
        self.prepare(); before = self.originals()
        self.migrate()
        migrated = self.current_store().get_job('live')
        self.assertEqual(migrated.state, JobState.RECOVERY_REQUIRED)
        self.assertEqual(migrated.frozen_policy['directory'], str(live_root))
        self.assertEqual(self.old_store.get_job('live').state, JobState.RUNNING)
        self.assertEqual(self.originals(), before)
        self.assertEqual(media.read_bytes(), b'original recording')

    def test_old_snapshot_corruption_and_symlink_do_not_touch_originals(self):
        self.prepare()
        path = self.old_store.snapshot_path
        original = path.read_bytes(); path.write_bytes(b'{}\n')
        before = self.originals()
        with self.assertRaises(Exception): self.migrate()
        self.assertEqual(self.originals(), before)
        path.write_bytes(original)
        outside = self.product/'outside-snapshot'; path.rename(outside); path.symlink_to(outside)
        with self.assertRaises(Exception): self.migrate()
        self.assertEqual(outside.read_bytes(), original)

    def test_established_local_directory_and_id_missing_require_recovery(self):
        self.prepare()
        keys = product_local_store(self.product)
        from src.core.local_integrity import ID_KEY
        keys.delete_text(ID_KEY)
        with self.assertRaisesRegex(RuntimeError, 'LOCAL_INTEGRITY_RECOVERY_REQUIRED'): self.prepare()

    def test_first_preparation_interruption_retains_identity_for_retry(self):
        from src.core.agent_entry import standalone_state as module
        with patch.object(module, 'verify_install_state', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'): self.prepare()
        from src.core.local_integrity import ID_KEY
        identity = product_local_store(self.product).get_text(ID_KEY)
        self.prepare()
        self.assertEqual(product_local_store(self.product).get_text(ID_KEY), identity)

    def test_partial_commit_reconnects_before_business_barrier_is_released(self):
        self.prepare()
        from src.core.updater.handover import handover_lock
        from src.core.job_ledger.store import JobLedgerStore
        real = JobLedgerStore.import_verified_job
        callbacks = []
        def import_then_stop(store, **arguments):
            real(store, **arguments)
            raise OSError(28, 'disk full after committed item')
        def reconnect():
            with self.assertRaises(Exception):
                with handover_lock(self.product/'updates'): pass
            callbacks.append('under barrier')
        with patch.object(JobLedgerStore, 'import_verified_job', import_then_stop), self.assertRaises(OSError):
            migrate_legacy_tasks(self.app, self.product, legacy_keys=self.legacy_keys,
                key_reader=lambda *_: self.old_keys, on_storage_changed=reconnect)
        self.assertEqual(callbacks, ['under barrier'])
        self.assertEqual(self.current_store().get_job('job-001'), self.old_store.get_job('job-001'))

    def test_view_retry_preserves_skipped_members_and_empty_groups_once(self):
        from src.core.agent_entry.local_task_storage import merge_views
        old = {'groups':[{'id':1,'name':'原批次','folder_id':None,'expanded':True,
            'members':[{'task_id':'old'}, {'title':'跳过','stable_key':'skip','skipped_reason':'existing'}]},
            {'id':2,'name':'空分组','folder_id':None,'expanded':False,'members':[]}], 'hidden_ids':['old']}
        first = merge_views({'groups':[], 'hidden_ids':[]}, old, {'old'})
        self.assertEqual(first, merge_views(first, old, {'old'}))

    def test_live_legacy_gui_worker_prevents_import_without_cancelling_it(self):
        import os
        self.old_store.transition('job-001', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        self.old_store.claim('job-001', expected_revision=2, worker_instance_id='still-alive', operation_receipt=None, changed_at='2026-10-13T00:00:02Z')
        GuiTaskViewStore(self.old_root/'jobs').set_owner('job-001', os.getpid())
        self.prepare(); before = self.originals()
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'): self.migrate()
        self.assertEqual(self.originals(), before)

    def test_local_upgrade_checkpoint_uses_stable_storage_without_system_backend(self):
        self.prepare(); self.current_store()
        from src.core.agent_entry.history_checkpoint import prepare_checkpoint, read_checkpoint
        keys = product_local_store(self.product)
        owner = json.loads((install_state_root(self.app, self.product)/'ownership.json').read_text())
        with patch.object(Path, 'home', return_value=self.root/'home'), \
                patch('src.core.agent_entry.standalone_paths.installed_app', side_effect=lambda app: app):
            checkpoint = prepare_checkpoint(self.app, keys=keys, run_cli=lambda _: {'jobs':[]})
        self.assertEqual(json.loads(checkpoint.read_text())['body']['snapshot'], snapshot_digest(self.product/TASK_DIRECTORY))
        self.assertEqual(read_checkpoint(product_root=self.product, owner=owner,
            generation=owner['core_cdhash'], keys=keys, state=install_state_root(self.app, self.product))[0], [])

    def test_original_operation_receipt_and_retry_lineage_are_imported_intact(self):
        from dataclasses import replace
        from test_phase5_job_ledger import _operation_receipt
        receipt = replace(_operation_receipt('original-receipt'), installation_id=self.old_owner['installation_id'])
        self.old_store.transition('job-001', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        with patch('src.core.link_transcription_application._validate_job_approval_claim', return_value=True):
            self.old_store.claim('job-001', expected_revision=2, worker_instance_id='fake-worker', operation_receipt=receipt, changed_at='2026-10-13T00:00:02Z')
        self.old_store.transition('job-001', expected_revision=3, new_state=JobState.FAILED, changed_at='2026-10-13T00:00:03Z')
        self.old_store.retry_attempt('job-001', expected_revision=4, new_attempt_id='retried', changed_at='2026-10-13T00:00:04Z')
        self.prepare(); self.migrate()
        current = self.current_store()
        self.assertEqual(current.get_job('job-001'), self.old_store.get_job('job-001'))
        self.assertEqual(current.receipt_inheritance('job-001', attempt_id='retried'),
            self.old_store.receipt_inheritance('job-001', attempt_id='retried'))

    def test_early_shared_ledger_running_owner_blocks_import_without_changing_originals(self):
        import os
        import shutil
        from src.core.job_ledger.store import JobLedgerStore
        self.prepare()
        shared = self.product/'jobs'
        shutil.copytree(self.old_root/'jobs', shared)
        # This test models an earlier shared ledger, without a generation copy.
        shutil.rmtree(self.old_root)
        anchor = self.old_store._anchor_store
        old = JobLedgerStore(shared, anchor)
        old.transition('job-001', expected_revision=1, new_state=JobState.QUEUED, changed_at='2026-10-13T00:00:01Z')
        old.claim('job-001', expected_revision=2, worker_instance_id='fake-worker', operation_receipt=None, changed_at='2026-10-13T00:00:02Z')
        GuiTaskViewStore(shared).set_owner('job-001', os.getpid())
        before = snapshot_digest(shared)
        with patch('src.core.cli_runtime.keychain.KeyringJobIntegrityAnchorStore', return_value=anchor), \
                self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'):
            self.migrate()
        self.assertEqual(snapshot_digest(shared), before)
        self.assertEqual(self.current_store().list_jobs(), ())
        GuiTaskViewStore(shared).set_owner('job-001', None)
        before = snapshot_digest(shared)
        with patch('src.core.cli_runtime.keychain.KeyringJobIntegrityAnchorStore', return_value=anchor):
            self.assertEqual(self.migrate(), 1)
        self.assertEqual(snapshot_digest(shared), before)
        self.assertEqual(self.current_store().get_job('job-001').state, JobState.RECOVERY_REQUIRED)
        self.assertEqual(old.get_job('job-001').state, JobState.RUNNING)
