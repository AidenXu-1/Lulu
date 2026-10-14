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

    def test_install_preparation_shows_window_and_gates_core_until_success(self):
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
            side_effect=RuntimeError("fixture unavailable after preparation")) as factory:
            window = MainWindow(defer_core_startup=True)
            self.windows.append(window)
            window.show()
            for _ in range(10):
                self.app.processEvents()
                time.sleep(.005)
            self.assertTrue(window.isVisible())
            self.assertTrue(window._task_queue_startup_pending)
            self.assertIsNone(window._task_queue_startup_worker)
            self.assertTrue(window.update_controller.active_work())
            factory.assert_not_called()
            window._sync_shared_tasks()
            factory.assert_not_called()
            window.finish_install_preparation(True)
            end = time.monotonic() + 2
            while not factory.called and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(.005)
            self.assertTrue(factory.called)
            window._task_queue_startup_worker.wait(2000)
            self.app.processEvents()
            self.assertTrue(window.isVisible())

    def test_failed_install_preparation_keeps_main_window_without_core_retry(self):
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher") as factory:
            window = MainWindow(defer_core_startup=True)
            self.windows.append(window)
            window.show()
            window.finish_install_preparation(False)
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            self.assertTrue(window._task_queue_store_failed)
            self.assertFalse(window._task_queue_auto_sync_allowed)
            self.assertFalse(window._install_preparation_pending)
            factory.assert_not_called()

    def test_close_during_preparation_does_not_overwrite_unread_history(self):
        from types import SimpleNamespace
        recovery = Mock()
        shell = SimpleNamespace(_install_preparation_pending=True, _close_settling=True,
                                tasks=[], task_groups=[], _task_queue_recovery=recovery)
        self.assertTrue(MainWindow._persist_task_queue(shell, force=True))
        recovery.save.assert_not_called()
        shell.tasks = [object()]
        self.assertTrue(MainWindow._persist_task_queue(shell, force=True))
        recovery.save.assert_called_once_with(shell.tasks, [], preserve_existing=True)

    def test_real_close_during_preparation_preserves_old_draft_and_new_input(self):
        import json
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task import Task
        recovery = GuiTaskRecoveryStore(self.root / "task-recovery" / "unsynced.json")
        recovery.save([Task("gui-job-prior", "上次输入", "/fixture/prior.wav", "file")], [])
        previous = json.loads(recovery.path.read_bytes())["tasks"][0]
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch.object(
            GuiTaskRecoveryStore, "production", return_value=recovery), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher") as factory:
            window = MainWindow(defer_core_startup=True)
            self.windows.append(window)
            window.show()
            new = window._add_task("/fixture/new.wav", "file", "本次输入")
            self.assertEqual(json.loads(recovery.path.read_bytes())["tasks"], [previous])
            self.assertTrue(window.close())
            self.app.processEvents()
            saved = {item["id"]: item for item in json.loads(recovery.path.read_bytes())["tasks"]}
            self.assertEqual(saved["gui-job-prior"], previous)
            self.assertEqual(saved[new.id]["task"]["source"], "/fixture/new.wav")
            self.assertFalse(window.isVisible())
            factory.assert_not_called()

    def test_close_during_preparation_with_full_disk_keeps_window_and_old_draft(self):
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task import Task
        recovery = GuiTaskRecoveryStore(self.root / "task-recovery" / "unsynced.json")
        recovery.save([Task("gui-job-prior", "上次输入", "/fixture/prior.wav", "file")], [])
        previous = recovery.path.read_bytes()
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch.object(
            GuiTaskRecoveryStore, "production", return_value=recovery), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher") as factory:
            window = MainWindow(defer_core_startup=True)
            self.windows.append(window)
            window.show()
            new = window._add_task("/fixture/new.wav", "file", "本次输入")
            with patch.object(recovery, "save", side_effect=OSError("fixture disk full")):
                self.assertFalse(window.close())
            self.assertTrue(window.isVisible())
            self.assertIn(new, window.tasks)
            self.assertEqual(recovery.path.read_bytes(), previous)
            self.assertFalse(window._close_settling)
            factory.assert_not_called()

    def test_default_window_is_visible_while_core_connection_is_still_preparing(self):
        entered, released, visible = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def slow_transport_factory():
            calls.append((threading.get_ident(), visible.is_set()))
            entered.set()
            released.wait(1)
            return lambda command, arguments: {"jobs": []}
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
            side_effect=slow_transport_factory):
            try:
                window = MainWindow()
                self.windows.append(window)
                window.show()
                visible.set()
                end = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < end:
                    self.app.processEvents()
                    time.sleep(.005)
                self.assertTrue(window.isVisible())
                self.assertTrue(entered.is_set())
                self.assertNotEqual(calls[0][0], threading.get_ident())
                self.assertTrue(calls[0][1])
                self.assertTrue(window._task_queue_startup_pending)
                self.assertFalse(any(isinstance(widget, QDialog) and widget.isVisible()
                                     for widget in self.app.topLevelWidgets()))
            finally:
                released.set()
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    self.app.processEvents()
                    worker = getattr(window, "_task_queue_startup_worker", None)
                    if worker is None or not worker.isRunning():
                        break
                    time.sleep(.005)
                self.app.processEvents()

    def test_failed_default_connection_keeps_window_visible_and_reports_not_ready(self):
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
            side_effect=RuntimeError("CORE_TASK_SERVICE_UNAVAILABLE")):
            window = MainWindow()
            self.windows.append(window)
            notices = []
            window.extraction_workspace.bridge.notice.connect(notices.append)
            window.show()
            end = time.monotonic() + 2
            while time.monotonic() < end:
                self.app.processEvents()
                if window._task_queue_store_failed:
                    break
                time.sleep(.005)
            self.assertTrue(window.isVisible())
            labels = [label.text() for label in window.findChildren(QLabel) if label.isVisible()]
            self.assertTrue(any("原记录已保留" in text for text in notices + labels))
            self.assertEqual(window.tasks, [])
            worker = window._task_queue_startup_worker
            worker.wait(1000)

    def test_close_during_default_preparation_retains_worker_until_finished(self):
        entered, released = threading.Event(), threading.Event()
        def slow_transport_factory():
            entered.set()
            released.wait(2)
            return lambda command, arguments: {"jobs": []}
        AppConfig.load().auto_update_enabled = False
        with patch.object(sys, "frozen", True, create=True), patch(
            "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
            side_effect=slow_transport_factory):
            window = MainWindow()
            self.windows.append(window)
            window.show()
            try:
                end = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < end:
                    self.app.processEvents()
                    time.sleep(.005)
                self.assertTrue(entered.is_set())
                self.assertFalse(window.has_active_work())
                window.close()
                self.assertTrue(window._task_queue_startup_worker.isRunning())
                self.assertTrue(window.isVisible())
            finally:
                released.set()
                window._task_queue_startup_worker.wait(2000)
                end = time.monotonic() + 2
                while window.isVisible() and time.monotonic() < end:
                    self.app.processEvents()
                    time.sleep(.005)
                self.assertFalse(window.isVisible())
