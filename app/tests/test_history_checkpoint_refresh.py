"""Rollback checkpoint refresh: temporary files and in-memory keys only."""
import json
import shutil
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from test_history_checkpoint import HistoryCheckpointTests
from test_core_storage_binding import CoreMemoryKeys
from test_phase5_job_ledger import _create_arguments


class HistoryRefreshTests(TestCase):
    setUp = HistoryCheckpointTests.setUp
    write = HistoryCheckpointTests.write
    snapshot = HistoryCheckpointTests.snapshot
    read = HistoryCheckpointTests.read

    def handover(self, rows=None, previous='c'*64):
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        from src.core.agent_entry.task_handover import FILENAME, seal_handover
        from src.core.updater.secure_io import atomic_json
        path = self.state/MATERIALS_DIR/FILENAME
        atomic_json(path, seal_handover(self.rows if rows is None else rows,
            keys=self.keys, owner=self.owner, previous_manifest=previous,
            product_root=self.root, gui_context=self.context))
        return path

    def capture(self, reader=None, context_effect=None):
        from src.core.agent_entry.standalone_history import capture_history
        reader = reader or Mock(side_effect=AssertionError('no historical key read'))
        with patch('src.core.agent_entry.standalone_history.previous_core_identity',
                return_value=(Path('/Applications/Lulu.app/Contents/MacOS/lulu-agent-core'), self.generation)), \
             patch('src.core.agent_entry.task_handover_context.capture_context',
                return_value=self.context, side_effect=context_effect):
            return capture_history(self.state, self.owner, product_root=self.root,
                keys=self.keys, key_reader=reader)

    def test_resealed_rollback_handover_refreshes_without_old_private_keys(self):
        self.write()
        self.handover(previous='d'*64)
        reader = Mock(side_effect=AssertionError('no historical key read'))
        self.assertEqual(self.capture(reader), (self.rows, self.context))
        reader.assert_not_called()
        # Pre-swap consumers still reject an obsolete checkpoint.
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASKS_CHANGED'):
            self.read()

    def test_rollback_additional_authenticated_rows_are_not_lost(self):
        self.write()
        extra = dict(self.rows[0], job_id='new-1', receipt_ref='lulu-job:new-1:revision:2')
        self.handover([extra])
        rows, _ = self.capture()
        self.assertEqual({r['job_id'] for r in rows}, {'fixture-1', 'new-1'})

    def native_ledger(self):
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        shutil.rmtree(self.taskroot)
        self.taskroot.mkdir(mode=0o700)
        keys = CoreMemoryKeys()
        store = prepare_core_task_store(self.taskroot, self.owner['installation_id'],
            keys, legacy_storage=False)
        return keys, store

    def append_terminal(self, store):
        from src.core.job_ledger.contracts import JobState
        store.create_job(**dict(_create_arguments(), installation_id=self.owner['installation_id']))
        store.transition('job-001', expected_revision=1, new_state=JobState.CANCELLED,
            changed_at='2026-08-20T01:00:01Z')

    def test_new_native_task_after_rollback_is_authenticated_and_merged(self):
        keys, store = self.native_ledger()
        self.write()
        self.append_terminal(store)
        before, writes = self.snapshot(), list(keys.writes)
        rows, _ = self.capture(Mock(return_value=keys))
        self.assertEqual({r['job_id'] for r in rows}, {'fixture-1', 'job-001'})
        self.assertEqual((self.snapshot(), keys.writes), (before, writes))

    def test_deleted_established_task_root_or_handover_is_rejected(self):
        self.write()
        shutil.rmtree(self.taskroot)
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASKS_CHANGED'):
            self.capture()
        self.taskroot.mkdir(); (self.taskroot/'state').write_text('original')
        handover = self.handover()
        from src.core.agent_entry.history_checkpoint import write_checkpoint
        from src.core.agent_entry.product_transaction import snapshot_digest
        write_checkpoint(product_root=self.root, owner=self.owner, generation=self.generation,
            keys=self.keys, records=self.rows, context=self.context,
            expected_snapshot=self.snapshot(), state=self.state,
            expected_handover_snapshot=snapshot_digest(handover))
        handover.unlink()
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASKS_CHANGED'):
            self.capture()

    def test_empty_recreated_directory_without_registry_is_not_empty_history(self):
        self.write(); shutil.rmtree(self.taskroot); self.taskroot.mkdir(mode=0o700)
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'):
            self.capture(Mock(return_value=CoreMemoryKeys()))

    def test_changed_native_ledger_tamper_missing_registry_or_anchor_is_rejected(self):
        keys, store = self.native_ledger(); self.write(); self.append_terminal(store)
        saved = dict(keys.values)
        from src.core.job_ledger.storage_binding import REGISTRY_KEY, ANCHOR_PREFIX
        for name in (REGISTRY_KEY, next(k for k in keys.values if k.startswith(ANCHOR_PREFIX))):
            with self.subTest(missing=name):
                keys.values = dict(saved); keys.values.pop(name)
                with self.assertRaises(Exception): self.capture(Mock(return_value=keys))
        keys.values = saved
        (self.taskroot/'jobs/job-ledger-v1/snapshot.json').write_text('{}')
        with self.assertRaises(Exception): self.capture(Mock(return_value=keys))

    def test_checkpoint_or_current_handover_authentication_cannot_be_bypassed(self):
        checkpoint = self.write(); handover = self.handover()
        saved = checkpoint.read_bytes()
        value = json.loads(saved); value['hmac_sha256'] = '0'*64
        checkpoint.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'): self.capture()
        checkpoint.write_bytes(saved)
        value = json.loads(handover.read_bytes()); value['body']['installation_id'] = 'other'
        handover.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'): self.capture()

    def test_authenticated_conflicting_revision_remains_rejected(self):
        self.write()
        self.handover([dict(self.rows[0], job_revision=3,
            receipt_ref='lulu-job:fixture-1:revision:3')])
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASK_HANDOVER_INVALID'): self.capture()

    def test_concurrent_task_or_handover_changes_during_refresh_are_rejected(self):
        self.write(); handover = self.handover()
        for target in (self.taskroot/'state', handover):
            with self.subTest(target=target.name):
                original = target.read_bytes()
                def race(*args, **kwargs):
                    target.write_bytes(original+b' ')
                    return self.context
                with self.assertRaisesRegex(RuntimeError, 'PRODUCT_TASKS_CHANGED'):
                    self.capture(context_effect=race)
                target.write_bytes(original)

    def test_handover_symlink_is_rejected_before_refresh(self):
        self.write(); handover = self.handover(); other = handover.with_suffix('.other')
        handover.rename(other); handover.symlink_to(other.name)
        with self.assertRaises(Exception): self.capture()
