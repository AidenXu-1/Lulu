"""The real default window must render before its Core connection is prepared."""
import sys
import threading
import time
from unittest.mock import patch
from unittest.mock import Mock
from tests.test_task_queue_ui_recovery import TaskQueueUiRecoveryTests
from src.ui.main_window import MainWindow
from src.config import AppConfig
from PySide6.QtWidgets import QDialog, QLabel


class DefaultCoreStartupUiTests(TaskQueueUiRecoveryTests):
    def test_upgrade_integrity_error_explains_recovery_without_authorization_loop(self):
        for code in ('JOB_CORE_ACCESS_INVALID', 'JOB_CORE_PARTITION_INVALID',
                     'PRODUCT_TASK_HANDOVER_INVALID'):
            with self.subTest(code=code):
                window = self._window()
                window.show()
                window._install_preparation_pending = True
                notices = []
                window._toast = notices.append
                window.finish_install_preparation(False, reason=code)
                self.assertTrue(window.isVisible())
                self.assertIn('保留', window._install_preparation_error)
                self.assertIn(code, window._install_preparation_error)
                self.assertNotIn('重新打开', window._install_preparation_error)
                self.assertNotIn('完成授权', window._install_preparation_error)
                self.assertFalse(window._task_queue_auto_sync_allowed)
                self.assertTrue(notices)

    def test_history_group_collision_preserves_both_batches_on_repeated_restore(self):
        from src.core.gui_task_ledger import CoreTaskQueueStore
        from src.core.task import Task, TaskGroup, TaskGroupMember
        from src.core.task_queue_store import TaskQueueSnapshot
        window = self._window()
        window._task_queue_store = CoreTaskQueueStore.unavailable()
        window._install_preparation_pending = True
        paths = [self.root / f'new-{i}.wav' for i in range(2)]
        for path in paths:
            path.write_bytes(b'isolated fixture')
        window.extraction_workspace.add_files([str(p) for p in paths])
        old = Task('gui-job-old', '旧输入', '/fixture/old.wav', 'file')
        old_group = TaskGroup(1, '旧批次', [TaskGroupMember('', old.title, old.source, old.kind, old.id)], expanded=True)
        snapshot = TaskQueueSnapshot((old,), (old_group,))
        for _ in range(2):
            window._restore_task_queue(snapshot)
            self.assertEqual(len(window.tasks), 3)
            groups = {g.name: g for g in window.task_groups}
            self.assertEqual(set(groups), {'旧批次', '本地文件'})
            self.assertEqual(len({g.id for g in window.task_groups}), 2)
            self.assertTrue(groups['旧批次'].expanded)
            self.assertEqual(groups['旧批次'].members[0].task_id, old.id)
            self.assertEqual(len(groups['本地文件'].members), 2)

    def test_failed_preparation_rejects_single_all_and_group_start_without_mutation(self):
        from src.core.gui_task_ledger import CoreTaskQueueStore
        from src.core.task import TaskGroup, TaskGroupMember, TaskState
        window = self._window()
        window._task_queue_store = CoreTaskQueueStore.unavailable()
        window._install_preparation_pending = True
        task = window._add_task('/fixture/input.wav', 'file', '本次输入')
        group = TaskGroup(1, '本次批次', [TaskGroupMember('', task.title, task.source, task.kind, task.id)])
        window.task_groups = [group]
        notices = []
        window._toast = notices.append
        window.finish_install_preparation(False)
        policy = task.execution_policy
        for operation in (lambda: window._start_task(task.id), window._start_all,
                          lambda: window._start_task_group(group.id)):
            notices.clear()
            operation()
            self.assertEqual(task.state, TaskState.PENDING)
            self.assertEqual(task.execution_policy, policy)
            self.assertTrue(any('重新打开 Lulu' in text for text in notices))
            self.assertIsNone(window.current)
        task._core_recovered_queue = True
        task.state = TaskState.QUEUED
        window._paused_task_group_ids.add(group.id)
        window._toggle_task_group_pause(group.id)
        self.assertIn(group.id, window._paused_task_group_ids)
        window._start_task_group(group.id)
        self.assertTrue(task._core_recovered_queue)
        task.state = TaskState.FAILED
        window._retry_task_group(group.id)
        self.assertEqual(task.state, TaskState.FAILED)

    def test_real_preparation_close_preserves_colliding_batch_metadata(self):
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task import Task, TaskGroup, TaskGroupMember
        from src.core.task_queue_store import TaskQueueSnapshot
        recovery = GuiTaskRecoveryStore(self.root / 'task-recovery' / 'unsynced.json')
        old = Task('gui-job-old', '旧输入', '/fixture/old.wav', 'file')
        old_group = TaskGroup(1, '旧批次', [TaskGroupMember('', old.title, old.source, old.kind, old.id)], expanded=True)
        recovery.save([old], [old_group])
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, 'frozen', True, create=True), patch.object(
            GuiTaskRecoveryStore, 'production', return_value=recovery), patch(
            'src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher') as factory:
            window = MainWindow(defer_core_startup=True)
            self.windows.append(window)
            window.show()
            paths = [self.root / f'new-{i}.wav' for i in range(2)]
            for path in paths:
                path.write_bytes(b'isolated fixture')
            window.extraction_workspace.add_files([str(p) for p in paths])
            self.assertTrue(window.close())
            result, found = recovery.merge(TaskQueueSnapshot((), ()))
            self.assertTrue(found)
            self.assertEqual(len(result.tasks), 3)
            groups = {g.name: g for g in result.groups}
            self.assertEqual(set(groups), {'旧批次', '本地文件'})
            self.assertTrue(groups['旧批次'].expanded)
            self.assertEqual(groups['旧批次'].members[0].task_id, old.id)
            self.assertEqual(len(groups['本地文件'].members), 2)
            self.assertEqual(len({g.id for g in result.groups}), 2)
            self.assertTrue(all(p.read_bytes() == b'isolated fixture' for p in paths))
            factory.assert_not_called()
