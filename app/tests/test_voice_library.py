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


    def test_broken_index_disables_changes_and_restored_library_reopens(self):
        original=voices.INDEX.read_bytes();voices.INDEX.write_bytes(original[:-8])
        broken=voices.INDEX.read_bytes();controller=self.manager()
        self.assertEqual(controller.count.text(),'音色库待恢复')
        self.assertIn('原资料保留',controller.error.text())
        self.assertFalse(controller.add_button.isEnabled())
        self.assertFalse(controller.record_button.isEnabled())
        self.assertEqual(voices.INDEX.read_bytes(),broken)
        self.assertTrue(Path(self.first.reference_audio_path).is_file())
        voices.INDEX.write_bytes(original);controller.rebuild()
        self.assertEqual(set(controller.rows),{self.first.id,self.second.id})
        self.assertTrue(controller.add_button.isEnabled())






    def test_partial_delete_keeps_edit_open_and_reports_retry(self):
        c=self.manager();before=voices.INDEX.read_bytes()
        def fail(path, **kwargs):
            Path(self.first.reference_audio_path).unlink()
            if not kwargs.get('ignore_errors'):raise PermissionError('injected partial removal')
        def remove(d):
            accepted=[];d.accepted.connect(lambda:accepted.append(True))
            next(b for b in d.findChildren(QPushButton) if b.text()=='删除音色').click()
            self.assertEqual(accepted,[])
            self.assertTrue(any('重试' in label.text() for label in d.findChildren(QLabel)))
            self.assertIn(self.first.id,c.rows)
            self.assertFalse(c.play_buttons[self.first.id][0].isEnabled())
            d.reject();return QDialog.Rejected
        with patch.object(QDialog,'exec',remove),patch('src.ui.voice_library.dialogs.confirm',return_value=True), \
                patch.object(voices.shutil,'rmtree',side_effect=fail):
            c.child(c.edit,self.first)
        self.assertEqual(voices.INDEX.read_bytes(),before)
        self.assertTrue(Path(self.second.reference_audio_path).is_file())
