"""The GUI appears before mocked preparation; no service, key or install writes."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtCore import QProcess
from src.core.agent_entry.standalone_bootstrap import start_preparation
from src.core.agent_entry.standalone_source import DESCRIPTOR


class StandaloneBootstrapUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / 'Lulu.app'
        (self.bundle / DESCRIPTOR).parent.mkdir(parents=True)
        (self.bundle / DESCRIPTOR).touch()
        self.application = SimpleNamespace(quit=Mock(), aboutToQuit=Mock())
        self.process = Mock()
        self.process.state.return_value = QProcess.ProcessState.NotRunning
        self.process_type = Mock(return_value=self.process,
            ProcessState=QProcess.ProcessState, ProcessChannelMode=QProcess.ProcessChannelMode)
        self.timer = Mock()
        self.started, self.ready, self.failed = Mock(), Mock(), Mock()
        for boundary in (
            patch.object(sys, 'frozen', True, create=True),
            patch.object(sys, 'executable', str(self.bundle / 'Contents/MacOS/Lulu')),
            patch.object(sys, 'argv', ['Lulu']),
            patch('src.core.agent_entry.standalone_paths.installed_app'),
            patch('PySide6.QtCore.QProcess', self.process_type),
            patch('PySide6.QtCore.QTimer', return_value=self.timer),
            patch('PySide6.QtWidgets.QMessageBox'),
            patch('PySide6.QtWidgets.QDialog', side_effect=AssertionError('No preparation window')),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def start(self):
        return start_preparation(self.application, self.ready,
                                 on_start=self.started, on_failed=self.failed)

    def complete(self, code):
        self.process.finished.connect.call_args.args[0](code, QProcess.ExitStatus.NormalExit)

    def test_main_window_is_presented_before_core_starts_and_only_once(self):
        self.process.start.side_effect = lambda *_: self.started.assert_called_once_with()
        self.assertTrue(self.start())
        self.ready.assert_not_called()
        self.complete(0)
        self.ready.assert_called_once_with()
        self.complete(0)
        self.ready.assert_called_once_with()
        self.failed.assert_not_called()
        self.application.quit.assert_not_called()

    def test_failed_preparation_keeps_normal_main_window_and_reports_failure(self):
        self.start()
        self.complete(1)
        self.failed.assert_called_once_with()
        self.ready.assert_not_called()
        self.application.quit.assert_not_called()

    def test_preparation_failure_carries_only_the_closed_current_process_code(self):
        self.process.readAllStandardOutput.return_value = (
            b'library notice\nLULU_PREPARATION_FAILURE:JOB_CORE_ACCESS_INVALID\n')
        self.start()
        self.complete(1)
        self.assertEqual(self.application._lulu_install_preparation_failure,
                         'JOB_CORE_ACCESS_INVALID')
        self.application.quit.assert_not_called()

    def test_preparation_output_cannot_become_user_instructions(self):
        self.process.readAllStandardOutput.return_value = (
            b'LULU_PREPARATION_FAILURE:delete your secrets\n')
        self.start()
        self.complete(1)
        self.assertEqual(self.application._lulu_install_preparation_failure,
                         'INSTALLATION_PREPARATION_FAILED')

    def test_timeout_has_a_distinct_reason_and_ignores_old_child_output(self):
        self.process.readAllStandardOutput.return_value = (
            b'LULU_PREPARATION_FAILURE:JOB_CORE_ACCESS_INVALID\n')
        self.start()
        self.timer.timeout.connect.call_args.args[0]()
        self.assertEqual(self.application._lulu_install_preparation_failure,
                         'INSTALLATION_PREPARATION_TIMEOUT')
        self.complete(0)
        self.ready.assert_not_called()

    def test_success_never_keeps_a_failure_reason(self):
        self.application._lulu_install_preparation_failure = 'JOB_CORE_ACCESS_INVALID'
        self.start()
        self.complete(0)
        self.assertEqual(self.application._lulu_install_preparation_failure, '')

    def test_timeout_stops_process_and_cannot_later_release_the_task_gate(self):
        self.start()
        self.process.state.return_value = QProcess.ProcessState.Running
        self.timer.timeout.connect.call_args.args[0]()
        self.process.kill.assert_called_once_with()
        self.complete(0)
        self.ready.assert_not_called()
        self.failed.assert_called_once_with()

    def test_quit_cancels_owned_process_without_success_or_error_callbacks(self):
        self.start()
        self.process.state.return_value = QProcess.ProcessState.Running
        self.application.aboutToQuit.connect.call_args.args[0]()
        self.process.kill.assert_called_once_with()
        self.complete(0)
        self.ready.assert_not_called()
        self.failed.assert_not_called()

    def test_failed_update_quits_for_existing_rollback_protocol(self):
        with patch.object(sys, 'argv', ['Lulu', '--lulu-update-ready', '/fixture/plan.json']):
            self.start()
            self.assertEqual(self.process.start.call_args.args[1],
                             ['--prepare-install', '--update-plan', '/fixture/plan.json'])
            self.complete(1)
        self.application.quit.assert_called_once_with()
        self.ready.assert_not_called()

    def test_new_helper_allows_authorization_wait_but_old_helper_retains_short_budget(self):
        for extra, milliseconds in (([], 35000), (['--lulu-update-authorization-wait'], 300000)):
            with self.subTest(extra=extra), patch.object(sys, 'argv',
                    ['Lulu', '--lulu-update-ready', '/fixture/plan.json'] + extra):
                self.start()
                self.timer.start.assert_called_with(milliseconds)
                self.ready.assert_not_called()
                self.timer.timeout.connect.call_args.args[0]()
                self.process.kill.assert_not_called()  # mocked child already stopped
                self.assertEqual(self.application._lulu_install_preparation_failure,
                                 'INSTALLATION_PREPARATION_TIMEOUT')
                self.complete(0)
                self.ready.assert_not_called()

    def test_source_launch_has_no_preparation_or_extra_window(self):
        with patch.object(sys, 'frozen', False):
            self.assertFalse(self.start())
        self.process_type.assert_not_called()
        self.started.assert_not_called()
