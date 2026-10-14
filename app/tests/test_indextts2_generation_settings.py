"""Isolated settings, request transport and PCM boundaries; no real model."""
import tempfile
import json
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_voice_page_model_shell as fixture
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from src.core.synthesis import indextts2_worker as native
from src.core.synthesis.indextts2_subprocess import _build_payload
from src.core.synthesis.base import SynthesisRequest, VoiceProfile
from src.ui import indextts2_settings as ui
from src.ui import worker as ui_worker


class GenerationOptionsTests(unittest.TestCase):
    def test_invalid_requests_stop_before_runtime_or_output_access(self):
        payload = {'model_version': '2.0', 'speech': {'generation': {'num_beams': 99}}}
        with tempfile.TemporaryDirectory() as temp, patch.object(native, '_ensure_runtime_paths') as paths, \
             patch.object(native, '_emit') as emit:
            self.assertEqual(native._PersistentServer().handle_generate(payload), 1)
            request = Path(temp) / 'request.json'
            request.write_text(json.dumps(payload))
            self.assertEqual(native.run_request(request), 1)
            paths.assert_not_called()
            self.assertTrue(all(call.args[0]['code'] == 'INVALID_REQUEST' for call in emit.call_args_list))

    def test_preferences_round_trip_and_default_instances_are_independent(self):
        from src import config
        with tempfile.TemporaryDirectory() as temp, patch.object(config, '_CONFIG_PATH', Path(temp) / 'config.json'):
            first, second = config.AppConfig(), config.AppConfig()
            first.indextts2_generation_options['interval_silence'] = 500
            first.indextts25_generation_options['interval_silence'] = 300
            self.assertEqual(second.indextts25_generation_options, {})
            self.assertEqual(second.indextts2_generation_options, {})
            self.assertTrue(first.save())
            self.assertEqual(config.AppConfig.load().indextts2_generation_options, {'interval_silence': 500})
            self.assertEqual(config.AppConfig.load().indextts25_generation_options, {'interval_silence': 300})

    def test_sparse_defaults_and_invalid_options(self):
        self.assertEqual(native.validate_index2_generation({}), {})
        defaults = {key: limits[0] for key, limits in native.INDEX2_GENERATION_FIELDS.items()}
        self.assertEqual(native.validate_index2_generation(defaults), defaults)
        for invalid in ([1], {'do_sample': False}, {'top_p': float('nan')},
                        {'temperature': float('inf')}, {'num_beams': True},
                        {'num_beams': 1.5}, {'interval_silence': -1}, {'max_mel_tokens': 1816}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                native.validate_index2_generation(invalid)

    def test_full_transport_and_version_isolation(self):
        changes = {key: bounds[1] for key, bounds in native.INDEX2_GENERATION_FIELDS.items()}
        for version in ('2.0', '2.5'):
            voice = VoiceProfile('v', 'V', '/unused.wav')
            with patch.object(ui_worker, 'get_voice', return_value=voice), \
                 patch.object(ui_worker, 'IndexTTS2SubprocessEngine') as engine, \
                 patch.object(ui_worker, 'persistent_worker_status', return_value={}):
                job = ui_worker.IndexTTS2SynthesisWorker('测试', 'v', '/unused-out.wav', 'natural',
                    model_version=version, generation_options=changes)
                job.run()
                request = engine.return_value.synthesize.call_args.args[0]
            status = SimpleNamespace(runtime_dir=Path('/unused-runtime'), model_dir=Path('/unused-model'), model_version=version, snapshot=lambda: {})
            payload = _build_payload(request, status, Path('/unused-staging.wav'), 'job')
            tts = SimpleNamespace(model_version=version, device='cpu')
            with patch.object(native, '_complete_infer') as infer:
                native._safe_infer(tts, speech=payload['speech'], text='测试')
            if version == '2.0':
                self.assertEqual(payload['speech']['generation'], changes)
                self.assertEqual(infer.call_args.kwargs, dict(text='测试', **changes))
            else:
                self.assertEqual(payload['speech']['generation'], changes)
                self.assertEqual(infer.call_args.kwargs, dict(text='测试', lang='ZH', duration_factor=1.0, **changes))

    def test_official_numeric_boundaries_reach_both_engines(self):
        for version in ('2.0', '2.5'):
            options = {'top_p': 0.0, 'max_text_tokens_per_segment': 600}
            tts = SimpleNamespace(model_version=version, device='cpu')
            with patch.object(native, '_complete_infer') as infer:
                native._safe_infer(tts, speech={'generation': options}, text='原文')
                self.assertEqual(infer.call_args.kwargs['top_p'], 0.0)
                self.assertEqual(infer.call_args.kwargs['max_text_tokens_per_segment'], 600)
            for invalid in ({'top_p': -0.01}, {'max_text_tokens_per_segment': 601}):
                with self.assertRaises(ValueError):
                    native.validate_index_generation(invalid, version)

    def test_25_defaults_and_invalid_settings_rejected_before_runtime(self):
        tts = SimpleNamespace(model_version='2.5', device='cpu')
        with patch.object(native, '_complete_infer') as infer:
            native._safe_infer(tts, speech={'generation': {}}, text='原文')
            self.assertEqual(infer.call_args.kwargs, dict(text='原文', lang='ZH', duration_factor=1.0))
        for invalid in ({'num_beams': 0}, {'temperature': float('nan')}, {'do_sample': False}):
            payload = {'model_version': '2.5', 'job_id': 'bad', 'speech': {'generation': invalid}}
            with tempfile.TemporaryDirectory() as temp, patch.object(native, '_ensure_runtime_paths') as paths, patch.object(native, '_emit') as emit:
                request = Path(temp) / 'request.json'
                request.write_text(json.dumps(payload))
                self.assertEqual(native._PersistentServer().handle_generate(payload), 1)
                self.assertEqual(native.run_request(request), 1)
                paths.assert_not_called()
                self.assertTrue(all(call.args[0]['code'] == 'INVALID_REQUEST' for call in emit.call_args_list))

    def test_default_infer_unchanged_and_mps_not_silently_overridden(self):
        tts = SimpleNamespace(model_version='2.0', device='cpu')
        with patch.object(native, '_complete_infer') as infer:
            native._safe_infer(tts, speech={'generation': {}}, text='原文')
            self.assertEqual(infer.call_args.kwargs, {'text': '原文'})
        tts.device = 'mps'
        with self.assertRaisesRegex(ValueError, 'CPU'):
            native._safe_infer(tts, speech={'generation': {'num_beams': 4}}, text='原文')

    def test_explicit_gap_zero_and_pcm_duration_without_trailing_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parts = [root / f'{i}.wav' for i in range(3)]
            for part in parts:
                with wave.open(str(part), 'wb') as output:
                    output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                    output.writeframes(b'\x01\x01' * 2205)
            for milliseconds in (0, 200, 650, 2000):
                gap = native._segment_gap('2.0', {'generation': {'interval_silence': milliseconds}})
                duration, rate = native._stitch_wavs(parts, root / 'joined.wav', gap)
                self.assertAlmostEqual(duration, .3 + 2 * int(22050 * gap) / 22050)
                self.assertEqual(rate, 22050)
                with wave.open(str(root / 'joined.wav'), 'rb') as stream:
                    self.assertTrue(stream.readframes(stream.getnframes()).endswith(b'\x01\x01'))
        self.assertEqual(native._segment_gap('2.0', {}), .16)
        self.assertEqual(native._segment_gap('2.5', {'generation': {'interval_silence': 650}}), .65)
        self.assertEqual(native._segment_gap('2.5', {}), .16)

    def test_one_shot_segmented_path_uses_same_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tts = SimpleNamespace(model_version='2.0', device='cpu')
            def infer(tts, **options):
                self.assertEqual(options['speech']['generation']['interval_silence'], 600)
                with wave.open(str(options['output_path']), 'wb') as output:
                    output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                    output.writeframes(b'\x01\x01' * 2205)
            with patch.object(native, '_create_tts', return_value=tts), patch.object(native, '_safe_infer', infer), \
                 patch.object(native, '_emit'), patch.object(native.sys, 'path', list(native.sys.path)):
                exit_code = native._run_segmented_in_process(job_id='test', started=0, vendor_dir=root,
                    model_dir=root, reference_audio=root/'unused.wav', temp_wav=root/'joined.wav',
                    text_segments=['第一句。', '第二句。'], device='cpu', emotion_path=None,
                    emotion_text=None, emotion_vector=None, emotion_weight=.8, emotion={},
                    speech={'generation': {'interval_silence': 600}}, model_version='2.0')
            self.assertEqual(exit_code, 0)
            self.assertAlmostEqual(native._duration_and_rate(root/'joined.wav')[0], .8)


class SettingsDialogTests(unittest.TestCase):
    setUp = fixture.VoicePageModelShellTests.setUp
    tearDown = fixture.VoicePageModelShellTests.tearDown

    def page(self):
        page = fixture.VoicePage(fixture._Cfg(self.root), fixture._Main())
        self.addCleanup(page.close)
        self.addCleanup(page.shutdown_workers)
        page._switch_panel(3)
        return page

    def dialog(self, **kwargs):
        page = self.page()
        with patch.object(ui, 'reduced_motion', return_value=False):
            dialog = ui.IndexTTS2SettingsDialog(page, {}, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_advanced_content_and_narrow_dialog(self):
        dialog = self.dialog(animate=False)
        dialog.resize(560, 520)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog.advanced.isVisible())
        self.assertTrue(dialog.custom_gap.isVisible())
        self.assertFalse(hasattr(dialog, 'random'))
        self.assertEqual(len(dialog.controls), 8)
        self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)
        self.assertTrue(dialog.apply_button.isVisible())
        self.assertLess(dialog.apply_button.geometry().bottom(), dialog.height())
        self.assertEqual(dialog.values(), {})
        dialog.reject()

    def test_save_reset_cancel_and_failure_preserve_emotion_and_other_version(self):
        page = self.page()
        page.cfg.save = Mock(return_value=True)
        for panel, key, other in ((3, 'indextts2_generation_options', 'indextts25_generation_options'),
                                  (0, 'indextts25_generation_options', 'indextts2_generation_options')):
            page._switch_panel(panel)
            before = {'temperature': .7, 'interval_silence': 600}
            setattr(page.cfg, key, dict(before))
            setattr(page.cfg, other, {'top_k': 20})
            page._approved_index.random_emotion = True
            def reset(dialog):
                dialog.restore_defaults()
                dialog.apply_button.click()
                return dialog.result()
            with patch.object(ui.IndexTTS2SettingsDialog, 'exec', reset):
                page._approved_index._generation_settings()
            self.assertEqual(getattr(page.cfg, key), {})
            self.assertEqual(getattr(page.cfg, other), {'top_k': 20})
            self.assertTrue(page._approved_index.random_emotion)
            def cancel(dialog):
                dialog.custom_gap.setChecked(True)
                dialog.gap.setValue(.9)
                dialog.controls['temperature'].setValue(1.1)
                dialog.reject()
                return dialog.result()
            with patch.object(ui.IndexTTS2SettingsDialog, 'exec', cancel):
                page._approved_index._generation_settings()
            self.assertEqual(getattr(page.cfg, key), {})
            setattr(page.cfg, key, before)
            page.cfg.save.return_value = False
            def fail(dialog):
                dialog.restore_defaults()
                dialog.apply_button.click()
                self.assertIn('未能保存', dialog.message.text())
                dialog.reject()
                return dialog.result()
            with patch.object(ui.IndexTTS2SettingsDialog, 'exec', fail):
                page._approved_index._generation_settings()
            self.assertEqual(getattr(page.cfg, key), before)
            self.assertTrue(page._approved_index.random_emotion)
            page.cfg.save.return_value = True

    def test_gap_zero_and_reset(self):
        d = ui.IndexTTS2SettingsDialog(self.page(), {'temperature': .7, 'interval_silence': 600}, animate=False)
        self.addCleanup(d.deleteLater)
        d.gap.setValue(0)
        self.assertEqual(d.values(), {'temperature': .7, 'interval_silence': 0})
        d.restore_defaults()
        self.assertEqual(d.values(), {})
        self.assertFalse(d.custom_gap.isChecked())
        self.assertFalse(d.gap.isEnabled())

    def test_number_fields_manual_entry_steps_bounds_and_disabled_state(self):
        d = self.dialog(animate=False)
        spin = d.controls['temperature']
        field = spin.parentWidget()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), '1.2')
        spin.interpretText()
        self.assertEqual(d.values(), {'temperature': 1.2})
        field.plus.click()
        self.assertAlmostEqual(spin.value(), 1.3)
        field.minus.click()
        self.assertAlmostEqual(spin.value(), 1.2)
        spin.setValue(spin.maximum())
        self.assertFalse(field.plus.isEnabled())
        field.plus.click()
        self.assertEqual(spin.value(), spin.maximum())
        spin.setValue(spin.minimum())
        self.assertFalse(field.minus.isEnabled())
        length = d.controls['length_penalty']
        d.controls['num_beams'].setValue(1)
        self.assertFalse(length.isEnabled())
        self.assertFalse(length.parentWidget().plus.isEnabled())
        self.assertFalse(length.parentWidget().minus.isEnabled())
        length.parentWidget().plus.click()
        self.assertEqual(length.value(), 0)
        d.controls['num_beams'].setValue(3)
        self.assertTrue(length.parentWidget().plus.isEnabled())
        self.assertFalse(d.gap.parentWidget().plus.isEnabled())
        d.custom_gap.click()
        self.assertTrue(d.gap.parentWidget().plus.isEnabled())
        d.restore_defaults()
        self.assertFalse(d.gap.parentWidget().plus.isEnabled())
        self.assertEqual(d.values(), {})

    def test_invalid_settings_show_repair_and_gap_marks_button_changed(self):
        page = self.page()
        d = ui.IndexTTS2SettingsDialog(page, {'temperature': 'broken'}, animate=False)
        self.addCleanup(d.deleteLater)
        self.assertIn('无效', d.message.text())
        self.assertEqual(d.values(), {})
        w = page._approved_index
        for values, state in (({}, '默认'), ({'interval_silence': 0}, '已调整'), ({'temperature': 'broken'}, '需检查')):
            page.cfg.indextts2_generation_options = values
            w.sync_settings()
            self.assertIn(state, w.advanced_settings_button.text())

    def test_emotion_dialog_apply_cancel_and_version_restore(self):
        page = self.page()
        page.show()
        w = page._approved_index
        self.assertFalse(w.random_emotion)
        self.assertFalse(hasattr(w, 'emotion_variation'))
        for mode in ('vector', 'emotext', 'emoaudio', 'follow'):
            page._set_indextts2_emotion_mode(mode)
            self.assertEqual(w.emotion_settings_button.isVisible(), mode in ('vector', 'emotext'))
            d = ui.IndexTTSEmotionDialog(page, mode=mode)
            self.addCleanup(d.deleteLater)
            active = mode in ('vector', 'emotext')
            self.assertEqual(d.windowTitle(), '情绪自由')
            self.assertEqual('后可设置' in d.mode_note.text(), not active)
            self.assertEqual(d.apply_button.isEnabled(), active)
            self.assertEqual(d.enabled_choice.isEnabled(), active)
            self.assertTrue(d.disabled_choice.isChecked())
            if not active:
                d.enabled_choice.click()
                d.apply_button.click()
                self.assertFalse(d.enabled_choice.isChecked())
                self.assertEqual(d.result(), QDialog.Rejected)
        page._set_indextts2_emotion_mode('vector')
        page.indextts2_advanced_sliders[0].setValue(30)
        before = [s.value() for s in page.indextts2_advanced_sliders]
        def choose(dialog):
            dialog.enabled_choice.click()
            self.assertFalse(dialog.disabled_choice.isChecked())
            dialog.apply_button.click()
            return dialog.result()
        with patch.object(ui.IndexTTSEmotionDialog, 'exec', choose):
            w._emotion_settings()
        self.assertTrue(w.random_emotion)
        self.assertIn('开启', w.emotion_settings_button.text())
        def cancel(dialog):
            dialog.disabled_choice.click()
            QTest.keyClick(dialog, Qt.Key_Escape)
            return dialog.result()
        with patch.object(ui.IndexTTSEmotionDialog, 'exec', cancel):
            w._emotion_settings()
        self.assertTrue(w.random_emotion)
        self.assertEqual([s.value() for s in page.indextts2_advanced_sliders], before)
        page._set_indextts2_emotion_mode('follow')
        self.assertTrue(w.emotion_settings_button.isHidden())
        page._switch_panel(0)
        self.assertFalse(w.random_emotion)
        self.assertTrue(w.emotion_settings_button.isHidden())
        page._switch_panel(3)
        self.assertTrue(w.random_emotion)
        self.assertTrue(w.emotion_settings_button.isHidden())
        self.assertEqual([s.value() for s in page.indextts2_advanced_sliders], before)
        page._set_indextts2_emotion_mode('vector')
        self.assertTrue(w.emotion_settings_button.isEnabled())
        self.assertIn('开启', w.emotion_settings_button.text())

    def test_emotion_entry_follows_mode_and_busy_in_both_versions(self):
        page = self.page()
        w = page._approved_index
        for panel in (3, 0):
            page._switch_panel(panel)
            for mode in ('follow', 'vector', 'emoaudio', 'emotext'):
                page._set_indextts2_emotion_mode(mode)
                active = mode in ('vector', 'emotext')
                self.assertEqual(w.emotion_settings_button.isEnabled(), active)
                self.assertEqual(w.emotion_settings_button.isHidden(), not active)
                if not active:
                    with patch.object(ui.IndexTTSEmotionDialog, 'exec') as execute:
                        w.emotion_settings_button.click()
                        w._emotion_settings()
                        execute.assert_not_called()
                w.sync_busy(True)
                w.sync_emotion_settings()
                self.assertFalse(w.emotion_settings_button.isEnabled())
                w.sync_busy(False)
                self.assertEqual(w.emotion_settings_button.isEnabled(), active)
                self.assertEqual(w.emotion_settings_button.isHidden(), not active)
            w.sync_busy(True)
            page._set_indextts2_emotion_mode('follow')
            page._set_indextts2_emotion_mode('vector')
            self.assertFalse(w.emotion_settings_button.isEnabled())
            w.sync_busy(False)
            self.assertTrue(w.emotion_settings_button.isEnabled())

    def test_toolbar_order_and_busy_blocks_settings(self):
        from PySide6.QtCore import QPoint
        page = self.page()
        page._set_indextts2_emotion_mode('vector')
        page.show()
        for width in (1400, 1000, 700):
            page.resize(width, 900)
            # Resize queues the responsive reflow, which then queues layout work.
            QTest.qWait(50)
            w = page._approved_index
            directory = page.btn_indextts2_output_dir.mapTo(w, QPoint())
            emotion = w.emotion_settings_button.mapTo(w, QPoint())
            self.assertEqual(directory.y(), emotion.y())
            self.assertGreater(emotion.x(), directory.x() + page.btn_indextts2_output_dir.width())
            self.assertEqual(page.page_scroll.horizontalScrollBar().maximum(), 0)
            buttons = (w.advanced_settings_button, page.btn_indextts2_preview, page.btn_indextts2_generate)
            positions = [button.mapTo(w, QPoint()) for button in buttons]
            self.assertEqual(len({point.y() for point in positions}), 1)
            self.assertEqual([point.x() for point in positions], sorted(point.x() for point in positions))
        w.sync_busy(True)
        self.assertFalse(w.emotion_settings_button.isEnabled())
        self.assertFalse(w.advanced_settings_button.isEnabled())
        with patch.object(ui.IndexTTS2SettingsDialog, 'exec') as execute:
            w._generation_settings()
            execute.assert_not_called()
        with patch.object(ui.IndexTTSEmotionDialog, 'exec') as execute:
            w._emotion_settings()
            execute.assert_not_called()
        w.sync_busy(False)
        self.assertTrue(w.emotion_settings_button.isEnabled())

    def test_open_close_motion_and_escape_interrupt(self):
        dialog = self.dialog(animate=True)
        dialog.show()
        QTest.qWait(20)
        self.assertLess(dialog.windowOpacity(), 1)
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.Rejected)
        other = self.dialog(animate=True)
        other.show()
        QTest.qWait(200)
        other.close()
        QTest.qWait(150)
        self.assertFalse(other.isVisible())
        closing = self.dialog(animate=True)
        closing.show()
        QTest.qWait(20)
        closing.accept()
        QTest.keyClick(closing.gap, Qt.Key_Up)
        self.assertFalse(closing.isVisible())
        self.assertEqual(closing.result(), QDialog.Accepted)


    def test_page_snapshots_each_versions_settings_for_full_and_preview(self):
        page = self.page()
        page.cfg.indextts2_generation_options = {'temperature': .7, 'interval_silence': 500}
        page.cfg.indextts25_generation_options = {'temperature': .9, 'interval_silence': 0}
        for panel, version, key in ((3, '2.0', 'indextts2_generation_options'), (0, '2.5', 'indextts25_generation_options')):
            page._switch_panel(panel)
            page._indextts2_status = fixture._ready_status()
            page._indextts2_status.model_version = version
            page._indextts2_voice_ready = lambda: True
            page._indextts2_background_status_snapshot = lambda: {'alive': False}
            page._set_indextts2_text_source('custom')
            page.indextts2_text_edit.setPlainText('这是测试文案。')
            page.indextts2_text_edit.selectAll()
            for preview in (False, True):
                with self.subTest(version=version, preview=preview), patch.object(fixture.voice_page_module, 'IndexTTS2SynthesisWorker', fixture._FakeIndexTTS2Worker):
                    page._generate_indextts2(preview=preview)
                    worker = fixture._FakeIndexTTS2Worker.instances[-1]
                    self.assertEqual(worker.kwargs['generation_options'], getattr(page.cfg, key))
                    self.assertEqual(worker.kwargs['model_version'], version)
                    self.assertIsNot(worker.kwargs['generation_options'], getattr(page.cfg, key))
                page._indextts2_worker = None
