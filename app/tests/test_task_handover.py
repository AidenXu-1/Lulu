"""Synthetic upgrade projections; no installed CLI, Keychain or user files."""
import base64
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.core.agent_entry.task_handover import capture_terminal_tasks, seal_handover, open_handover


def record():
    return {'job_id': 'job-old', 'job_kind': 'local_transcription', 'job_revision': 4,
            'state': 'succeeded', 'lease_epoch': 1, 'last_sequence': 4,
            'result_ref': 'lulu-result:job-old:sha256:' + 'a' * 64,
            'receipt_ref': 'lulu-job:job-old:revision:4', 'partial_failures': []}


class TaskHandoverTests(unittest.TestCase):
    def setUp(self):
        self.owner = {'installation_id': 'synthetic-install', 'installation_id_digest': 'b' * 64,
                      'component_manifest_sha256': 'c' * 64}
        self.keys = SimpleNamespace(get_text=lambda name: base64.b64encode(b'x' * 32).decode())
        self.root = Path('/synthetic/Lulu')

    def sealed(self):
        return seal_handover([record()], keys=self.keys, owner=self.owner,
                             previous_manifest='d' * 64, product_root=self.root)


    def test_completed_live_recording_without_result_artifact_preserves_authenticated_history(self):
        row = dict(record(), job_kind='live_recording', result_ref=None)
        summary = {k: v for k, v in row.items() if k != 'partial_failures'}
        captured = capture_terminal_tasks(Mock(side_effect=[{'jobs': [summary]}, row]))
        sealed = seal_handover(captured, keys=self.keys, owner=self.owner,
                              previous_manifest='d' * 64, product_root=self.root)
        self.assertEqual(open_handover(sealed, keys=self.keys, owner=self.owner,
                                      product_root=self.root), [row])
        self.assertEqual(row['result_ref'], None)

    def test_live_history_exception_does_not_accept_other_missing_or_changed_results(self):
        from src.core.agent_entry.task_handover import validate_records
        for kind in ('local_transcription', 'gui_transcription', 'local_synthesis',
                     'link_extraction', 'workspace_conversation'):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
                validate_records([dict(record(), job_kind=kind, result_ref=None)])
        with self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
            validate_records([dict(record(), job_kind='live_recording', state='partial', result_ref=None)])
        row = dict(record(), job_kind='live_recording', result_ref=None)
        summary = {k: v for k, v in row.items() if k != 'partial_failures'}
        with self.assertRaisesRegex(RuntimeError, 'TASKS_CHANGED'):
            capture_terminal_tasks(Mock(side_effect=[{'jobs': [summary]}, record()]))
        sealed = seal_handover([row], keys=self.keys, owner=self.owner,
                              previous_manifest='d' * 64, product_root=self.root)
        sealed['body']['jobs'][0]['job_revision'] = 5
        with self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
            open_handover(sealed, keys=self.keys, owner=self.owner, product_root=self.root)

    def test_tamper_old_target_owner_root_and_wrong_key_are_rejected(self):
        for field in ('result', 'target', 'owner', 'root', 'key'):
            with self.subTest(field=field):
                payload, owner, keys, root = self.sealed(), dict(self.owner), self.keys, self.root
                if field == 'result': payload['body']['jobs'][0]['result_ref'] = 'lulu-result:job-old:sha256:' + 'e' * 64
                if field == 'target': owner['component_manifest_sha256'] = 'e' * 64
                if field == 'owner': owner['installation_id'] = 'other-install'
                if field == 'root': root = Path('/other/Lulu')
                if field == 'key': keys = SimpleNamespace(get_text=lambda name: base64.b64encode(b'y' * 32).decode())
                with self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
                    open_handover(payload, keys=keys, owner=owner, product_root=root)


    def test_runtime_history_is_visible_but_cannot_cancel_or_synthesize_old_events(self):
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import LocalTranscriptionJobApplication
        application = Mock(spec=LocalTranscriptionJobApplication)
        runtime = LocalTranscriptionCliRuntime(application, historical_jobs=[record()])
        control = Mock(); control.list.return_value = ()
        runtime._authorized_job_control = Mock(return_value=control)
        self.assertEqual(runtime.jobs_list()['jobs'][0]['job_id'], 'job-old')
        self.assertEqual(runtime.jobs_receipt('job-old'), record())
        self.assertEqual(runtime.gui_jobs_list()['jobs'][0]['created_by'], 'upgrade-history')
        with self.assertRaisesRegex(RuntimeError, 'CAPABILITY_UNAVAILABLE') as cancelled:
            runtime.jobs_cancel('job-old')
        self.assertEqual(cancelled.exception.reason_code, 'HISTORY_READ_ONLY')
        with self.assertRaisesRegex(RuntimeError, 'CAPABILITY_UNAVAILABLE') as waited:
            runtime.jobs_wait('job-old', 0)
        self.assertEqual(waited.exception.reason_code, 'HISTORY_EVENTS_NOT_IMPORTED')
        control.cancel.assert_not_called()
        control.wait.assert_not_called()


    def test_namespaced_store_never_reads_old_task_service_or_accepts_another_generation(self):
        from unittest.mock import patch
        from src.core.cli_runtime.core_task_keychain import CoreTaskKeyringStore
        from src.core.license.store import SecureStoreError
        native = Mock(); native.cdhash = 'a' * 40
        store = CoreTaskKeyringStore(); store.core_generation = 'a' * 40
        with patch('src.core.cli_runtime.core_task_keychain.current_core', return_value=Path('/Applications/Lulu.app/Contents/MacOS/lulu-agent-core')), \
             patch('src.core.cli_runtime.core_task_keychain._assert_core_account'), \
             patch.object(store, '_keyring', return_value=object()), \
             patch('src.core.cli_runtime.core_task_keychain.NativeCoreTaskStore', return_value=native):
            store.get_text('lulu_core_job_registry_v1')
            self.assertEqual(native.service, 'Lulu.CoreTasks.' + 'a' * 40)
            native.read.reset_mock()
            store.core_generation = 'b' * 40
            with self.assertRaisesRegex(SecureStoreError, 'IDENTITY_REQUIRED'):
                store.get_text('lulu_core_job_registry_v1')
            native.read.assert_not_called()


