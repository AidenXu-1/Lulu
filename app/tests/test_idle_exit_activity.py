"""Exit warnings distinguish active work from passive checks; no real services."""
from types import SimpleNamespace
import unittest
from src.ui.main_window import MainWindow
from src.core.agent_workspace.service import WorkspaceService


def shell(**values):
    return SimpleNamespace(tasks=[],_mixed_rec_active=False,_mixed_pending_stop=False,
        _system_rec_session=None,**values)


class IdleExitActivityTests(unittest.TestCase):
    def test_voice_environment_read_is_not_user_work(self):
        window=shell(voice_page=SimpleNamespace(_indextts2_status_worker=SimpleNamespace(isRunning=lambda:True)))
        self.assertFalse(MainWindow.has_active_work(window))

    def test_queue_save_is_waited_for_without_a_user_task_warning(self):
        window=shell(_core_save_worker=SimpleNamespace(isRunning=lambda:True))
        self.assertFalse(MainWindow.has_active_work(window))

    def test_real_synthesis_and_transcription_still_block_exit(self):
        worker=SimpleNamespace(isRunning=lambda:True)
        self.assertTrue(MainWindow.has_active_work(shell(voice_page=SimpleNamespace(_indextts2_worker=worker))))
        window=shell();window.tasks=[SimpleNamespace(is_active=True)]
        self.assertTrue(MainWindow.has_active_work(window))

    def test_read_does_not_mask_a_simultaneous_real_operation(self):
        worker=SimpleNamespace(isRunning=lambda:True)
        window=shell(voice_page=SimpleNamespace(_indextts2_status_worker=worker,_ref_worker=worker))
        self.assertTrue(MainWindow.has_active_work(window))

    def poll(self, states):
        service=object.__new__(WorkspaceService)
        service.origin='gui'
        service.runtime=SimpleNamespace(_job_control=SimpleNamespace(list=lambda:[
            SimpleNamespace(state=SimpleNamespace(value=s),job_kind='local_transcription',frozen_policy={'engine':'fixture'}) for s in states]),
            expansion_control=lambda:SimpleNamespace(poll=lambda *args:{}),_expansion_batches={})
        return service.control_poll('fixture',[],{})

    def test_prepared_records_do_not_report_running_or_lock_the_model(self):
        reply=self.poll(['prepared'])
        self.assertFalse(reply['core_busy']);self.assertEqual(reply['busy_models'],[])

    def test_running_queued_and_settling_requests_remain_protected(self):
        for state in ('queued','running','cancel_requested'):
            with self.subTest(state=state):
                reply=self.poll([state]);self.assertTrue(reply['core_busy'])
                self.assertEqual(reply['busy_models'],['fixture'])

    def test_terminal_history_is_idle(self):
        self.assertFalse(self.poll(['succeeded','failed','cancelled','partial'])['core_busy'])

    def test_measured_active_core_work_remains_protected(self):
        self.assertTrue(MainWindow.has_active_work(shell(_external_core_busy=True)))


class IdleExitWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_extraction_main_window import ExtractionMainWindowTests
        ExtractionMainWindowTests.setUpClass()

    def test_idle_close_waits_for_status_thread_without_showing_task_confirmation(self):
        import threading
        from unittest.mock import patch
        from PySide6.QtCore import QThread
        from tests.test_extraction_main_window import ExtractionMainWindowTests
        fixture=ExtractionMainWindowTests();fixture.setUp()
        window=fixture.main;previous=window.voice_page
        release=threading.Event()
        class Status(QThread):
            def run(self):release.wait(4)
        worker=Status();worker.start()
        window.voice_page=SimpleNamespace(_indextts2_status_worker=worker,shutdown_workers=lambda *a:True)
        try:
            self.assertIn(worker,window._registered_qthreads())
            with patch('src.ui.main_window.dialogs.confirm',return_value=False) as confirm, \
                    patch.object(window,'_choose_xhs_exit_action',return_value='keep'), \
                    patch.object(window,'_settle_xhs_exit',return_value=True):
                window.close()
                self.assertTrue(window.isVisible());self.assertTrue(worker.isRunning())
                confirm.assert_not_called()
                release.set();fixture.until(lambda:not window.isVisible())
                confirm.assert_not_called()
        finally:
            release.set();worker.wait(5000);window.voice_page=previous
            fixture.tearDown()
