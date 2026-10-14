"""History actions target their row and preserve model-specific entry points."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from src.ui import theme as T
from src.ui.voice_history import VoiceHistoryDialog, _record_labels

class VoiceHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.page = QWidget()
        self.page._consumer = T.consumer_theme()
        self.page._index_version = '2.5'
        self.page._indextts2_results = []
        for i in range(2):
            path = Path(self.temp.name) / f'{i}.wav'
            path.touch()
            self.page._indextts2_results.append({'output_path': str(path), 'title': f'配音 {i}'})
        self.page._indextts2_last_output = self.page._indextts2_results[0]['output_path']
        self.page._select_indextts2_result = Mock()
        self.page._reveal_indextts2_output = Mock()
        self.page._open_voice_library = Mock()
        self.workbench = SimpleNamespace(page=self.page, _open_finished_audio=Mock())
        self.dialog = VoiceHistoryDialog(self.workbench)

    def tearDown(self):
        self.dialog.close()
        self.page.close()
        self.temp.cleanup()

    def test_actions_target_their_record(self):
        view, reveal = self.dialog.record_rows[1].findChildren(QPushButton)
        reveal.click()
        self.page._reveal_indextts2_output.assert_called_once_with(self.page._indextts2_results[1])
        view.click()
        self.page._select_indextts2_result.assert_called_once_with(self.page._indextts2_results[1])
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)

    def test_missing_file_cannot_replace_current_result(self):
        Path(self.page._indextts2_results[1]['output_path']).unlink()
        with patch('src.ui.voice_history.dialogs.alert') as alert:
            self.dialog.record_rows[1].findChildren(QPushButton)[0].click()
        alert.assert_called_once()
        self.page._select_indextts2_result.assert_not_called()
        reopened = VoiceHistoryDialog(self.workbench)
        try:
            self.assertTrue(all(not b.isEnabled() for b in reopened.record_rows[1].findChildren(QPushButton)))
        finally:
            reopened.close()

    def test_library_and_import_dismiss_history_before_navigation(self):
        self.page._open_voice_library.side_effect = lambda: self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)
        self.dialog.library_button.click()
        self.page._open_voice_library.assert_called_once()
        self.dialog.import_button.click()
        self.workbench._open_finished_audio.assert_called_once()

    def test_index2_has_no_import_adjustment_and_empty_history_is_usable(self):
        self.page._index_version = '2.0'
        self.page._indextts2_results = []
        empty = VoiceHistoryDialog(self.workbench)
        try:
            self.assertIsNone(empty.import_button)
            self.assertEqual(empty.record_rows, [])
            self.assertTrue(empty.library_button.isEnabled())
        finally:
            empty.close()

    def test_metadata_does_not_invent_time_and_distinguishes_previews(self):
        self.assertEqual(_record_labels({'output_path': '/tmp/Lulu配音_20260915_231409_局部调整.wav',
            'summary': '局部调整 · 原版保留'}), ('局部调整后的配音', '局部调整', '原版保留'))
        title, kind, details = _record_labels({'title': '选段试听 · 音色 · 4.6s', 'voice_label': '音色',
            'generated_at': '2026-09-15T23:14:09', 'duration_label': '4.6s'})
        self.assertEqual((title, kind), ('音色', '选段试听'))
        self.assertIn('4.6 秒 · 未入库', details)
