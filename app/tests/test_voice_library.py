"""Voice management UI checks: temporary library, fake audio, no model/credentials."""
import json
import wave
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLineEdit, QPlainTextEdit, QPushButton, QLabel

from tests import test_voice_page_model_shell as fixture
from tests.test_voice_page_model_shell import VoicePage, _Cfg, _Main, voice_page_module, _SignalProbe
from src.core.synthesis import voices
from src.ui.voice_wave_slider import VoiceWaveSlider


class VoiceLibraryTests(unittest.TestCase):
    # Reuse the isolation fixture, not the parent suite.
    def setUp(self):
        self.fixture = fixture.VoicePageModelShellTests()
        self.fixture.setUp()
        self.root, self.app = self.fixture.root, self.fixture.app
        self.models = patch.object(voice_page_module.local_models, 'is_downloaded', return_value=False)
        self.models.start()
        self.signal_disconnect = patch.object(_SignalProbe, 'disconnect', lambda signal, fn: signal._callbacks.remove(fn), create=True)
        self.signal_disconnect.start()
        self.sample = self.root / 'sample.wav'
        with wave.open(str(self.sample), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', ''))
            f.writeframes(b'\0\0' * 8000)
        self.first = voices.add_voice('温暖男声', str(self.sample))
        self.second = voices.add_voice('清亮女声', str(self.sample), '你好，欢迎来到声音的世界。')
        self.page = VoicePage(_Cfg(self.root), _Main())
        self.page.resize(960, 800)
        self.page.show()

    def tearDown(self):
        if self.page._voice_library_dialog:
            self.page._voice_library_dialog.reject()
        for slider in self.page.findChildren(VoiceWaveSlider):
            slider.stop_decode()
        self.page.close()
        self.signal_disconnect.stop()
        self.models.stop()
        self.fixture.tearDown()

    def manager(self, vox=False):
        self.page._open_voice_picker(self.page.voice_combo if vox else self.page.indextts2_voice_combo)
        self.app.processEvents()
        return self.page._voice_library_dialog.controller

    def test_library_search_select_and_cancel_do_not_rewrite_voices(self):
        before = voices.INDEX.read_bytes()
        c = self.manager()
        c.search.setText('女声')
        self.assertEqual(set(c.rows), {self.second.id})
        c.dialog.reject()
        self.assertEqual(voices.INDEX.read_bytes(), before)
        c = self.manager(True)
        c.use(self.second.id)
        self.assertEqual(self.page.voice_combo.currentData(), self.second.id)
        self.assertEqual(voices.INDEX.read_bytes(), before)

    def test_readiness_is_engine_specific_and_missing_audio_cannot_play(self):
        c = self.manager()
        row = c.rows[self.first.id]
        self.assertTrue(any('可直接配音' in label.text() for label in row.findChildren(QLabel)))
        c.dialog.reject()
        c = self.manager(True)
        self.assertTrue(any('完整克隆需补充参考文字' in label.text() for label in c.rows[self.first.id].findChildren(QLabel)))
        Path(self.first.reference_audio_path).unlink()
        c.rebuild()
        self.assertFalse(c.play_buttons[self.first.id][0].isEnabled())
        self.assertFalse(next(b for b in c.rows[self.first.id].findChildren(QPushButton) if b.text() in ('使用', '使用中')).isEnabled())

    def test_preview_is_exclusive_and_stops_on_search_and_close(self):
        c = self.manager()
        self.page._approved_index.voice_player.play()
        c.play(self.first)
        self.assertEqual(self.page._approved_index.voice_player.playbackState(), c.player.PausedState)
        self.assertEqual(c.player.playbackState(), c.player.PlayingState)
        c.play(self.second)
        self.assertIn('试听', c.play_buttons[self.first.id][0].accessibleName())
        self.assertIn('暂停', c.play_buttons[self.second.id][0].accessibleName())
        c.search.setText('男声')
        self.assertEqual(c.player.playbackState(), c.player.StoppedState)
        c.play(self.first)
        c.dialog.reject()
        self.assertEqual(c.player.playbackState(), c.player.StoppedState)

    def test_edit_saves_atomically_without_touching_recording(self):
        c = self.manager()
        original = Path(self.first.reference_audio_path).read_bytes()
        def edit(d):
            d.findChild(QLineEdit).setText('新的名字')
            d.findChild(QPlainTextEdit).setPlainText('这是参考录音的内容。')
            next(b for b in d.findChildren(QPushButton) if b.text() == '保存').click()
            return QDialog.Accepted
        with patch.object(QDialog, 'exec', edit):
            c.child(c.edit, self.first)
        saved = voices.get_voice(self.first.id)
        self.assertEqual(saved.name, '新的名字')
        self.assertEqual(saved.reference_text, '这是参考录音的内容。')
        self.assertEqual(Path(saved.reference_audio_path).read_bytes(), original)
        self.assertTrue(c.dialog.isVisible())

    def test_failed_edit_and_cancel_keep_metadata(self):
        c = self.manager()
        before = voices.INDEX.read_bytes()
        def edit(d):
            d.findChild(QLineEdit).setText('未保存名称')
            next(b for b in d.findChildren(QPushButton) if b.text() == '保存').click()
            self.assertTrue(any('保存失败' in label.text() for label in d.findChildren(QLabel)))
            d.reject()
            return QDialog.Rejected
        with patch.object(QDialog, 'exec', edit), patch.object(voices, 'update_voice', side_effect=OSError('保存失败')):
            c.child(c.edit, self.first)
        self.assertEqual(voices.INDEX.read_bytes(), before)

    def test_delete_cancel_and_confirm_are_limited_to_selected_voice(self):
        c = self.manager()
        def remove(d):
            next(b for b in d.findChildren(QPushButton) if b.text() == '删除音色').click()
            return QDialog.Rejected
        with patch.object(QDialog, 'exec', remove), patch('src.ui.voice_library.dialogs.confirm', return_value=False):
            c.child(c.edit, self.first)
        self.assertIsNotNone(voices.get_voice(self.first.id))
        with patch.object(QDialog, 'exec', remove), patch('src.ui.voice_library.dialogs.confirm', return_value=True):
            c.child(c.edit, self.first)
        self.assertIsNone(voices.get_voice(self.first.id))
        self.assertIsNotNone(voices.get_voice(self.second.id))
        self.assertTrue(Path(self.second.reference_audio_path).is_file())

    def test_child_workflow_does_not_dismiss_parent(self):
        c = self.manager()
        def child():
            QTest.mouseClick(c.dialog, Qt.LeftButton, pos=QPoint(5, 5))
            self.assertTrue(c.dialog.isVisible())
        c.child(child)
        QTest.keyClick(c.dialog, Qt.Key_Escape)
        self.assertIsNone(self.page.graphicsEffect())

    def test_cancelled_add_keeps_search(self):
        c = self.manager()
        c.search.setText('男声')
        with patch.object(self.page, '_new_voice'):
            c.child(c.add, False)
        self.assertEqual(c.search.text(), '男声')

    def test_add_failure_keeps_entered_name_and_allows_retry(self):
        real_add = voices.add_voice
        calls = []
        def add(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('disk error')
            return real_add(*args)
        def interact(d):
            field = d.findChild(QLineEdit)
            field.setText('新建的音色')
            save = next(b for b in d.findChildren(QPushButton) if b.text() == '保存音色')
            save.click()
            self.assertEqual(d.result(), QDialog.Rejected)
            self.assertEqual(field.text(), '新建的音色')
            self.assertTrue(next(label for label in d.findChildren(QLabel) if label.accessibleName() == '音色保存提示').text())
            save.click()
            return d.result()
        with patch.object(QDialog, 'exec', interact), patch.object(voices, 'add_voice', side_effect=add):
            self.page._new_voice_from_path(str(self.sample), target_combo=self.page.indextts2_voice_combo)
        self.assertEqual(self.page.indextts2_voice_combo.currentText(), '新建的音色')
        self.assertEqual(len(calls), 2)

    def test_metadata_failure_preserves_index_and_unknown_fields(self):
        records = json.loads(voices.INDEX.read_text())
        records[0]['legacy'] = {'keep': True}
        voices.INDEX.write_text(json.dumps(records))
        before = voices.INDEX.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                voices.update_voice(self.first.id, name='新名称')
        self.assertEqual(voices.INDEX.read_bytes(), before)
        voices.update_voice(self.first.id, name='新名称')
        self.assertEqual(json.loads(voices.INDEX.read_text())[0]['legacy'], {'keep': True})
        with self.assertRaises(ValueError):
            voices.update_voice(self.first.id, name='   ')
