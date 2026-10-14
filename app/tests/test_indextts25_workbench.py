"""Approved B contracts. All configuration, voices and credentials are isolated."""
import math
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import wave

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from tests import test_voice_page_model_shell as fixture
from src.ui.voice_wave_slider import VoiceWaveSlider


class WorkbenchTests(unittest.TestCase):
    def test_generation_stays_in_library_and_download_is_an_explicit_copy(self):
        p = self.page
        p._indextts2_status = fixture._ready_status()
        p._indextts2_voice_ready = lambda: True
        p._indextts2_background_status_snapshot = lambda: {'alive': False}
        p.indextts2_text_edit.setPlainText('生成后先保存到音频库。')
        download = self.fixture.root / 'downloads'
        p.cfg.tts_output_dir = str(download)
        with patch.object(fixture.voice_page_module, 'IndexTTS2SynthesisWorker', fixture._FakeIndexTTS2Worker):
            p._generate_indextts2()
            kwargs = fixture._FakeIndexTTS2Worker.instances[-1].kwargs
        output = Path(fixture._FakeIndexTTS2Worker.instances[-1].args[2])
        self.assertEqual(output.parent, fixture.voice_page_module.library.audio_storage_dir())
        self.assertFalse(download.exists())
        output.write_bytes(b'voice')
        record = {'output_path': str(output), 'default_filename': 'audio.wav'}
        with patch.object(fixture.voice_page_module.QFileDialog, 'getSaveFileName') as prompt:
            p._export_indextts2_wav(record)
            p._export_indextts2_wav(record)
        prompt.assert_not_called()
        self.assertEqual(len(list(download.glob('*.wav'))), 2)
        self.assertEqual(output.read_bytes(), b'voice')
        p._indextts2_worker = None

    def test_result_name_is_above_wave_and_download_directory_is_on_right(self):
        p = self.page
        p._select_indextts2_result({'output_path': '/missing.wav', 'summary': '测试音频'})
        p.resize(1400, 1000)
        QTest.qWait(30)
        y = lambda w: w.mapTo(p, QPoint()).y()
        x = lambda w: w.mapTo(p, QPoint()).x()
        self.assertLess(y(p.indextts2_result_summary), y(p.indextts2_player_slider))
        self.assertGreater(x(p.btn_indextts2_output_dir), x(p.btn_indextts2_export))
        self.assertEqual(p.btn_indextts2_export.text(), '下载音频')

    def test_result_reset_keeps_icon_only_when_already_stopped(self):
        p = self.page
        p._reset_indextts2_player()
        self.assertEqual(p.btn_indextts2_play.text(), '')
        self.assertEqual(p.btn_indextts2_play.accessibleName(), '播放本次结果')
        self.assertFalse(p.btn_indextts2_play.icon().isNull())

    def test_import_other_audio_is_in_history_not_current_result(self):
        from PySide6.QtWidgets import QDialog, QPushButton
        w = self.page._approved_index
        self.assertFalse(any(b.text() == '打开成品' for b in w.findChildren(QPushButton)))
        def visit_history(dialog):
            button = next(b for b in dialog.findChildren(QPushButton)
                          if b is dialog.import_button)
            button.click()
            return 0
        with patch.object(w, '_open_finished_audio') as import_audio, patch.object(QDialog, 'exec', visit_history):
            w._history()
        import_audio.assert_called_once()

    def test_initial_follow_page_is_selected_without_clicking_a_tab(self):
        self.assertEqual(self.page._indextts2_emotion_mode, 'follow')
        self.assertEqual(self.page.indextts2_emotion_stack.currentIndex(), 0)

    def test_follow_card_has_quiet_texture_and_no_empty_play_action(self):
        w = self.page._approved_index
        self.page.resize(1400, 900)
        QTest.qWait(30)
        self.assertTrue(w.follow_texture.isVisible())
        self.assertEqual(w.follow_texture.focusPolicy(), Qt.NoFocus)
        with patch.object(self.page, '_selected_indextts2_voice', return_value=None):
            w.sync_voice()
        self.assertFalse(w.follow_play.isEnabled())
        self.assertIn('选择', w.follow_name.text())
        self.page.resize(668, 900)
        QTest.qWait(30)
        self.assertTrue(w.follow_texture.isHidden())

    def test_follow_preview_button_tracks_shared_voice_player(self):
        w = self.page._approved_index
        path = self.fixture.root / 'voice.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 8000)
        with patch.object(self.page, '_selected_indextts2_voice', return_value=SimpleNamespace(
                name='很长的音色名称' * 15, reference_audio_path=str(path))):
            w.sync_voice()
        self.assertTrue(w.follow_play.isEnabled())
        w.follow_play.click()
        self.assertEqual(w.follow_play.text(), '暂停原音')
        w.voice_player.pause()
        self.assertEqual(w.follow_play.text(), '试听原音')

    def test_wave_seek_updates_player_on_press_and_drag(self):
        w = self.page._approved_index
        w.voice_player._duration = 100000
        w.voice_wave.setRange(0, 100000)
        QTest.mousePress(w.voice_wave, Qt.LeftButton, pos=QPoint(w.voice_wave.width() // 2, 22))
        self.assertAlmostEqual(w.voice_player.position(), 50000, delta=1500)
        QTest.mouseRelease(w.voice_wave, Qt.LeftButton, pos=QPoint(w.voice_wave.width() - 3, 22))
        self.assertGreater(w.voice_player.position(), 98000)

    def test_source_counts_use_spoken_section_and_preview_never_opens_sidebar(self):
        p = self.page
        body = '声' * 214
        raw = '# 标题\n来源资料' + 'x' * 340 + '\n## 逐字稿\n' + body
        with patch.object(fixture.voice_page_module.library, 'load', return_value=SimpleNamespace(text=raw)):
            p._src_set_mode('indextts2', 'library')
            p._src_on_picked('indextts2', {'id': 'test', 'title': '标题', 'kind': 'transcript', 'char_count': 585})
        self.assertEqual(p.indextts2_text_edit.toPlainText(), body)
        st = p._src['indextts2']
        self.assertEqual(st['picked_meta'].text(), '配音正文 214 字')
        self.assertIn('214', p._approved_index.selection_hint.text())
        self.assertTrue(st['picked_view'].isHidden())
        with patch.object(p._approved_index, '_view_text') as preview:
            p._src_view_picked('indextts2')
            preview.assert_called_once()

    def test_compact_toolbar_and_aligned_labels(self):
        w = self.page._approved_index
        self.page.resize(1400, 900)
        QTest.qWait(30)
        positions = [c.mapTo(w, QPoint()) for c in w.action_cells]
        self.assertEqual(len({p.y() for p in positions}), 1)
        self.assertEqual([p.x() for p in positions], sorted(p.x() for p in positions))
        self.assertEqual(w.language_label.mapTo(w, QPoint()).y(), w.speed_label.mapTo(w, QPoint()).y())
        self.assertEqual(w.language_label.alignment(), Qt.AlignCenter)
        self.assertEqual(w.speed_label.alignment(), Qt.AlignCenter)
        self.assertLess(self.page.btn_indextts2_glossary.mapTo(w, QPoint()).x(), w.view_text.mapTo(w, QPoint()).x())
        self.assertTrue(self.page.indextts2_text_length_hint.isHidden())

    def test_wave_includes_audio_after_five_minutes(self):
        path = self.fixture.root / 'long.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 355000)
            audio.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(i * .2))) for i in range(5000)))
        slider = self.page._approved_index.ref_wave
        slider.load_file(str(path))
        self.fixture._wait_until(lambda: slider._process is None, timeout=10)
        self.assertEqual(len(slider.peaks), 160)
        self.assertEqual(slider.peaks[0], 0)
        self.assertGreater(slider.peaks[-1], .9)
        self.assertLessEqual(len(slider._raw_peaks), 4096)

    def setUp(self):
        self.fixture = fixture.VoicePageModelShellTests()
        self.fixture.setUp()
        self.page = fixture.VoicePage(fixture._Cfg(self.fixture.root), fixture._Main())
        self.page.resize(1200, 900)
        self.page.show()
        self.fixture.app.processEvents()

    def tearDown(self):
        for slider in self.page.findChildren(VoiceWaveSlider):
            slider.stop_decode()
        self.page.close()
        self.fixture.tearDown()

    def test_four_modes_keep_geometry_and_visible_controls(self):
        p = self.page
        for width in (1400, 1024, 668):
            p.resize(width, 900)
            QTest.qWait(50)
            heights = []
            tops = []
            for mode in ('follow', 'emoaudio', 'vector', 'emotext'):
                p.indextts2_emotion_mode_buttons[mode].click()
                QTest.qWait(20)
                heights.append(p._approved_index.emotion_card.height())
                tops.append(p._approved_index.emotion_card.mapTo(p, QPoint()).y())
                self.assertEqual(p._indextts2_emotion_mode, mode)
                self.assertTrue(p.indextts2_emotion_mode_buttons[mode].isVisible())
                self.assertEqual(p.page_scroll.horizontalScrollBar().maximum(), 0)
            self.assertEqual(len(set(heights)), 1, (width, heights))
            self.assertEqual(len(set(tops)), 1, (width, tops))
        self.assertTrue(p.indextts2_emotion_mode_buttons['template'].isHidden())

    def test_custom_draft_survives_empty_and_loaded_source_switches(self):
        p = self.page
        p._src_set_mode('indextts2', 'custom')
        p.indextts2_text_edit.setPlainText('自定义草稿，不应丢失。')
        p._src_set_mode('indextts2', 'library')
        self.assertEqual(p.indextts2_text_edit.toPlainText(), '')
        p._src['indextts2']['upload_text'] = '上传文案'
        p._src_set_mode('indextts2', 'upload')
        self.assertEqual(p.indextts2_text_edit.toPlainText(), '上传文案')
        p._src_set_mode('indextts2', 'custom')
        self.assertEqual(p.indextts2_text_edit.toPlainText(), '自定义草稿，不应丢失。')

    def test_reading_button_stays_anchored_and_clear_cancel_preserves_text(self):
        p = self.page
        x = []
        for mode in ('custom', 'upload', 'library', 'custom'):
            p._src_set_mode('indextts2', mode)
            self.fixture.app.processEvents()
            x.append(p.btn_indextts2_glossary.mapTo(p, QPoint()).x())
        self.assertEqual(len(set(x)), 1)
        p.indextts2_text_edit.setPlainText('不要清空')
        with patch('src.ui.indextts25_workbench.dialogs.confirm', return_value=False):
            p._approved_index.clear.click()
        self.assertEqual(p.indextts2_text_edit.toPlainText(), '不要清空')

    def test_preview_requires_selection_and_retains_exact_text(self):
        p = self.page
        p._indextts2_status = fixture._ready_status()
        p._indextts2_voice_ready = lambda: True
        p.indextts2_text_edit.setPlainText('第一句。第二句。')
        p._update_indextts2_generate_state()
        self.assertFalse(p.btn_indextts2_preview.isEnabled())
        cursor = p.indextts2_text_edit.textCursor()
        cursor.setPosition(4)
        cursor.setPosition(8, cursor.MoveMode.KeepAnchor)
        p.indextts2_text_edit.setTextCursor(cursor)
        self.assertTrue(p.btn_indextts2_preview.isEnabled())
        self.assertEqual(p._indextts2_preview_text(), '第二句。')

    def test_language_speed_and_strength_mapping(self):
        p = self.page
        select = p._approved_index.language_select
        self.assertEqual(p.indextts2_language_combo.count(), 5)
        select.set_value('JA', emit=True)
        self.assertEqual(p.indextts2_language_combo.currentData(), 'JA')
        p.indextts2_language_combo.setCurrentIndex(1)
        self.assertEqual(select.currentData(), 'EN')
        for value in (0.5, 1.0, 2.0):
            p._approved_index.speed_value.setValue(value)
            self.assertEqual(p.indextts2_speed_slider.value(), round(value * 100))
        for value in (0.0, 0.8, 1.0):
            p._set_indextts2_advanced_weight(value)
            self.assertEqual(p._current_indextts2_advanced_weight(), value)

    def test_busy_cancel_remains_in_window_when_scrolled(self):
        p = self.page
        p._approved_index.sync_busy(True)
        p.btn_indextts2_cancel.setEnabled(True)
        p._approved_index.sync_busy(True)
        p.page_scroll.verticalScrollBar().setValue(p.page_scroll.verticalScrollBar().maximum())
        self.fixture.app.processEvents()
        button = p._approved_index.busy_cancel
        self.assertTrue(button.isVisible())
        self.assertTrue(p.rect().contains(button.mapTo(p, button.rect().bottomRight())))
        p._approved_index.sync_busy(False)
        self.assertFalse(button.isVisible())

    def test_waveform_decodes_real_file_and_invalid_file_never_gets_fake_peaks(self):
        path = self.fixture.root / 'wave.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(i * .2))) for i in range(8000)))
        slider = self.page._approved_index.ref_wave
        slider.load_file(str(path))
        self.fixture._wait_until(lambda: bool(slider.peaks), timeout=10)
        self.assertEqual(len(slider.peaks), 160)
        original = path.read_bytes()
        broken = self.fixture.root / 'bad.wav'
        broken.write_bytes(b'not audio')
        slider.load_file(str(broken))
        self.fixture._wait_until(lambda: slider._process is None, timeout=10)
        self.assertEqual(slider.peaks, [])
        self.assertEqual(path.read_bytes(), original)

    def test_random_option_reaches_worker_only_for_supported_modes(self):
        p = self.page
        p._indextts2_status = fixture._ready_status()
        p._indextts2_voice_ready = lambda: True
        p._indextts2_background_status_snapshot = lambda: {'alive': False}
        p.indextts2_text_edit.setPlainText('测试随机情绪。')
        p._approved_index.random_emotion = True
        with patch.object(fixture.voice_page_module, 'IndexTTS2SynthesisWorker', fixture._FakeIndexTTS2Worker):
            for mode, expected in (('follow', False), ('vector', True)):
                p._indextts2_worker = None
                p._set_indextts2_emotion_mode(mode)
                p._generate_indextts2()
                self.assertEqual(fixture._FakeIndexTTS2Worker.instances[-1].kwargs['random_emotion'], expected)
        p._indextts2_worker = None
