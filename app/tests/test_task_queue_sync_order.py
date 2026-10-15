"""Queue refresh and startup ordering; isolated GUI with no network or Core."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, Mock
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.task import TaskState
from src.core.task_queue_store import TaskQueueSnapshot
from tests import test_task_groups as fixture


class QueueSyncOrderTests(TestCase):
    setUpClass = fixture.TaskGroupRuntimeTests.__dict__['setUpClass']
    setUp = fixture.TaskGroupRuntimeTests.setUp
    tearDown = fixture.TaskGroupRuntimeTests.tearDown
    _make_group = fixture.TaskGroupRuntimeTests._make_group


    def queued(self):
        group = self._make_group(2)
        for member in group.members:
            existing = self.window._task(member.task_id)
            existing._core_state = 'prepared'
        task = self.window._task(group.members[0].task_id)
        task.state = TaskState.QUEUED
        task._core_state = 'queued'
        task._core_job_revision = 2
        task._core_result_ref = None
        task._core_view_only = False
        self.window._task_queue_store = SimpleNamespace(
            async_write=True, authoritative=True,
            _desired_state=CoreTaskQueueStore._desired_state,
        )
        self.window._core_recovery_pending = False
        return task

    def test_recovery_write_failure_releases_unstarted_job_and_allows_resume(self):
        task = self.queued()
        window = self.window
        window._task_queue_startup_pending = False
        window._task_queue_recovery_checked = True
        save = Mock(side_effect=OSError('disk full'))
        window._task_queue_recovery = SimpleNamespace(save=save)
        window._pump()
        self.assertIsNone(window.current)
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertIsNone(window._core_starting_task_id)
        self.assertTrue(task._core_recovered_queue)
        window._task_queue_auto_sync_allowed = True
        with patch.object(window, '_start_task_queue_connection') as connect:
            window._sync_shared_tasks()
        connect.assert_called_once()
        window._task_queue_startup_pending = False
        save.side_effect = None
        # Exercise the real asynchronous save/acknowledgement path with a local
        # ledger substitute. Download/recognition remain a controllable worker.
        def confirm(tasks, groups):
            for snapshot in tasks:
                snapshot._core_state = CoreTaskQueueStore._desired_state(snapshot)
                snapshot._core_job_revision = getattr(snapshot, '_core_job_revision', 1) + 1
                snapshot._core_result_ref = None
                snapshot._core_view_only = False
            return True
        window._task_queue_store.save = confirm
        from PySide6.QtTest import QTest
        import time
        with patch('src.ui.worker.TranscribeWorker', fixture._FakeWorker):
            window._start_task(task.id)
            deadline = time.monotonic() + 3
            while window.current is None and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertIsNotNone(window.current)
            self.assertIs(window.current[0], task)
            window.current[1].finish_cancelled()
            while window._core_save_worker is not None and time.monotonic() < deadline:
                QTest.qWait(5)
            self.assertIsNone(window._core_save_worker)


    def test_recovery_failure_keeps_inflight_claim_until_authoritative_refresh(self):
        from copy import copy
        task = self.queued()
        window = self.window
        task.state = TaskState.RUNNING
        window._core_starting_task_id = task.id
        window._task_queue_recovery_checked = True
        window._task_queue_recovery = SimpleNamespace(save=Mock(side_effect=OSError('disk full')))
        window._core_save_worker = object()
        window._task_queue_auto_sync_allowed = True
        self.assertFalse(window._persist_task_queue())
        self.assertEqual(window._core_starting_task_id, task.id)
        with patch.object(window, '_start_task_queue_connection') as connect:
            window._sync_shared_tasks()
        connect.assert_not_called()
        window._core_save_worker = None
        acknowledged = copy(task)
        acknowledged._core_state = 'running'
        acknowledged._core_job_revision += 1
        window._restore_task_queue(TaskQueueSnapshot((acknowledged,), ()))
        self.assertIsNone(window._core_starting_task_id)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIsNone(window.current)
        self.assertIn('尚未开始', task.error)

    def test_recovery_failure_does_not_cancel_real_running_attempt(self):
        task = self.queued()
        window = self.window
        task.state = TaskState.RUNNING
        worker = Mock()
        window.current = (task, worker)
        window._task_queue_recovery_checked = True
        window._task_queue_recovery = SimpleNamespace(save=Mock(side_effect=OSError('disk full')))
        try:
            self.assertFalse(window._persist_task_queue())
            self.assertEqual(task.state, TaskState.RUNNING)
            self.assertIs(window.current[1], worker)
            worker.request_cancel.assert_not_called()
        finally:
            window.current = None

    def test_refresh_in_flight_does_not_turn_next_job_into_interrupted_start(self):
        task = self.queued()
        self.window._task_queue_startup_pending = True
        with patch.object(self.window, '_persist_task_queue') as save:
            self.window._pump()
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertIsNone(getattr(self.window, '_core_starting_task_id', None))
        save.assert_not_called()

    def test_start_confirmation_in_flight_defers_periodic_refresh(self):
        task = self.queued()
        self.window._core_starting_task_id = task.id
        self.window._task_queue_auto_sync_allowed = True
        self.window._task_queue_startup_pending = False
        with patch.object(self.window, '_start_task_queue_connection') as connect:
            self.window._sync_shared_tasks()
        connect.assert_not_called()
        self.assertFalse(self.window._task_queue_startup_pending)

    def test_refresh_completion_resumes_current_queue_but_not_recovered_queue(self):
        task = self.queued()
        self.window._task_queue_startup_pending = True
        for recovered in (False, True):
            task._core_recovered_queue = recovered
            task.state = TaskState.QUEUED
            self.window._core_starting_task_id = None
            with patch.object(self.window, '_restore_task_queue'), \
                 patch.object(self.window, '_persist_task_queue') as save:
                self.window._task_queue_connection_ready(
                    self.window._task_queue_store, TaskQueueSnapshot((), ()))
            self.assertEqual(task.state, TaskState.QUEUED if recovered else TaskState.RUNNING)
            self.assertEqual(save.call_count, 0 if recovered else 1)
