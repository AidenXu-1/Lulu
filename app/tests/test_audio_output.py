import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, SIGNAL
from PySide6.QtMultimedia import QAudioOutput
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from src.ui.audio_output import create_audio_output


class AudioOutputTests(unittest.TestCase):
    def test_keeps_native_parent_volume_and_signal_behavior(self):
        app = QApplication.instance() or QApplication([])
        parent = QObject()
        output = create_audio_output(parent)
        self.assertIsInstance(output, QAudioOutput)
        self.assertIs(output.parent(), parent)
        self.assertEqual(output.volume(), 1.0)
        self.assertFalse(output.isMuted())
        self.assertEqual(output.receivers(SIGNAL("volumeChanged(float)")), 0)
        changes = QSignalSpy(output.volumeChanged)
        output.setVolume(0.42)
        self.assertAlmostEqual(output.volume(), 0.42, places=5)
        self.assertEqual(changes.count(), 1)

    def test_audio_receiver_teardown_does_not_block_ui_accessibility(self):
        # Reproduce FFmpeg's native receiver destruction on another thread.
        # The original QAudioOutput constructor hangs in queryAccessibleInterface
        # here: the receiver's Qt mutex and Python's GIL are acquired in reverse.
        # A subprocess timeout keeps a regression from hanging the test runner.
        script = textwrap.dedent("""
            import faulthandler, sys, time
            from PySide6.QtCore import QObject, QThread
            from PySide6.QtGui import QAccessible
            from PySide6.QtWidgets import QApplication, QLabel
            from src.ui.audio_output import create_audio_output
            app = QApplication([])
            faulthandler.dump_traceback_later(5, exit=True)
            output = create_audio_output()
            receiver = QObject()
            thread = QThread()
            widgets = [QLabel() for _ in range(3000)]
            receiver.moveToThread(thread)
            output.volumeChanged.connect(receiver.deleteLater)
            thread.start()
            sys.setswitchinterval(1)
            receiver.deleteLater()
            deadline = time.perf_counter() + 0.05
            while time.perf_counter() < deadline:
                pass
            for widget in widgets:
                QAccessible.queryAccessibleInterface(widget)
            thread.quit()
            assert thread.wait(1000)
            faulthandler.cancel_dump_traceback_later()
            print('AUDIO_TEARDOWN_UI_OK')
        """)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AUDIO_TEARDOWN_UI_OK", result.stdout)
