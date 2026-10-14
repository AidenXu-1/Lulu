"""Failed preparations in signed temporary Apps; only in-memory secure storage."""
import json
import shutil
import unittest
from unittest.mock import patch

import test_review_regressions as fixtures


class FailedCandidateIdentityTests(unittest.TestCase):
    setUp = fixtures.UpgradeRuntimeTests.setUp
    candidate = fixtures.UpgradeRuntimeTests.candidate
    task = fixtures.UpgradeRuntimeTests.task

    def test_failed_history_read_retains_candidate_identity_for_rollback_and_reopen(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.standalone_source import inspect_install_source
        from src.core.agent_entry.standalone_state import install_state_root
        from src.core.agent_entry.installed_materials import _private_json
        from src.core.agent_entry.task_handover import FILENAME
        self.task()
        self.candidate('zz-baseline', '/usr/bin/false')
        baseline = prepare_current_app(backup_app=self.backup)
        backup = self.root/'baseline/Lulu.app'
        backup.parent.mkdir()
        shutil.copytree(self.app, backup, symlinks=True)
        self.candidate('zz-failed', '/usr/bin/printf')
        proof = inspect_install_source(self.app)
        candidate = proof.receipt['core_cdhash']
        digest = next(row['sha256'] for row in proof.manifest['components']
                      if row['path'] == 'Contents/MacOS/lulu-agent-core')
        failed_state = install_state_root(self.app, self.product)
        with patch('src.core.agent_entry.standalone_history.capture_history',
                   side_effect=RuntimeError('injected failure after user authorization')):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                prepare_current_app(backup_app=backup)
        self.assertFalse(failed_state.exists())
        # The failed candidate has no ownership receipt. Restore the baseline.
        shutil.rmtree(self.app)
        shutil.copytree(backup, self.app, symlinks=True)
        def reader(_keys, _core, generation, *, known_cores):
            self.assertEqual(known_cores[digest], candidate)
            return self.old_keys
        for _ in range(2):
            with patch('src.core.agent_entry.standalone_history.historical_keys', side_effect=reader) as read:
                self.assertEqual(prepare_current_app(), baseline)
                read.assert_called_once()
            handover = _private_json(baseline/'product-materials-v2'/FILENAME)
            self.assertEqual([row['job_id'] for row in handover['body']['jobs']], ['job-001'])

    def test_unverified_candidate_is_never_remembered(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.attempted_cores import read_attempted_cores
        self.candidate('zz-broken', '/usr/bin/false')
        executable = self.app/'Contents/MacOS/lulu-agent-core'
        executable.write_bytes(executable.read_bytes()+b'tampered')
        with self.assertRaises(RuntimeError):
            prepare_current_app(backup_app=self.backup)
        self.assertEqual(read_attempted_cores(self.product, self.owner, self.keys), {})

    def test_attempt_identity_tampering_stops_before_history_access(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        self.candidate('zz-candidate', '/usr/bin/false')
        with patch('src.core.agent_entry.standalone_history.capture_history', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                prepare_current_app(backup_app=self.backup)
        path = self.product/'AgentEntry/attempted-cores.json'
        value = json.loads(path.read_text())
        value['body']['cores']['f'*64] = 'e'*40
        path.write_text(json.dumps(value))
        with patch('src.core.agent_entry.standalone_history.capture_history') as capture:
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
                prepare_current_app(backup_app=self.backup)
            capture.assert_not_called()

    def test_identity_receipt_write_failure_prevents_any_historical_read(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        self.candidate('zz-candidate', '/usr/bin/false')
        with patch('src.core.agent_entry.attempted_cores.atomic_json', side_effect=OSError('disk full')), \
                patch('src.core.agent_entry.standalone_history.capture_history') as capture:
            with self.assertRaisesRegex(OSError, 'disk full'):
                prepare_current_app(backup_app=self.backup)
            capture.assert_not_called()

    def test_authenticated_attempt_cannot_move_to_another_installation_or_product_root(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.attempted_cores import read_attempted_cores
        self.candidate('zz-candidate', '/usr/bin/false')
        prepare_current_app(backup_app=self.backup)
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
            read_attempted_cores(self.product, dict(self.owner, installation_id='other-installation'), self.keys)
        other = self.root/'other-product'
        shutil.copytree(self.product/'AgentEntry', other/'AgentEntry')
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
            read_attempted_cores(other, self.owner, self.keys)

    def test_current_writer_identities_require_bound_owner_and_authenticated_handover(self):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.attempted_cores import current_core_identities
        from src.core.agent_entry.installed_materials import _private_json, MATERIALS_DIR
        from src.core.agent_entry.task_handover import FILENAME
        self.candidate('zz-candidate', '/usr/bin/false')
        state = prepare_current_app(backup_app=self.backup)
        generation = _private_json(state/'ownership.json')['core_cdhash']
        self.assertEqual(current_core_identities(self.app, product_root=self.product,
            generation=generation, keys=self.keys), frozenset({generation, self.owner['core_cdhash']}))
        with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_COMPONENT_MISMATCH'):
            current_core_identities(self.app, product_root=self.product, generation='f'*40, keys=self.keys)
        path = state/MATERIALS_DIR/FILENAME
        value = _private_json(path); value['body']['source_cores']['a'*64] = 'e'*40
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
            current_core_identities(self.app, product_root=self.product, generation=generation, keys=self.keys)

    def _rollback_during_first_storage(self, cut):
        from src.core.agent_entry.standalone_runtime import prepare_current_app
        from src.core.agent_entry.installed_materials import _private_json
        from src.core.agent_entry.product_transaction import snapshot_digest
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        from test_core_storage_binding import CoreMemoryKeys
        from copy import deepcopy
        self.task()
        self.candidate('zz-initialization', '/usr/bin/false')
        candidate_state = prepare_current_app(backup_app=self.backup)
        generation = _private_json(candidate_state/'ownership.json')['core_cdhash']
        candidate_copy = self.root/'candidate/Lulu.app'; candidate_copy.parent.mkdir()
        shutil.copytree(self.app, candidate_copy, symlinks=True)
        storage = self.product/'task-generations'/generation; storage.mkdir(mode=0o700)
        candidate_keys = CoreMemoryKeys()
        with self.assertRaisesRegex(RuntimeError, 'CORE_STORAGE_CRASH'):
            prepare_core_task_store(storage, self.owner['installation_id'], candidate_keys,
                                    crash_after=cut, legacy_storage=False)
        before, secrets, writes = snapshot_digest(storage), deepcopy(candidate_keys.values), list(candidate_keys.writes)
        def reader(_keys, _core, identity, **kwargs):
            return candidate_keys if identity == generation else self.old_keys
        shutil.rmtree(self.app); shutil.copytree(self.backup, self.app, symlinks=True)
        with patch('src.core.agent_entry.standalone_history.historical_keys', side_effect=reader):
            self.assertEqual(prepare_current_app(), self.a)
        self.assertEqual(snapshot_digest(storage), before)
        self.assertEqual((candidate_keys.values, candidate_keys.writes), (secrets, writes))
        shutil.rmtree(self.app); shutil.copytree(candidate_copy, self.app, symlinks=True)
        with patch('src.core.agent_entry.standalone_history.historical_keys', side_effect=reader):
            self.assertEqual(prepare_current_app(backup_app=self.backup), candidate_state)
        self.assertEqual(prepare_core_task_store(storage, self.owner['installation_id'],
                                                candidate_keys, legacy_storage=False).list_jobs(), ())
        self.assertEqual(sum(op == 'create' and name.startswith('lulu_core_job_key_v1:')
                             for op, name in candidate_keys.writes), 1)

    def test_rollback_and_retry_after_first_registry_registration(self):
        self._rollback_during_first_storage('registered')

    def test_rollback_and_retry_after_empty_ledger_before_ready(self):
        self._rollback_during_first_storage('ledger')
