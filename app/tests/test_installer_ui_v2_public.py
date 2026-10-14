import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from src.core.agent_entry.installer_ui import create_install_dialog,independent_dmg_hash


class InstallerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_escape_and_window_close_cannot_hide_running_installation(self):
        class Worker:
            running=True
            def isRunning(self):return self.running
        worker=Worker();dialog=create_install_dialog(worker);dialog.show();self.app.processEvents()
        QTest.keyClick(dialog,Qt.Key_Escape);self.app.processEvents();self.assertTrue(dialog.isVisible())
        dialog.close();self.app.processEvents();self.assertTrue(dialog.isVisible())
        worker.running=False
        QTest.keyClick(dialog,Qt.Key_Escape);self.app.processEvents();self.assertFalse(dialog.isVisible())
        dialog.deleteLater()

    def test_hash_file_is_external_exactly_named_and_bound_by_raw_bytes(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);dmg=root/'Lulu.dmg';dmg.write_bytes(b'disk bytes')
            with self.assertRaisesRegex(RuntimeError,'PACKAGE_INDEPENDENT_HASH_REQUIRED'):
                independent_dmg_hash(dmg)
            digest=hashlib.sha256(dmg.read_bytes()).hexdigest();sidecar=root/'Lulu.dmg.sha256'
            sidecar.write_text(digest+'  Lulu.dmg\n')
            actual,source=independent_dmg_hash(dmg)
            self.assertEqual(actual,digest);self.assertEqual(source['sha256'],hashlib.sha256(sidecar.read_bytes()).hexdigest())
            sidecar.write_text(digest+'  other.dmg\n')
            with self.assertRaises(RuntimeError):independent_dmg_hash(dmg)

    def test_missing_hash_does_not_fall_back_to_a_self_computed_digest(self):
        with TemporaryDirectory() as tmp:
            dmg=Path(tmp)/'Lulu.dmg';dmg.write_bytes(b'disk bytes')
            with patch('src.core.agent_entry.installer_ui.hashlib.sha256') as digest:
                with self.assertRaisesRegex(RuntimeError,'PACKAGE_INDEPENDENT_HASH_REQUIRED'):
                    independent_dmg_hash(dmg)
                digest.assert_not_called()
