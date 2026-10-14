"""Version routing and two workspaces, with no model loading or real credentials."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtTest import QTest

from tests import test_voice_page_model_shell as fixture
from src.core.synthesis import indextts2_readiness as readiness
from src.core.synthesis import indextts2_subprocess as adapter
from src.core.synthesis import indextts2_worker as model_worker
from src.core.synthesis import glossary
from src.core.synthesis.base import SynthesisRequest, VoiceProfile, SynthesisError
from src.core.synthesis.indextts2_results import build_result_summary
from src.ui import worker as ui_worker


class DualVersionPageTests(unittest.TestCase):
    setUp = fixture.VoicePageModelShellTests.setUp
    tearDown = fixture.VoicePageModelShellTests.tearDown

    def page(self):
        page = fixture.VoicePage(fixture._Cfg(self.root), fixture._Main())
        self.addCleanup(page.close)
        self.addCleanup(page.shutdown_workers)
        return page

    def test_switch_keeps_shared_text_and_separates_settings_results(self):
        page = self.page()
        page._set_indextts2_text_source('custom')
        page.indextts2_text_edit.setPlainText('两个模型使用同一段文案。')
        page.indextts2_speed_slider.setValue(125)
        page.indextts2_emotion_text_edit.setPlainText('温柔')
        page._set_indextts2_emotion_mode('emotext')
        page._indextts2_results = [{'output_path':'/missing/version25.wav'}]
        page._switch_panel(3)
        self.assertEqual(page._index_version, '2.0')
        self.assertEqual(page._active_model_name(), 'IndexTTS 2')
        self.assertEqual(page._indextts2_emotion_mode, 'follow')
        self.assertEqual(page._indextts2_results, [])
        self.assertEqual(page._src['indextts2']['mode'], 'library')
        page._src['indextts2']['buttons']['custom'].click()
        self.assertEqual(page.indextts2_text_edit.toPlainText(), '两个模型使用同一段文案。')
        self.assertFalse(page._approved_index.sound_cells[2].isEnabled())
        self.assertTrue(page.btn_indextts2_glossary.isEnabled())
        page.indextts2_emotion_text_edit.setPlainText('开心')
        page._set_indextts2_emotion_mode('emotext')
        page._switch_panel(0)
        self.assertEqual(page.indextts2_speed_slider.value(), 125)
        self.assertEqual(page.indextts2_emotion_text_edit.toPlainText(), '温柔')
        self.assertEqual(len(page._indextts2_results), 1)
        self.assertTrue(page._approved_index.sound_cells[2].isEnabled())
        page._switch_panel(3)
        self.assertEqual(page.indextts2_emotion_text_edit.toPlainText(), '开心')

    def test_busy_or_checking_prevents_version_change(self):
        page = self.page()
        page._indextts2_status_checking = True
        page._switch_panel(3)
        self.assertEqual(page._index_version, '2.5')
        page._indextts2_status_checking = False
        page._indextts2_worker = SimpleNamespace(isRunning=lambda: True)
        page._switch_panel(3)
        self.assertEqual(page._index_version, '2.5')
        page._indextts2_worker = None

    def test_index2_glossary_dialog_and_generation_use_the_same_native_reading(self):
        page = self.page()
        page._switch_panel(3)
        self.assertTrue(page.btn_indextts2_glossary.isEnabled())
        glossary.upsert_term('落空', 'LUO4 KONG1', kind='pronunciation')
        text = '这次不会落空。'
        page.indextts2_text_edit.setPlainText(text)
        with patch.object(fixture.voice_page_module.QDialog, 'exec', return_value=1) as dialog:
            page.btn_indextts2_glossary.click()
            dialog.assert_called_once()
        panel = page.indextts2_glossary_panel
        self.assertIn('中文拼音', panel.kind_combo.itemText(1))
        self.assertEqual(panel.preview_text(text).text, '这次不会LUO4 KONG1。')
        page._indextts2_status = fixture._ready_status()
        page._indextts2_status.model_version = '2.0'
        page._indextts2_voice_ready = lambda: True
        page._indextts2_background_status_snapshot = lambda: {'alive': False}
        page._update_indextts2_generate_state()
        with patch.object(fixture.voice_page_module, 'IndexTTS2SynthesisWorker', fixture._FakeIndexTTS2Worker):
            page.btn_indextts2_generate.click()
            worker = fixture._FakeIndexTTS2Worker.instances[-1]
            self.assertEqual(worker.args[0], '这次不会LUO4 KONG1。')
            self.assertEqual(worker.kwargs['model_version'], '2.0')
        page._indextts2_worker = None
        self.assertEqual(page.indextts2_text_edit.toPlainText(), text)
        page._switch_panel(0)
        self.assertEqual(panel.preview_text(text).text, '这次不会<落空|LUO4 KONG1>。')

    def test_index2_hides_unsupported_sound_controls_across_resize_and_switch(self):
        page = self.page()
        page.show()
        page._switch_panel(3)
        workbench = page._approved_index
        for width in (1400, 668, 1400):
            page.resize(width, 900)
            QTest.qWait(50)
            self.assertTrue(workbench.sound_cells[1].isHidden())
            self.assertTrue(workbench.sound_cells[2].isHidden())
            self.assertTrue(workbench.sound_cells[0].isVisible())
            self.assertEqual(page.page_scroll.horizontalScrollBar().maximum(), 0)
        page._switch_panel(0)
        self.app.processEvents()
        self.assertTrue(workbench.sound_cells[1].isVisible())
        self.assertTrue(workbench.sound_cells[2].isVisible())

    def test_index2_rejects_incompatible_reading_without_changing_dictionary(self):
        page = self.page()
        page._switch_panel(3)
        panel = page.indextts2_glossary_panel
        panel.source_input.setText('hello')
        panel.replacement_input.setText('HH AH0 L OW1')
        panel.kind_combo.setCurrentIndex(1)
        before = glossary.load_store()
        with patch('src.ui.glossary_panel.dialogs.alert') as alert:
            panel.save_btn.click()
            alert.assert_called_once()
        self.assertEqual(glossary.load_store(), before)

    def test_index2_preview_and_full_generation_keep_identical_quality_options(self):
        page = self.page()
        page._switch_panel(3)
        page._indextts2_status = fixture._ready_status()
        page._indextts2_status.model_version = '2.0'
        page._indextts2_voice_ready = lambda: True
        page._indextts2_background_status_snapshot = lambda: {'alive': False}
        page._set_indextts2_text_source('custom')
        text = '第一句不落空。第二句保持自然。'
        page.indextts2_text_edit.setPlainText(text)
        glossary.upsert_term('落空', 'LUO4 KONG1', kind='pronunciation')
        page._approved_index.random_emotion = True
        page.indextts2_emotion_text_edit.setPlainText('开心')
        reference = self.root / 'emotion.wav'
        reference.touch()
        page._indextts2_emotion_audio_path = str(reference)
        with patch.object(fixture.voice_page_module, 'IndexTTS2SynthesisWorker', fixture._FakeIndexTTS2Worker):
            for mode in ('follow', 'vector', 'emotext', 'emoaudio'):
                with self.subTest(mode=mode):
                    page._set_indextts2_emotion_mode(mode)
                    cursor = page.indextts2_text_edit.textCursor()
                    cursor.setPosition(0)
                    page.indextts2_text_edit.setTextCursor(cursor)
                    self.assertFalse(page.btn_indextts2_preview.isEnabled())
                    cursor.setPosition(len('第一句不落空。'), cursor.MoveMode.KeepAnchor)
                    page.indextts2_text_edit.setTextCursor(cursor)
                    self.assertTrue(page.btn_indextts2_preview.isEnabled())
                    page.btn_indextts2_preview.click()
                    preview = fixture._FakeIndexTTS2Worker.instances[-1]
                    self.assertTrue(preview.kwargs['preview'])
                    self.assertEqual(preview.args[0], '第一句不LUO4 KONG1。')
                    page._indextts2_worker = None
                    page._update_indextts2_generate_state()
                    page.btn_indextts2_generate.click()
                    full = fixture._FakeIndexTTS2Worker.instances[-1]
                    self.assertEqual({k:v for k,v in preview.kwargs.items() if k != 'preview'},
                                     {k:v for k,v in full.kwargs.items() if k != 'preview'})
                    self.assertEqual(preview.args[3:11], full.args[3:11])
                    self.assertEqual(full.kwargs['random_emotion'], mode in ('vector', 'emotext'))
                    self.assertEqual(full.kwargs['model_version'], '2.0')
                    self.assertEqual(page.indextts2_text_edit.toPlainText(), text)
                    page._indextts2_worker = None

    def test_index2_preview_result_does_not_save_to_library(self):
        page = self.page()
        page._switch_panel(3)
        result = SimpleNamespace(output_path=self.root / 'preview.wav', duration_seconds=1.0,
                                 voice_id='', text='试听', meta={'preview': True, 'model_version': '2.0'})
        with patch.object(page, '_save_voice_safely') as save:
            page._on_indextts2_done(result)
            save.assert_not_called()
        self.assertIn('选段试听', page.indextts2_status.text())

    def test_wrong_version_is_not_ready(self):
        page = self.page()
        page._switch_panel(3)
        page._indextts2_status = fixture._ready_status()
        self.assertFalse(page._indextts2_environment_ready())
        self.assertIn('版本', page._indextts2_gate_reason())

    def test_version_2_cannot_sync_version_25_configuration(self):
        page = self.page()
        page._switch_panel(3)
        with patch.object(fixture.voice_page_module, 'IndexTTS2ConfigSyncWorker') as worker:
            page._sync_indextts2_config()
            worker.assert_not_called()


class DualVersionRoutingTests(unittest.TestCase):
    def test_version_2_never_receives_new_language_or_duration_parameters(self):
        tts = SimpleNamespace(model_version='2.0', device='cpu')
        with patch.object(model_worker, '_complete_infer') as infer:
            model_worker._safe_infer(tts, speech={'language':'JA', 'speed':1.5, 'random_emotion':True}, text='测试')
            self.assertEqual(infer.call_args.kwargs, {'text':'测试', 'use_random':True})

    def test_version_2_cpu_monitors_memory_without_quality_quota(self):
        with patch.object(adapter.sys, 'platform', 'darwin'), patch.object(adapter.subprocess, 'check_output', return_value=str(48 * 1024**3)):
            guard = adapter._ProcessMemoryMonitor(SimpleNamespace(pid=12345, poll=lambda:None), 'cpu', model_version='2.0')
        self.assertTrue(guard.enabled)
        self.assertNotIn('memory_limit_bytes', guard.annotate({})['meta'])

    def test_ui_worker_routes_each_version_without_loading_models(self):
        for version in ('2.0', '2.5'):
            with self.subTest(version=version), patch.object(ui_worker, 'get_voice', return_value=VoiceProfile(
                'voice', '声音', '/unused.wav')), patch.object(ui_worker, 'IndexTTS2SubprocessEngine') as engine:
                worker = ui_worker.IndexTTS2SynthesisWorker('测试', 'voice', '/unused-output.wav', 'natural', model_version=version)
                worker.run()
                request = engine.return_value.synthesize.call_args.args[0]
                self.assertEqual(request.engine_options['model_version'], version)
                self.assertEqual(request.engine_options['device'], 'cpu')

    def test_version_2_paths_and_both_receipts_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rt = root/'local_indextts2/runtime'
            md = root/'local_indextts2/weights'
            for file in (*[rt/x for x in readiness.RUNTIME_REQUIRED if x != 'python'],
                         rt/'python/bin/python3', *[md/x for x in readiness.MODEL_REQUIRED]):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('fixture')
            (md/'config.yaml').write_text('version: 2.0\n')
            with patch.object(readiness.rp,'tts_root',return_value=root), patch.object(readiness.rp,'_installed_runtime_manifest',return_value={'version':'fixture'}), patch.object(readiness,'install_receipt_valid',return_value=True) as receipt:
                status = readiness.check_runtime(expected_version='2.0')
                self.assertTrue(status.ready)
                self.assertEqual(status.runtime_dir, rt)
                self.assertEqual(status.model_version, '2.0')
                self.assertEqual([x.args[0] for x in receipt.call_args_list], ['tts-runtime:local_indextts2','tts-weights:local_indextts2'])
                receipt.return_value = False
                status = readiness.check_runtime(expected_version='2.0')
                self.assertFalse(status.ready)
                self.assertIn('runtime/install_receipt_invalid', status.missing_items)
                self.assertIn('model/install_receipt_invalid', status.missing_items)

    def test_model_mismatch_cannot_start_a_process(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp)/'reference.wav'; audio.touch()
            request = SynthesisRequest('测试', VoiceProfile('voice','声音',str(audio)), Path(temp)/'out.wav', engine_options={'model_version':'2.0'})
            with patch.object(adapter,'check_runtime',return_value=SimpleNamespace(model_version='2.5',ready=True)) as check, patch.object(adapter,'_start_worker') as start:
                with self.assertRaisesRegex(SynthesisError,'不一致'):
                    adapter.IndexTTS2SubprocessEngine().synthesize(request)
                check.assert_called_once_with(expected_version='2.0')
                start.assert_not_called()

    def test_version_2_result_never_claims_new_language_or_speed(self):
        result = build_result_summary(output_path='sample.wav',voice_label='我的音色',style_meta={},result_meta={'model_version':'2.0','emotion_control_mode':'follow'},duration_seconds=2,glossary_applied=False)
        self.assertEqual(result['model'],'IndexTTS 2')
        self.assertIn('原生节奏',result['summary'])
        self.assertNotIn('2.5',result['summary'])