class TaskHandoverInstallationTests(unittest.TestCase):
    def test_damaged_app_cannot_skip_verification_for_a_different_generation(self):
        from tempfile import TemporaryDirectory
        from src.core.agent_entry.product_install import ProductInstaller, ProductPaths
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            installer = ProductInstaller(ProductPaths(root/'app', root/'state', root/'agent', root/'skill', root/'cli'),
                key_store=Mock(), service=Mock(), stop_clients=Mock(), clients_stopped=Mock(), task_exporter=Mock())
            installer.verify_installed = Mock(side_effect=RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH'))
            installer._previous = Mock()
            with self.assertRaisesRegex(RuntimeError, 'COMPONENT_MISMATCH'):
                installer._prepare_task_handover(root/'materials',
                    {'component_manifest_sha256': 'a'*64}, {'component_manifest_sha256': 'b'*64})
            installer.task_exporter.assert_not_called()
            installer._previous.assert_not_called()
            self.assertFalse((root/'materials').exists())

    def test_quiescent_failure_changes_no_installed_object_and_recovers_old_service(self):
        from tempfile import TemporaryDirectory
        from test_product_transaction_v2_public import ProductTransactionTests
        from src.core.agent_entry.product_transaction import ProductTransaction
        fixture = ProductTransactionTests()
        with TemporaryDirectory() as temporary:
            state, targets, sources, service = fixture.fixture(Path(temporary).resolve(), True)
            tx = ProductTransaction(state, targets, service=service)
            def changed():
                self.assertFalse(service.loaded)
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            with self.assertRaisesRegex(RuntimeError, 'TASKS_CHANGED'):
                fixture.execute(tx, sources, validate_quiescent=changed)
            for name, path in targets.items():
                self.assertEqual(path.read_text(), 'old:' + name)
            import json
            journal = json.loads((tx._work('transaction-0001') / 'journal.json').read_text())
            self.assertEqual(journal['effect_count'], 0)
            tx.recover('transaction-0001', stop_clients=lambda: None, clients_stopped=lambda: True,
                       rollback_verifier=lambda: True, final_verifier=lambda: True)
            self.assertTrue(service.loaded)


if __name__ == '__main__':
    unittest.main()
