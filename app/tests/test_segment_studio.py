from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest

from tests import test_voice_page_model_shell as fixture
from tests.test_segment_project import FakeEngine, wav
from src.core.synthesis.base import VoiceProfile
from src.core.synthesis.segment_project import SegmentProject
from src.ui.segment_studio import SegmentStudio, ProjectWorker
from src.ui.voice_wave_slider import VoiceWaveSlider


class SegmentStudioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.VoicePageModelShellTests()
        self.fixture.setUp()
        self.page = fixture.VoicePage(fixture._Cfg(self.fixture.root), fixture._Main())
        ref = self.fixture.root / 'studio-ref.wav'
        wav(ref)
        self.project = SegmentProject.create(self.fixture.root, '第一段完整意思。\n\n第二段完整意思。',
            VoiceProfile('test', '测试音色', str(ref)), {'emotion_mode': 'follow'})
        self.engine = FakeEngine()
        self.dialog = SegmentStudio(self.page, project=self.project, engine_factory=lambda: self.engine)
        self.guard = patch.object(self.page, '_guard_local_synthesis_start', return_value=True)
        self.guard.start()
        self.dialog.show()
        self.fixture.app.processEvents()

    def tearDown(self):
        if self.dialog.worker and self.dialog.worker.isRunning():
            self.dialog.worker.request_cancel()
            self.dialog.worker.wait(5000)
        self.dialog.close()
        for wave in self.page.findChildren(VoiceWaveSlider):
            wave.stop_decode()
        self.page.close()
        self.guard.stop()
        self.fixture.tearDown()

    def wait_idle(self):
        self.fixture._wait_until(lambda: not self.dialog.busy(), timeout=8)
        self.fixture.app.processEvents()

    def test_generate_retry_choose_and_reopen_keep_other_paragraph(self):
        d = self.dialog
        d.generate_missing.click()
        self.wait_idle()
        data = self.project.read()
        self.assertTrue(all(self.project.current(p) for p in data['parts']))
        first, second = [p['selected'] for p in data['parts']]
        d.regenerate.click()
        self.wait_idle()
        self.assertEqual(self.project.read()['parts'][0]['selected'], first)
        self.assertEqual(self.project.read()['parts'][1]['selected'], second)
        self.assertEqual(d.versions.count(), 2)
        d.choose_button.click()
        self.assertNotEqual(self.project.read()['parts'][0]['selected'], first)
        d.versions.setCurrentIndex(d.versions.findData(first))
        d.choose_button.click()
        self.assertEqual(SegmentProject(self.project.path).read()['parts'][0]['selected'], first)
        self.assertTrue(d.export_button.isEnabled())

    def test_empty_edit_blocks_switch_instead_of_losing_text(self):
        d = self.dialog
        first = d._part_id
        d.editor.clear()
        d.parts.setCurrentRow(1)
        self.assertEqual(d._part_id, first)
        self.assertEqual(d.editor.toPlainText(), '')
        self.assertIn('没有文案', d.status.text())
        d.editor.setPlainText('补全这一段。')
        d.parts.setCurrentRow(1)
        self.assertEqual(self.project.read()['parts'][0]['text'], '补全这一段。')

    def test_small_window_actions_and_close_are_reachable(self):
        d = self.dialog
        d.resize(640, 620)
        QTest.qWait(30)
        for button in (d.regenerate, d.generate_missing, d.export_button, d.close_button):
            if button is not d.close_button:
                d.body_scroll.ensureWidgetVisible(button)
                self.fixture.app.processEvents()
            self.assertTrue(button.isVisible())
            self.assertGreaterEqual(button.mapTo(d, button.rect().bottomRight()).y(), 0)
            self.assertLessEqual(button.mapTo(d, button.rect().bottomRight()).y(), d.height())
        self.assertGreater(d.editor.width(), 180)
        for upper, lower in ((d.editor, d.edit_hint), (d.versions, d.version_text),
                             (d.version_text, d.play_button), (d.play_button, d.wave)):
            self.assertLess(upper.mapTo(d.body, upper.rect().bottomLeft()).y(),
                            lower.mapTo(d.body, lower.rect().topLeft()).y())

    def test_cancel_worker_before_start_never_generates(self):
        ids = [p['id'] for p in self.project.read()['parts']]
        worker = ProjectWorker(self.project, ids, engine_factory=lambda: self.engine)
        errors = []
        worker.failed.connect(errors.append)
        worker.request_cancel()
        worker.run()
        self.assertEqual(self.engine.calls, [])
        self.assertIn('保留', errors[0])

    def test_entry_is_available_only_for_25_and_not_during_generation(self):
        w = self.page._approved_index
        self.assertTrue(w.studio_button.isHidden())
        self.page._indextts2_last_output = str(self.project.file('references/voice.wav'))
        w.sync_adjustment_entry()
        self.assertFalse(w.studio_button.isHidden())
        w.sync_busy(True)
        self.assertFalse(w.studio_button.isEnabled())
        w.sync_busy(False)
        self.page._index_version = '2.0'
        w.sync_busy(False)
        self.assertTrue(w.studio_button.isHidden())

    def test_create_uses_visible_settings_and_reference_copy(self):
        d = self.dialog
        reference = self.fixture.root / 'create-ref.wav'
        wav(reference)
        voice = VoiceProfile('new', '创建测试', str(reference))
        d.draft.setPlainText('第一段。\n\n第二段。')
        with patch.object(self.page, '_selected_indextts2_voice', return_value=voice):
            d.create_project()
        data = d.project.read()
        self.assertEqual(data['voice']['name'], '创建测试')
        self.assertEqual(data['options']['emotion_mode'], 'follow')
        self.assertEqual(data['options']['generation_options']['temperature'], .8)
        self.assertEqual(len(data['parts']), 2)
        self.assertIn('项目已保存', d.status.text())

    def test_export_button_and_reopen_project_restore_selected_versions(self):
        d = self.dialog
        d.generate_missing.click()
        self.wait_idle()
        target = self.fixture.root / 'export-via-ui.wav'
        with patch('src.ui.segment_studio.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            d.export_button.click()
        self.wait_idle()
        self.assertTrue(target.is_file())
        self.assertTrue(d.export_play.isEnabled())
        self.assertIn('已导出', d.status.text())
        selected = [p['selected'] for p in self.project.read()['parts']]
        with patch('src.ui.segment_studio.QFileDialog.getOpenFileName', return_value=(str(self.project.path), '')):
            d.open_button.click()
        self.assertEqual([p['selected'] for p in d.project.read()['parts']], selected)

    def test_typing_autosaves_without_resetting_cursor(self):
        d = self.dialog
        d.editor.setPlainText('自动保存的修改。')
        d.editor.moveCursor(QTextCursor.End)
        position = d.editor.textCursor().position()
        QTest.qWait(650)
        self.assertEqual(self.project.read()['parts'][0]['text'], '自动保存的修改。')
        self.assertEqual(d.editor.textCursor().position(), position)

    def test_choose_restores_old_take_even_with_empty_draft(self):
        d = self.dialog
        d.regenerate.click()
        self.wait_idle()
        old = self.project.read()['parts'][0]['text']
        d.editor.clear()
        d.choose_button.click()
        self.assertEqual(d.editor.toPlainText(), old)
        self.assertTrue(self.project.current(self.project.read()['parts'][0]))

    def test_candidate_play_restores_candidate_source_after_export(self):
        d = self.dialog
        d.generate_missing.click()
        self.wait_idle()
        target = self.fixture.root / 'play-merged.wav'
        self.project.export(target)
        d.play_export()
        self.assertEqual(Path(d.player.source().toLocalFile()).resolve(), target.resolve())
        d.play()
        self.assertEqual(d.player.source().toLocalFile(), str(self.project.checked_audio(d.selected_version())))
        self.assertFalse(d._playing_export)

    def test_stale_version_choice_does_not_overwrite_external_edit(self):
        d = self.dialog
        d.regenerate.click()
        self.wait_idle()
        self.project.edit(d._part_id, '另一个窗口保存的新文案。')
        d.choose_button.click()
        self.assertIn('在别处更新', d.status.text())
        self.assertEqual(self.project.read()['parts'][0]['text'], '另一个窗口保存的新文案。')


if __name__ == '__main__':
    unittest.main()
