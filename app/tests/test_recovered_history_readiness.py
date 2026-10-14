"""Exercise real recovery/ledger methods without a window, Core or credentials."""
from copy import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.gui_task_recovery import GuiTaskRecoveryStore
from src.core.models import Transcript
from src.core.task import Task, TaskState
from src.core.task_queue_store import TaskQueueSnapshot
from src.core.updater.startup import UpdateReadyGate
from src.ui.main_window import MainWindow


class RecoveredHistoryReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.recovery = GuiTaskRecoveryStore(Path(self.directory.name)/'unsynced.json')
        task = Task('gui-job-history', '历史任务', 'sample.wav', 'file', state=TaskState.CANCELLED)
        self.recovery.save([task], [])
        task._core_view_only = True
        task._core_state = 'cancelled'
        task._core_job_revision = 2
        task._core_result_ref = None
        self.snapshot, found = self.recovery.merge(TaskQueueSnapshot((task,), ()))
        self.assertTrue(found)
        self.dispatch = Mock(side_effect=AssertionError('No business command expected'))
        store = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.window = SimpleNamespace(
            tasks=list(self.snapshot.tasks), task_groups=[], current=None,
            _task_queue_recovery=self.recovery, _task_queue_recovery_checked=True,
            _core_recovery_pending=True, _core_save_pending=False, _core_save_worker=None,
            _core_hidden_intents=set(), _task_queue_store=store,
            _task_queue_store_failed=False, _task_queue_snapshot_locked=False,
            _task_queue_startup_pending=False, _task_queue_initial_connection=False,
            _close_settling=False, _result_recovery_failed=False,
            isVisible=lambda: True, _toast=Mock(), _pump=Mock(),
            _refresh_badge=Mock(), _suspend_unstarted_tasks=Mock(),
        )
        w = self.window
        w._task = lambda task_id: next((t for t in w.tasks if t.id == task_id), None)
        w._persist_task_queue = lambda **kw: MainWindow._persist_task_queue(w, **kw)
        w._finish_core_task_save = lambda worker: MainWindow._finish_core_task_save(w, worker)
        self.workers = []

        def worker_factory(store, tasks, groups, parent):
            worker = SimpleNamespace(store=store, tasks=[copy(t) for t in tasks],
                groups=list(groups), finished=Mock(), start=Mock(), deleteLater=Mock())
            self.workers.append(worker)
            return worker

        worker_patch = patch('src.ui.worker.CoreTaskSaveWorker', side_effect=worker_factory)
        worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def settle(self):
        worker = self.window._core_save_worker
        self.assertIsNotNone(worker, 'Recovery must reach its acknowledgement path')
        worker.ok = worker.store.save(worker.tasks, worker.groups)
        self.window._finish_core_task_save(worker)

    def test_readonly_history_finishes_recovery_and_becomes_ready(self):
        self.window._persist_task_queue()
        self.settle()
        self.assertFalse(self.window._core_recovery_pending)
        self.assertFalse(self.recovery.path.exists())
        self.assertEqual(len(list(self.recovery.path.parent.glob('recovered-*.json'))), 1)
        self.dispatch.assert_not_called()
        gate = UpdateReadyGate()
        self.assertFalse(gate.observe(self.window, now=0))
        self.assertTrue(gate.observe(self.window, now=2))

    def test_empty_recovered_history_also_settles(self):
        self.window.tasks = []
        self.window._persist_task_queue()
        self.settle()
        self.assertFalse(self.window._core_recovery_pending)

    def test_inflight_save_is_not_bypassed_by_readonly_history(self):
        self.window._core_save_worker = object()
        self.assertFalse(self.window._persist_task_queue())
        self.assertTrue(self.window._core_save_pending)
        self.assertEqual(self.workers, [])

    def test_unsaved_history_body_is_preserved_and_blocks_ready(self):
        self.window.tasks[0].transcript = Transcript(text='尚未保存的内容')
        self.window._persist_task_queue()
        self.settle()
        self.assertTrue(self.window._core_recovery_pending)
        restored, found = self.recovery.merge(TaskQueueSnapshot((), ()))
        self.assertTrue(found)
        self.assertEqual(restored.tasks[0].transcript.text, '尚未保存的内容')
        gate = UpdateReadyGate()
        self.assertFalse(gate.observe(self.window, now=0))
        self.assertFalse(gate.observe(self.window, now=20))

    def test_archive_failure_retains_draft_and_does_not_claim_ready(self):
        self.window._persist_task_queue()
        with patch.object(self.recovery, 'archive', side_effect=OSError('disk unavailable')):
            self.settle()
        self.assertTrue(self.recovery.path.exists())
        self.assertTrue(self.window._core_recovery_pending)
        self.window._toast.assert_called_once()

    def test_archive_retry_is_not_skipped_for_unchanged_task_signature(self):
        self.window._persist_task_queue()
        with patch.object(self.recovery, 'archive', side_effect=OSError('disk unavailable')):
            self.settle()
        self.window._persist_task_queue()
        self.settle()
        self.assertFalse(self.window._core_recovery_pending)

    def test_failed_save_keeps_recovery_pending(self):
        self.window._persist_task_queue()
        worker = self.window._core_save_worker
        self.assertIsNotNone(worker)
        worker.ok = False
        self.window._finish_core_task_save(worker)
        self.assertTrue(self.window._core_recovery_pending)
        self.assertTrue(self.window._task_queue_store_failed)
        self.assertTrue(self.recovery.path.exists())


if __name__ == '__main__':
    unittest.main()
