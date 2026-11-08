"""Exercise real Qt timers while a fake native recorder waits; no device access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import MethodType, SimpleNamespace
from threading import Event
import tempfile
import unittest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from src.ui.main_window import MainWindow


class RecordingResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'system.m4a'
        self.window = QWidget();self.addCleanup(self.window.close)
        w = self.window
        for name in ('_run_recording_call', '_start_system_recording', '_stop_system_recording',
                     '_cancel_recording', '_toggle_record', '_on_rec_finished', '_reset_rec_ui',
                     '_set_recording_ui', '_friendly_system_recording_error'):
            setattr(w, name, MethodType(getattr(MainWindow, name), w))
        w._recording_path = lambda _: str(self.output)
        w._rec_started_label = '验收';w._system_rec_session = None
        w._mixed_rec_active = False;w._mixed_pending_stop = False
        w._rec_cancel_requested = False;w._rec_elapsed_timer = QTimer(w)
        self.snapshots = [];self.notices = [];self.added = []
        w._publish_extraction_state = lambda: self.snapshots.append(getattr(w, '_recording_busy', ''))
        w._toast = self.notices.append
        w._add_task = lambda *a: self.added.append(a)
        self.cancelled = []
        self.session = SimpleNamespace(cancel=lambda:self.cancelled.append(True), is_active=lambda:True)
        self.release = Event();self.started = []
        def start(_):
            self.started.append(self.release.wait(2))
            return self.session
        w._ensure_system_audio_backend = lambda: SimpleNamespace(start=start)

    def test_start_keeps_event_loop_responsive_and_repeated_click_does_not_start_twice(self):
        def interact():
            self.window._toggle_record()
            self.release.set()
        QTimer.singleShot(25, interact)
        self.window._start_system_recording()
        self.assertEqual(self.started, [True])
        self.assertIs(self.window._system_rec_session, self.session)
        self.assertTrue(self.window._rec_elapsed_timer.isActive())
        self.assertIn('正在准备录音', self.snapshots)
        self.window._rec_elapsed_timer.stop()

    def test_cancel_while_starting_disposes_session_without_claiming_recording(self):
        def cancel():
            self.window._cancel_recording();self.release.set()
        QTimer.singleShot(25, cancel)
        self.window._start_system_recording()
        self.assertEqual(self.started, [True]);self.assertEqual(self.cancelled, [True])
        self.assertIsNone(self.window._system_rec_session)
        self.assertFalse(self.window._rec_elapsed_timer.isActive())
        self.assertFalse(self.added)

    def test_cancel_while_saving_keeps_window_live_and_never_enqueues_output(self):
        self.output.write_bytes(b'qa-only-recording')
        completed = []
        def stop():
            completed.append(self.release.wait(2))
            return SimpleNamespace(output_path=self.output)
        self.session.stop = stop;self.window._system_rec_session = self.session
        QTimer.singleShot(25, lambda:(self.window._cancel_recording(), self.release.set()))
        self.window._stop_system_recording()
        self.assertEqual(completed, [True]);self.assertFalse(self.added)
        self.assertFalse(self.output.exists());self.assertIsNone(self.window._system_rec_session)

    def test_helper_ending_itself_settles_existing_recording_instead_of_leaving_timer_running(self):
        self.window._rec_active_source='system_audio'
        self.window._system_rec_session=SimpleNamespace(is_active=lambda:False)
        settled=[];self.window._stop_system_recording=lambda:settled.append(True)
        MainWindow._on_rec_elapsed_tick(self.window)
        self.assertEqual(settled,[True])
