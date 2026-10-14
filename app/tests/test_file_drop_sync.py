import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.ui import theme as T
from src.ui.voice_page import MiniMaxCloneDialog


class FileDropSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        T.apply_theme("warm")
        self.app.setStyleSheet(T.QSS)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimax_clone_uses_upload_hero_and_preserves_selected_path(self):
        dialog = MiniMaxCloneDialog()
        dialog.show()
        self.app.processEvents()
        sample = self.root / "授权样本音频.wav"
        sample.write_bytes(b"sample")
        dialog._load_file(str(sample))

        self.assertEqual(dialog.file_drop.property("surfaceVariant"), "uploadHero")
        self.assertEqual(dialog.file_drop.title.text(), "拖入本地授权样本音频")
        self.assertTrue(dialog.file_drop.accessibleName())
        self.assertTrue(dialog.file_edit.isHidden())
        self.assertEqual(dialog.file_edit.text(), str(sample))
        self.assertIn(sample.name, dialog.file_drop.sub.text())



if __name__ == "__main__":
    unittest.main()
