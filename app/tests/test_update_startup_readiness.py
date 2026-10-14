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

    def test_ui_import_failure_never_acknowledges_update(self):
        original = builtins.__import__

        def import_with_failure(name, *args, **kwargs):
            if name == "PySide6.QtCore":
                raise RuntimeError("fixture UI import failure")
            return original(name, *args, **kwargs)

        with patch.object(main.sys, "argv", ["Lulu", "--lulu-update-ready", "/tmp/unused-plan"]), \
                patch.object(main, "_write_update_ready_if_requested") as ready, \
                patch("builtins.__import__", side_effect=import_with_failure):
            with self.assertRaisesRegex(RuntimeError, "fixture UI import failure"):
                main.main()
        ready.assert_not_called()

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

    def test_requires_stable_ready_window_and_resets_after_failure(self):
        from src.core.updater.startup import UpdateReadyGate
        gate = UpdateReadyGate()
        window = self.window()
        self.assertFalse(gate.observe(window, now=10))
        self.assertFalse(gate.observe(window, now=11))
        self.assertTrue(gate.observe(window, now=12))
        window._task_queue_store_failed = True
        self.assertFalse(gate.observe(window, now=13))
        window._task_queue_store_failed = False
        self.assertFalse(gate.observe(window, now=14))
        self.assertTrue(gate.observe(window, now=16))

    def test_document_workspace_must_finish_loading_before_update_confirmation(self):
        from src.core.updater.startup import UpdateReadyGate
        window = self.window()
        window.library_page = SimpleNamespace(initialization_succeeded=False)
        gate = UpdateReadyGate()
        self.assertFalse(gate.observe(window, now=0))
        self.assertFalse(gate.observe(window, now=20))
        window.library_page.initialization_succeeded = True
        self.assertFalse(gate.observe(window, now=21))
        self.assertTrue(gate.observe(window, now=23))

    def test_incomplete_or_failed_startup_never_acknowledges(self):
        from src.core.updater.startup import UpdateReadyGate
        cases = {
            "_close_settling": True, "_install_preparation_pending": True, "_task_queue_initial_connection": True,
            "_task_queue_startup_pending": True, "_task_queue_store_failed": True,
            "_task_queue_snapshot_locked": True, "_core_recovery_pending": True,
            "_core_save_pending": True, "_core_save_worker": object(),
            "_result_recovery_failed": True, "_task_queue_store": None,
            "isVisible": lambda: False, "_task_queue_recovery": object(),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                window = self.window()
                setattr(window, name, value)
                gate = UpdateReadyGate()
                self.assertFalse(gate.observe(window, now=0))
                self.assertFalse(gate.observe(window, now=20))

    def test_missing_window_state_fails_closed(self):
        from src.core.updater.startup import UpdateReadyGate
        gate = UpdateReadyGate()
        window = SimpleNamespace(isVisible=lambda: True)
        self.assertFalse(gate.observe(window, now=0))
        self.assertFalse(gate.observe(window, now=20))

    def test_qt_timer_writes_only_after_business_readiness_and_then_stops(self):
        from PySide6.QtWidgets import QApplication, QWidget
        application = QApplication.instance() or QApplication([])
        window = QWidget()
        for name, value in vars(self.window()).items():
            if name != "isVisible":
                setattr(window, name, value)
        window._task_queue_initial_connection = True
        window.show()
        application.processEvents()
        try:
            with patch.object(main.sys, "argv", ["Lulu", "--lulu-update-ready", "/tmp/unused-plan"]), \
                    patch.object(main, "_write_update_ready_if_requested", return_value=True) as ready, \
                    patch("time.monotonic", side_effect=[0, 10, 12]):
                main._schedule_update_ready(window)
                timer = window._update_ready_timer
                self.assertTrue(timer.isActive())
                timer.timeout.emit()
                ready.assert_not_called()
                window._task_queue_initial_connection = False
                timer.timeout.emit()
                ready.assert_not_called()
                timer.timeout.emit()
                ready.assert_called_once_with()
                self.assertFalse(timer.isActive())
        finally:
            window.close()
            window.deleteLater()
            application.processEvents()

    def test_normal_startup_does_not_create_update_timer(self):
        window = self.window()
        with patch.object(main.sys, "argv", ["Lulu"]):
            main._schedule_update_ready(window)
        self.assertFalse(hasattr(window, "_update_ready_timer"))
