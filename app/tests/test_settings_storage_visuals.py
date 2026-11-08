import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from src.core import models_manager
from src.core.synthesis import voices
from src.ui import icons, theme as T
from src.ui.settings_page import SettingsPage, StorageRelocateWorker


class _Cfg:
    def __init__(self, root: Path):
        self.theme = "warm"
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voice-outputs")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models-root")
        self.auto_update_enabled = True
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"
        self.save_result = True

    def save(self):
        return self.save_result


class SettingsStorageVisualTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _Cfg(self.root)

    def tearDown(self):
        T.apply_theme("warm")
        self.tmp.cleanup()







    def test_stop_voice_relocation_keeps_old_files_and_allows_retry(self):
        old, new = self.root / 'source', self.root / 'destination'
        old.mkdir()
        (old / 'voices.json').write_text('[]')
        (old / 'fixture.wav').write_bytes(b'isolated example')
        previous = voices.current_voice_library_dir()
        self.addCleanup(voices.configure_voice_library_dir, previous)
        voices.configure_voice_library_dir(old)
        seen = []
        with self.assertRaisesRegex(RuntimeError, '已取消'):
            voices.relocate_voice_library(new, progress=lambda *value: seen.append(value),
                                          is_cancelled=lambda: bool(seen))
        self.assertEqual(voices.current_voice_library_dir(), old)
        self.assertEqual((old / 'fixture.wav').read_bytes(), b'isolated example')
        self.assertEqual(list(new.iterdir()), [])
        self.assertEqual(voices.relocate_voice_library(new), new)
        self.assertEqual((new / 'fixture.wav').read_bytes(), (old / 'fixture.wav').read_bytes())
        self.assertFalse((new / '.voices.lock').exists())





if __name__ == "__main__":
    unittest.main()
