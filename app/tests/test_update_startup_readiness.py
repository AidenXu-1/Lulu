import builtins
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import main


class UpdateStartupReadinessTests(unittest.TestCase):
    def test_uncertain_installer_state_blocks_ui_and_preserves_diagnostic(self):
        from src.core.updater.models import UpdateError
        original = builtins.__import__
        def forbid_startup(name, *args, **kwargs):
            if name.startswith('PySide6') or name.endswith('standalone_bootstrap'):
                raise AssertionError('uncertain replacement must not start GUI or Core')
            return original(name, *args, **kwargs)
        for error in (OSError('fixture lock unavailable'), UpdateError('unsafe_lock', 'fixture')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(main.sys, 'argv', ['Lulu']), \
                 patch.object(main.sys, 'frozen', True, create=True), \
                 patch('src.core.updater.restart_recovery.launch_pending_recovery', side_effect=error), \
                 patch('src.core.diagnostics.record_failure') as diagnostic, \
                 patch.object(main, '_show_update_recovery_error', return_value=5) as repair_window, \
                 patch('builtins.__import__', side_effect=forbid_startup):
                self.assertEqual(main.main(), 5)
                diagnostic.assert_called_once_with('update_startup', error)
                repair_window.assert_called_once_with()

    def test_live_helper_blocks_normal_gui_before_any_ui_or_core_start(self):
        original = builtins.__import__
        def forbid_ui(name, *args, **kwargs):
            if name.startswith('PySide6'):
                raise AssertionError('second GUI must not start during replacement')
            return original(name, *args, **kwargs)
        with patch.object(main.sys, 'argv', ['Lulu']), \
             patch.object(main.sys, 'frozen', True, create=True), \
             patch('src.core.updater.restart_recovery.launch_pending_recovery', return_value=True), \
             patch('builtins.__import__', side_effect=forbid_ui):
            self.assertEqual(main.main(), 0)


    @staticmethod
    def window():
        return SimpleNamespace(
            isVisible=lambda: True, _close_settling=False, _install_preparation_pending=False,
            _task_queue_store=object(), _task_queue_initial_connection=False,
            _task_queue_startup_pending=False, _task_queue_store_failed=False,
            _task_queue_snapshot_locked=False, _core_recovery_pending=False,
            _task_queue_recovery=None, _task_queue_recovery_checked=False,
            _core_save_pending=False, _core_save_worker=None,
            _result_recovery_failed=False,
        )
