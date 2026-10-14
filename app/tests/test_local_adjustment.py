from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from src.core.synthesis.base import VoiceProfile
from src.core.synthesis.local_adjustment import LocalAdjustment
from src.core.synthesis.segment_project import ProjectCancelled, digest
from tests.test_segment_project import FakeEngine, wav


def pcm(path):
    with wave.open(str(path), 'rb') as f:
        return f.readframes(f.getnframes())


class LocalAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'finished.wav'
        # A varying signal makes wrong offsets and shifted tails observable.
        with wave.open(str(self.source), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            f.writeframes(b''.join(struct.pack('<h', (i % 5000) - 2500) for i in range(32000)))
        self.voice = VoiceProfile('test', '参考音色', str(self.source))
        self.project = LocalAdjustment.open_audio(self.root / 'work', self.source, self.voice, {'emotion_mode': 'follow'})
        self.project.set_selection(8000, 16000, '需要重新说的完整一句。')
        self.engine = FakeEngine()

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_replacement_keeps_prefix_suffix_and_original(self):
        original = self.source.read_bytes()
        version = self.project.generate_candidate(self.engine)
        self.assertIsNone(self.project.read()['parts'][0]['selected'])
        target = self.root / 'adopted.wav'
        result = self.project.adopt(target, version['id'])
        take = pcm(self.project.checked_audio(version))
        self.assertEqual(pcm(target), pcm(self.source)[:16000] + take + pcm(self.source)[32000:])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(result['duration'], (32000 - 8000 + len(take) // 2) / 8000)
        self.assertEqual(self.project.read()['adjustment']['last_export']['sha256'], digest(target))
        with self.assertRaisesRegex(ValueError, '新.*文件名'):
            self.project.adopt(target, version['id'])
        with self.assertRaises(ValueError):
            self.project.adopt(self.source, version['id'])

    def test_emotion_changes_reach_engine_and_old_candidate_restores_settings(self):
        first = self.project.generate_candidate(self.engine)
        for mode in ('emotext', 'vector', 'emoaudio', 'follow'):
            options = {'emotion_mode': mode, 'emotion_text': '开心又惊讶',
                       'emotion_vector': [.4, 0, 0, 0, 0, 0, .2, 0],
                       'emotion_audio_path': str(self.source), 'random_emotion': True}
            self.project.set_emotion(options)
            with self.assertRaisesRegex(ValueError, '情绪设置'):
                self.project.adopt(self.root / 'stale.wav', first['id'])
            take = self.project.generate_candidate(self.engine)
            actual = self.engine.calls[-1].engine_options
            self.assertEqual(actual['emotion_mode'], mode)
            self.assertEqual(actual['random_emotion'], mode in ('emotext', 'vector'))
            if mode == 'emoaudio':
                self.assertNotEqual(actual['emotion_audio_path'], str(self.source))
                self.assertEqual(Path(actual['emotion_audio_path']).read_bytes(), self.source.read_bytes())
            self.project.restore_candidate(first['id'])
            self.assertEqual(self.project.read()['options'], first['options'])
            self.assertTrue(self.project.settings_match(first))
            self.project.restore_candidate(take['id'])
            self.assertEqual(self.project.read()['options']['emotion_mode'], mode)
        self.project.restore_candidate(first['id'])
        self.project.adopt(self.root / 'restored-emotion.wav', first['id'])

    def test_invalid_emotion_and_legacy_restore_preserve_project(self):
        first = self.project.generate_candidate(self.engine)
        data = self.project.read()
        for v in data['parts'][0]['versions']:
            v.pop('options')
            v.pop('references')
        self.project.save(data, data['revision'])
        before = self.project.path.read_bytes()
        for options in ({'emotion_mode': 'bogus'}, {'emotion_mode': 'emotext'},
                        {'emotion_mode': 'vector', 'emotion_vector': [1]*8},
                        {'emotion_mode': 'emoaudio', 'emotion_audio_path': 'missing.wav'}):
            with self.assertRaises(ValueError):
                self.project.set_emotion(options)
            self.assertEqual(before, self.project.path.read_bytes())
        self.project.set_emotion({'emotion_mode': 'emotext', 'emotion_text': '轻松聊天'})
        self.project.restore_candidate(first['id'])
        self.assertEqual(self.project.read()['options']['emotion_mode'], 'follow')

    def test_range_and_text_changes_do_not_reuse_stale_candidate(self):
        version = self.project.generate_candidate(self.engine)
        self.project.set_selection(7000, 16000, version['text'])
        with self.assertRaisesRegex(ValueError, '范围或文案'):
            self.project.adopt(self.root / 'stale.wav', version['id'])
        self.project.set_selection(8000, 16000, '不同的文字。')
        with self.assertRaises(ValueError):
            self.project.adopt(self.root / 'stale.wav', version['id'])
        self.project.set_selection(8000, 16000, version['text'])
        self.project.adopt(self.root / 'restored.wav', version['id'])

    def test_preview_uses_same_candidate_boundaries_and_one_second_context(self):
        v = self.project.generate_candidate(self.engine)
        old = self.root / 'old.wav'
        new = self.root / 'new.wav'
        self.project.render(old, start=8000, end=16000, context=True)
        self.project.render(new, version_id=v['id'], context=True)
        self.assertEqual(pcm(old), pcm(self.source)[:48000])
        self.assertEqual(pcm(new), pcm(self.source)[:16000] + pcm(self.project.checked_audio(v)) + pcm(self.source)[32000:48000])
        selected = self.root / 'selected.wav'
        self.project.render(selected, start=8000, end=16000)
        self.assertEqual(pcm(selected), pcm(self.source)[16000:32000])

    def test_reopen_keeps_reference_candidates_and_settings(self):
        v = self.project.generate_candidate(self.engine)
        reopened = LocalAdjustment.open_audio(self.root / 'work', self.source, None, {})
        self.assertEqual(reopened.path, self.project.path)
        self.assertEqual(reopened.candidate(v['id'])['text'], v['text'])
        self.assertEqual(reopened.read()['options']['emotion_mode'], 'follow')
        self.source.unlink()
        reopened.generate_candidate(self.engine)
        self.assertTrue(reopened.source_audio().is_file())

    def test_cancel_fail_tamper_and_format_mismatch_preserve_files(self):
        v = self.project.generate_candidate(self.engine)
        before = self.project.path.read_bytes()
        with self.assertRaises(ProjectCancelled):
            self.project.generate_candidate(self.engine, cancelled=lambda: True)
        self.engine.fail = True
        with self.assertRaises(RuntimeError):
            self.project.generate_candidate(self.engine)
        self.assertEqual(before, self.project.path.read_bytes())
        target = self.root / 'cancel.wav'
        with self.assertRaises(ProjectCancelled):
            self.project.adopt(target, v['id'], cancelled=lambda: True)
        self.assertFalse(target.exists())
        wav(self.project.checked_audio(v), rate=24000)
        with self.assertRaisesRegex(ValueError, '改动'):
            self.project.adopt(target, v['id'])
        self.assertFalse(target.exists())
        self.assertEqual(before, self.project.path.read_bytes())

    def test_invalid_range_stale_revision_and_source_mutation_rejected(self):
        for left, right in ((0, 0), (8000, 7000), (-1, 9000), (0, 32001), (1.2, 20)):
            with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                self.project.set_selection(left, right, '文字。')
        with self.assertRaisesRegex(ValueError, '别处更新'):
            self.project.set_selection(0, 1000, '文字。', 0)
        wav(self.project.source_audio())
        with self.assertRaisesRegex(ValueError, '改动'):
            self.project.generate_candidate(self.engine)
        self.assertFalse(self.engine.calls)

    def test_format_conversion_preserves_original_and_mid_copy_cancel_leaves_no_output(self):
        from types import SimpleNamespace
        class DifferentRate:
            def synthesize(self, request, *_args):
                wav(request.output_path, rate=22050)
                return SimpleNamespace(wav_path=request.output_path, meta={})
        version = self.project.generate_candidate(DifferentRate())
        target = self.root / 'different.wav'
        self.project.adopt(target, version['id'])
        with wave.open(str(target), 'rb') as f:
            self.assertEqual(f.getframerate(), 8000)
            self.assertEqual(f.getnchannels(), 1)
        self.assertEqual(pcm(target)[:16000], pcm(self.source)[:16000])
        self.assertEqual(pcm(target)[-32000:], pcm(self.source)[32000:])
        version = self.project.generate_candidate(self.engine)
        target = self.root / 'cancelled.wav'
        steps = []
        def cancel_during_copy():
            steps.append(1)
            return len(steps) >= 2
        with self.assertRaises(ProjectCancelled):
            self.project.adopt(target, version['id'], cancelled=cancel_during_copy)
        self.assertFalse(target.exists())
        self.assertTrue(self.project.checked_audio(version).exists())

    def test_empty_draft_recovers_but_cannot_be_generated(self):
        self.project.set_selection(8000, 16000, '')
        self.assertEqual(LocalAdjustment(self.project.path).read()['adjustment']['draft'], '')
        with self.assertRaises(ValueError):
            self.project.generate_candidate(self.engine)
        self.assertFalse(self.engine.calls)


from tests import test_voice_page_model_shell as fixture
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint, Qt
from src.ui.local_adjustment import LocalAdjustmentDialog
from src.ui.voice_wave_slider import VoiceWaveSlider


class LocalAdjustmentDialogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.VoicePageModelShellTests()
        self.fixture.setUp()
        self.page = fixture.VoicePage(fixture._Cfg(self.fixture.root), fixture._Main())
        source = self.fixture.root / 'finished.wav'
        wav(source, frames=32000)
        self.project = LocalAdjustment.open_audio(self.fixture.root / 'work', source,
            VoiceProfile('v', '测试音色', str(source)), {'emotion_mode': 'follow'})
        self.engine = FakeEngine()
        self.dialog = LocalAdjustmentDialog(self.page, project=self.project, engine_factory=lambda: self.engine)
        self.guard = patch.object(self.page, '_guard_local_synthesis_start', return_value=True)
        self.guard.start()
        self.dialog.show()
        QTest.qWait(20)

    def tearDown(self):
        if self.dialog.worker:
            self.dialog.worker.cancelled = True
            self.fixture._wait_until(lambda: not self.dialog.busy(), timeout=10)
        self.dialog.editor.clear()
        self.dialog.close()
        for w in self.page.findChildren(VoiceWaveSlider):
            w.stop_decode()
        self.page.close()
        self.page.shutdown_workers()
        self.guard.stop()
        self.fixture.tearDown()

    def idle(self):
        self.fixture._wait_until(lambda: not self.dialog.busy(), timeout=10)

    def test_finished_first_generate_compare_restore_and_adopt(self):
        d = self.dialog
        self.assertFalse(d.generate.isEnabled())
        self.assertFalse(d.adopt_button.isEnabled())
        d.start.setValue(1)
        d.end.setValue(2)
        d.editor.setPlainText('新的一句话。')
        d.generate.click()
        self.idle()
        self.assertEqual(len(self.engine.calls), 1)
        self.assertTrue(d.compare_new.isEnabled())
        self.assertTrue(d.adopt_button.isEnabled())
        d.end.setValue(3)
        self.assertFalse(d.adopt_button.isEnabled())
        d.select_version()
        self.assertEqual(d.end.value(), 2)
        self.assertTrue(d.adopt_button.isEnabled())
        d.generate.click()
        self.idle()
        self.assertEqual(d.versions.count(), 2)
        d.versions.setCurrentIndex(0)
        target = fixture.voice_page_module.library.audio_storage_dir() / 'finished_局部调整.wav'
        receipts = []
        d.saved.connect(receipts.append)
        with patch('src.ui.local_adjustment.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            d.adopt_button.click()
        self.idle()
        self.assertTrue(target.is_file())
        self.assertEqual(receipts[0]['path'], str(target.resolve()))
        self.assertEqual(len(self.project.read()['parts'][0]['versions']), 2)

    def test_local_emotion_editor_is_independent_and_modes_are_effective(self):
        from src.ui.local_emotion import LocalEmotionDialog
        d = LocalEmotionDialog(self.dialog, self.project.read()['options'])
        d.show()
        before = self.page._indextts2_emotion_mode
        for mode in ('follow', 'emoaudio', 'vector', 'emotext'):
            d.set_mode(mode)
            self.assertEqual(d.freedom.isHidden(), mode not in ('vector', 'emotext'))
        d.text.setPlainText('轻松又开心')
        self.assertEqual(d.values()['emotion_text'], '轻松又开心')
        self.assertEqual(self.page._indextts2_emotion_mode, before)
        d.close()
        d.deleteLater()

    def test_no_audio_no_adjustment_entry_and_25_only(self):
        w = self.page._approved_index
        self.assertTrue(w.studio_button.isHidden())
        self.assertFalse(hasattr(w, 'open_finished_button'))
        self.page._indextts2_last_output = str(self.project.source_audio())
        w.sync_adjustment_entry()
        self.assertFalse(w.studio_button.isHidden())
        w.sync_busy(True)
        self.assertFalse(w.studio_button.isEnabled())
        self.page._switch_panel(3)
        self.assertTrue(w.studio_button.isHidden())

    def test_narrow_dialog_footer_and_controls_reachable(self):
        d = self.dialog
        d.resize(640, 600)
        QTest.qWait(80)
        from PySide6.QtWidgets import QScrollArea
        scroll = d.findChild(QScrollArea)
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertTrue(d.close_button.isVisible())
        self.assertTrue(d.adopt_button.isHidden())
        d.editor.setPlainText('新的一句话。')
        d.generate.click()
        self.idle()
        self.assertTrue(d.adopt_button.isVisible())
        self.assertLess(d.adopt_button.mapTo(d, d.adopt_button.rect().bottomRight()).y(), d.height())

    def test_drag_select_resize_and_click_seek_are_distinct(self):
        d = self.dialog
        self.assertTrue(d.precise.isHidden())
        d.start.setValue(1)
        d.end.setValue(2)
        w = d.wave
        left = QPoint(round(w._x(2.5 * 1000)), 53)
        right = QPoint(round(w._x(3.5 * 1000)), 53)
        QTest.mousePress(w, Qt.LeftButton, pos=left)
        QTest.mouseMove(w, right)
        QTest.mouseRelease(w, Qt.LeftButton, pos=right)
        self.assertAlmostEqual(d.start.value(), 2.5, delta=.03)
        self.assertAlmostEqual(d.end.value(), 3.5, delta=.03)
        original_end = d.end.value()
        handle = QPoint(round(w._x(w.selection[0])), 53)
        target = QPoint(round(w._x(2000)), 53)
        QTest.mousePress(w, Qt.LeftButton, pos=handle)
        QTest.mouseMove(w, target)
        QTest.mouseRelease(w, Qt.LeftButton, pos=target)
        self.assertAlmostEqual(d.start.value(), 2, delta=.03)
        self.assertEqual(d.end.value(), original_end)
        before = w.selection
        QTest.mouseClick(w, Qt.LeftButton, pos=QPoint(round(w._x(1000)), 53))
        self.assertEqual(w.selection, before)
        self.assertAlmostEqual(w.value(), 1000, delta=30)
        d.precise_toggle.click()
        self.assertTrue(d.precise.isVisible())

    def test_matching_candidate_restores_without_being_adopted(self):
        d = self.dialog
        d.editor.setPlainText('新的一句话。')
        d.generate.click()
        self.idle()
        chosen = d.versions.currentData()
        d.load_project()
        self.assertEqual(d.versions.currentData(), chosen)
        self.assertTrue(d.compare_new.isEnabled())
        self.assertIsNone(self.project.read()['parts'][0]['selected'])
