"""Offscreen compatibility control, mocked child; no credentials or real App/data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from src.ui.legacy_task_migration import LegacyTaskMigrationPanel, LegacyTaskInheritanceController


class LegacyTaskMigrationUiTests(unittest.TestCase):
    def setUp(self):
        self.application = QApplication.instance() or QApplication([])
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.product = Path(self.tmp.name).resolve()/'Lulu'
        (self.product/'task-generations').mkdir(parents=True)
        self.busy = Mock(return_value=False)
        self.controller = LegacyTaskInheritanceController(app=Path(self.tmp.name)/'Lulu.app',
            product=self.product, busy_probe=self.busy)
        self.panel = LegacyTaskMigrationPanel(controller=self.controller)
        self.addCleanup(self.panel.close)
        self.child = Mock()
        self.child.readAllStandardOutput.return_value = b'LULU_MIGRATION_RESULT:complete\n'
        self.factory = Mock(return_value=self.child, SeparateChannels=QProcess.SeparateChannels,
            NormalExit=QProcess.NormalExit, FailedToStart=QProcess.FailedToStart, Crashed=QProcess.Crashed)
        self.boundary = patch('src.ui.legacy_task_migration.QProcess', self.factory)
        self.boundary.start(); self.addCleanup(self.boundary.stop)

    def complete(self, code=0):
        self.child.finished.connect.call_args.args[0](code, QProcess.NormalExit)

    def test_ordinary_pending_display_has_no_recovery_button_or_child(self):
        self.factory.assert_not_called()
        self.assertFalse(self.controller.active)
        self.assertTrue(self.panel.isHidden())
        self.assertTrue(self.panel.button.isHidden())
        self.assertFalse((self.product/'local-integrity-v1').exists())

    def test_first_connection_automatically_inherits_without_clicking_settings(self):
        completed = []
        self.controller.finished.connect(completed.append)
        self.controller.maybe_start(); self.controller.maybe_start()
        self.child.start.assert_called_once_with(str(self.controller.app/'Contents/MacOS/lulu-agent-core'),
            ['--inherit-legacy-tasks'])
        self.assertTrue(self.controller.active)
        self.assertTrue(self.panel.button.isHidden())
        self.complete()
        self.assertEqual(completed, [True])
        self.assertTrue(self.panel.isHidden())
        self.controller.maybe_start()
        self.assertEqual(self.child.start.call_count, 1)

    def test_reopening_complete_or_already_attempted_upgrade_never_requests_credentials(self):
        for status in ('none', 'complete', 'partial', 'failed'):
            with patch('src.core.agent_entry.local_task_storage.migration_status', return_value=status), \
                    patch('src.core.agent_entry.local_task_storage.automatic_migration_needed', return_value=False):
                controller = LegacyTaskInheritanceController(app=self.controller.app,
                    product=self.product, busy_probe=self.busy)
                controller.maybe_start()
        self.factory.assert_not_called()

    def test_failure_has_exception_retry_but_does_not_automatically_retry(self):
        self.child.readAllStandardOutput.return_value = b'LULU_MIGRATION_FAILURE:PRODUCT_PREVIOUS_CORE_REQUIRED\n'
        self.controller.maybe_start(); self.complete(code=1)
        self.assertFalse(self.controller.active)
        self.assertFalse(self.panel.isHidden())
        self.assertFalse(self.panel.button.isHidden())
        self.assertIn('原资料和新任务已保留', self.panel.note.text())
        self.controller.maybe_start()
        self.assertEqual(self.child.start.call_count, 1)
        self.panel.button.click()
        self.assertEqual(self.child.start.call_args.args[1], ['--migrate-legacy-tasks'])

    def test_active_work_prevents_exception_retry_before_child(self):
        self.controller.state = 'failed'; self.controller.changed.emit()
        self.busy.return_value = True
        self.panel.button.click()
        self.factory.assert_not_called()
        self.assertIn('请先结束当前任务', self.panel.note.text())

    def test_partial_inheritance_refreshes_tasks_and_keeps_archive_scope_visible_without_normal_button(self):
        self.child.readAllStandardOutput.return_value = b'LULU_MIGRATION_RESULT:partial\n'
        completed = []; self.controller.finished.connect(completed.append)
        self.controller.maybe_start(); self.complete()
        self.assertEqual(completed, [True])
        self.assertIn('部分', self.panel.status.text())
        self.assertIn('旧原件完整保留', self.panel.note.text())
        self.assertIn('现有任务不会被覆盖', self.panel.note.text())
        self.assertTrue(self.panel.button.isHidden())
        self.assertFalse(self.panel.isHidden())

    def test_missing_success_marker_never_hides_failed_compatibility(self):
        self.child.readAllStandardOutput.return_value = b'arbitrary output\n'
        self.controller.maybe_start(); self.complete()
        self.assertEqual(self.controller.state, 'failed')
        self.assertFalse(self.panel.isHidden())

    def test_close_cancels_only_owned_helper_and_never_reconnects_closing_window(self):
        completed = []; self.controller.finished.connect(completed.append)
        self.controller.maybe_start(); self.controller.shutdown()
        self.child.kill.assert_called_once()
        self.child.waitForFinished.assert_called_once_with(1000)
        self.complete(code=1)
        self.assertEqual(completed, [])
        self.controller.maybe_start()
        self.assertEqual(self.child.start.call_count, 1)

    def test_closed_failure_codes_cannot_display_child_instructions_or_secrets(self):
        from src.ui.legacy_task_migration import failure_message, migration_result
        self.assertIn('还有任务在运行', failure_message(b'LULU_MIGRATION_FAILURE:PRODUCT_UNFINISHED_TASKS\n'))
        self.assertNotIn('delete', failure_message(b'LULU_MIGRATION_FAILURE:delete all data\n'))
        self.assertNotIn('secret', failure_message(b'secret value\n'))
        self.assertIsNone(migration_result(b'LULU_MIGRATION_RESULT:complete\nLULU_MIGRATION_RESULT:partial\n'))

    def test_inheritance_completion_refreshes_live_and_task_connections(self):
        from types import SimpleNamespace
        from src.ui.main_window import MainWindow
        for success in (False, True):
            window = SimpleNamespace(live_controller=Mock(connected=True, worker=None), _legacy_inheritance=None,
                _start_task_queue_connection=Mock())
            MainWindow._legacy_tasks_migration_finished(window, success)
            window.live_controller.reconnect.assert_called_once_with()
            window._start_task_queue_connection.assert_called_once_with()
            self.assertTrue(window._task_queue_startup_pending)
