"""A skill response may arrive while its thread still has cleanup to do."""
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from src.ui.approved_settings import ApprovedSettings
from src.ui.settings_page import _SettingsReadWorker


class SettingsSkillLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if condition():
                return
            time.sleep(.001)
        self.fail('skill operation did not settle')

    def exercise(self, *, close=False, fail=False):
        release_read, release_exit, emitted = Event(), Event(), Event()
        deletions, workers, replies = [], [], []

        class DelayedWorker(_SettingsReadWorker):
            def __init__(self, read):
                super().__init__(read)
                workers.append(self)

            def run(self):
                release_read.wait(3)
                super().run()
                emitted.set()
                release_exit.wait(3)

            def deleteLater(self):
                # Observe an unsafe deletion without crashing the test process.
                deletions.append(self.isRunning())
                if not self.isRunning():
                    super().deleteLater()

        owner = SimpleNamespace(_skill_worker=None, _settings_closing=False)
        def reply(value=None, *, message=''):
            replies.append((value, message))
        def dispatch(*args, **kwargs):
            if fail:
                raise ValueError('fixture failure')
            return {'entries': ['one']}

        with patch('src.ui.approved_settings._SettingsReadWorker', DelayedWorker), \
                patch('src.core.agent_workspace.client.dispatch', side_effect=dispatch) as call:
            try:
                ApprovedSettings._run_skill(owner, reply, 'skill_list', lambda v: v['entries'])
                worker = workers[0]
                if close:
                    owner._settings_closing = True
                release_read.set()
                self.assertTrue(emitted.wait(2))
                # Drain the result delivery while the actual thread is held open.
                self.app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.assertTrue(worker.isRunning())
                self.assertEqual(deletions, [], 'result delivery must not delete a running thread')
                self.assertIs(owner._skill_worker, worker)
                if close:
                    self.assertEqual(replies, [])
                elif fail:
                    self.assertEqual(replies, [(None, 'fixture failure')])
                else:
                    self.assertEqual(replies, [(['one'], '')])
                ApprovedSettings._run_skill(owner, reply, 'skill_list', lambda v: v)
                call.assert_called_once()
                self.assertIn('请等待', replies[-1][1])
                release_exit.set()
                self.assertTrue(worker.wait(2000))
                self.until(lambda: owner._skill_worker is None and bool(deletions))
                self.assertEqual(deletions, [False])
            finally:
                release_read.set()
                release_exit.set()
                for worker in workers:
                    from shiboken6 import isValid
                    if isValid(worker):
                        worker.wait(3000)
                        worker.deleteLater()
                self.app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_success_keeps_running_worker_and_rejects_overlap(self):
        self.exercise()

    def test_failed_result_keeps_worker_until_finished(self):
        self.exercise(fail=True)

    def test_close_ignores_late_result_and_retains_worker_until_finished(self):
        self.exercise(close=True)
