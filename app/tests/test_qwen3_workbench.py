"""Qwen mode boundaries and native user flows, with isolated storage and no model/network."""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis.base import VoiceProfile,SynthesisResult
from src.core.synthesis.qwen3_options import validate_options,generation_inputs,MODES,SPEAKERS

class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.ref=self.root/'ref.wav';self.ref.write_bytes(b'reference')
        self.voice=VoiceProfile('test','原声',str(self.ref),'录音原文')
    def tearDown(self):self.temp.cleanup()
    def test_exact_clone_text_and_official_defaults(self):
        text=' 完整文本。\n\n保留自然段。 '
        kw=generation_inputs(text,self.voice,{'instruction':'不能使用'})
        self.assertEqual(kw['text'],text);self.assertEqual(kw['ref_text'],'录音原文')
        self.assertNotIn('instruct',kw);self.assertNotIn('max_new_tokens',kw)
        self.assertEqual(kw['temperature'],.9);self.assertFalse(kw['non_streaming_mode'])
    def test_distinct_official_apis_do_not_leak_clone_reference(self):
        for mode in ('official','design'):
            kw=generation_inputs('原文',self.voice,{'mode':mode,'instruction':'轻声说'})
            self.assertNotIn('ref_audio',kw);self.assertEqual(kw['instruct'],'轻声说')
            self.assertEqual('speaker' in kw,mode=='official');self.assertTrue(kw['non_streaming_mode'])
    def test_xvector_requires_audio_but_not_text(self):
        self.voice.reference_text=''
        with self.assertRaisesRegex(ValueError,'参考文字'):generation_inputs('你好',self.voice,{})
        kw=generation_inputs('你好',self.voice,{'x_vector_only_mode':True})
        self.assertTrue(kw['x_vector_only_mode'])
        self.ref.unlink()
        with self.assertRaises(ValueError):generation_inputs('你好',self.voice,{'x_vector_only_mode':True})
    def test_invalid_values_rejected_before_model(self):
        for options in ({'mode':'invalid'},{'language':'bogus'},{'speaker':'bogus'},{'temperature':float('nan')},{'repetition_penalty':0},{'seed':-1},{'x_vector_only_mode':'yes'},{'mode':'design'}):
            with self.subTest(options=options),self.assertRaises(ValueError):validate_options(options)
    def test_pipeline_requires_reference_only_for_clone(self):
        from src.core.synthesis import pipeline
        from contextlib import nullcontext
        from types import SimpleNamespace
        with patch.object(pipeline,'get_voice',return_value=None),patch.object(pipeline,'get_synthesis_engine',return_value=SimpleNamespace(synthesize=lambda r,*_:r)),patch('src.core.synthesis.resource_lock.voxcpm2_resource',return_value=nullcontext()):
            for mode in ('official','design'):
                r=pipeline.synthesize_text('原文','',self.root/'out.wav',engine='local_qwen3_tts',engine_options={'mode':mode})
                self.assertIsNone(r.voice)
            with self.assertRaises(RuntimeError):pipeline.synthesize_text('原文','',self.root/'out.wav',engine='local_qwen3_tts')

class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        from tests import test_voice_page_model_shell as fixture
        self.fixture=fixture.VoicePageModelShellTests();self.fixture.setUp();self.module=fixture.voice_page_module
        self.models=patch.object(self.module.local_models,'is_downloaded',return_value=False);self.models.start()
        self.ready=patch('src.ui.qwen3_workbench.qwen3_runtime.readiness',return_value=(True,'就绪'));self.ready.start()
        self.page=fixture.VoicePage(fixture._Cfg(self.fixture.root),fixture._Main());self.w=self.page._qwen3
        self.page._switch_panel(4);self.page.resize(1100,900);self.page.show();self.fixture.app.processEvents()
        self.fixture._wait_until(lambda:not self.page._model_status_reads)
    def tearDown(self):
        self.page.shutdown_workers()
        from src.ui.voice_wave_slider import VoiceWaveSlider
        for slider in self.page.findChildren(VoiceWaveSlider):slider.stop_decode()
        self.page.close();self.ready.stop();self.models.stop();self.fixture.tearDown()
    def sample(self,name='sample'):
        path=self.fixture.root/(name+'.wav')
        with wave.open(str(path),'wb') as f:f.setparams((1,2,8000,0,'NONE',''));f.writeframes(b'\0\1'*800)
        return path
    def test_switches_all_engines_without_index_collision(self):
        for choice,actual in [(0,'IndexTTS 2.5'),(3,'IndexTTS 2'),(4,'Qwen3-TTS'),(1,'VoxCPM2'),(2,'IndexTTS 2.5'),(4,'Qwen3-TTS')]:
            self.page._switch_panel(choice);self.assertEqual(self.page._active_model_name(),actual)
        self.assertEqual(self.page.panel_stack.currentIndex(),3)
        self.assertFalse(self.page.page_privacy_badge.property('cloud'))
    def test_modes_keep_drafts_and_support_nine_speakers(self):
        self.w.direction.setPlainText('开心分享');self.w.description.setPlainText('温暖女声')
        for mode in MODES:self.w.change_mode(mode)
        self.assertEqual(self.w.direction.toPlainText(),'开心分享');self.assertEqual(self.w.description.toPlainText(),'温暖女声');self.assertEqual(len(SPEAKERS),9)
    def test_missing_models_disable_real_generation(self):
        self.w.text_edit.setPlainText('你好');self.w.change_mode('official')
        self.fixture._wait_until(lambda:not self.page._model_status_reads)
        with patch('src.ui.qwen3_workbench.qwen3_runtime.readiness',return_value=(False,'CustomVoice 模型尚未准备')):
            self.page.invalidate_model_status();self.w.sync_text()
            self.fixture._wait_until(lambda:not self.page._model_status_reads)
        self.assertFalse(self.w.generate_button.isEnabled());self.assertIn('尚未准备',self.w.model_status.text())
    def test_clear_can_be_undone(self):
        self.w.text_edit.setPlainText('我的原文');self.w.clear_text();self.assertFalse(self.w.clear.isEnabled());self.w.text_edit.undo();self.assertEqual(self.w.text_edit.toPlainText(),'我的原文')
    def test_source_switch_retains_custom_draft(self):
        self.page._src_set_mode('qwen3','custom')
        self.w.text_edit.setPlainText('我的草稿');self.page._src_set_mode('qwen3','library');self.assertEqual(self.w.text_edit.toPlainText(),'');self.page._src_set_mode('qwen3','custom');self.assertEqual(self.w.text_edit.toPlainText(),'我的草稿')

    def test_all_four_engines_start_in_library_and_remember_session_selection(self):
        choices=((3,'indextts2'),(0,'indextts2'),(1,'voxcpm2'),(4,'qwen3'))
        for engine,prefix in choices:
            self.page._switch_panel(engine)
            self.assertEqual(self.page._src[prefix]['mode'],'library')
        modes=('custom','upload','custom','upload')
        for (engine,prefix),mode in zip(choices,modes):
            self.page._switch_panel(engine);self.page._src[prefix]['buttons'][mode].click()
        for (engine,prefix),mode in zip(choices,modes):
            self.page._switch_panel(engine);self.page.refresh()
            self.assertEqual(self.page._src[prefix]['mode'],mode)
        from tests import test_voice_page_model_shell as fixture
        fresh=fixture.VoicePage(self.page.cfg,fixture._Main())
        try:
            for engine,prefix in choices:
                fresh._switch_panel(engine)
                self.assertEqual(fresh._src[prefix]['mode'],'library')
        finally:
            fresh._qwen3.shutdown();fresh.close()

    def test_external_text_opens_editor_and_survives_source_round_trip(self):
        self.assertTrue(self.page.set_text('外部发送的配音正文。'))
        for prefix in ('indextts2','voxcpm2','qwen3'):
            state=self.page._src[prefix]
            self.assertEqual(state['mode'],'custom')
            self.assertEqual(state['text_edit'].toPlainText(),'外部发送的配音正文。')
            self.page._src_set_mode(prefix,'library')
            self.page._src_set_mode(prefix,'custom')
            self.assertEqual(state['text_edit'].toPlainText(),'外部发送的配音正文。')

    def test_missing_library_document_cannot_generate_previous_text(self):
        from types import SimpleNamespace
        for prefix in ('indextts2','voxcpm2','qwen3'):
            self.page._src_set_mode(prefix,'library')
            with patch.object(self.module.library,'load',return_value=SimpleNamespace(text='上一篇正文')):
                self.page._src_on_picked(prefix,{'id':'previous','title':'上一篇','kind':'transcript'})
            with patch.object(self.module.library,'load',return_value=None),patch.object(self.module.dialogs,'alert') as alert:
                self.page._src_on_picked(prefix,{'id':'missing','title':'已删除的文章','kind':'transcript'})
                alert.assert_called_once()
            self.assertEqual(self.page._src_text(prefix),'')
    def test_real_worker_request_freezes_design_inputs(self):
        from tests.test_voice_page_model_shell import _SignalProbe
        class Worker:
            def __init__(w,*args,**kw):
                w.args=args;w.kw=kw;w.progress=_SignalProbe();w.finished_ok=_SignalProbe();w.failed=_SignalProbe();w.cancelled=_SignalProbe()
            def start(w):pass
            def isRunning(w):return False
        self.w.change_mode('design');self.w.description.setPlainText('温暖女声');self.w.text_edit.setPlainText('原文。\n第二段。')
        with patch.object(self.module,'SynthesisWorker',Worker):self.w.generate()
        w=self.w.worker;self.assertEqual(w.kw['engine'],'local_qwen3_tts');self.assertIsNone(w.kw['voice_profile']);self.assertEqual(w.args[0],'原文。\n第二段。')
        self.w.description.setPlainText('之后修改');self.assertEqual(w.kw['engine_options']['instruction'],'温暖女声')
        self.assertFalse(self.w.editor_card.isEnabled());self.page._switch_panel(1);self.assertEqual(self.page._active_model_name(),'Qwen3-TTS');self.w.cancelled()
    def test_failure_cancel_preserve_last_result(self):
        p=self.sample();r={'output_path':str(p),'wav_path':str(p),'text':'旧结果','meta':{},'duration':5.84}
        self.w.select_result(r);self.assertEqual(self.w.time.text(),'0:00 / 0:05');self.w.set_busy(True)
        with patch.object(self.module.dialogs,'alert'):self.w.failed('error')
        self.assertIs(self.w.current_record,r);self.w.set_busy(True);self.w.cancelled();self.assertIs(self.w.current_record,r)
    def test_full_result_library_and_selected_preview_not_saved(self):
        p=self.sample();options=validate_options({'mode':'design','instruction':'温暖'})
        result=SynthesisResult(p,p,.1,'local_qwen3_tts','','本次原文',{'mode':'design','options':options})
        with patch.object(self.page,'_save_voice_safely',return_value=(True,'')) as save:
            for preview in (False,True):
                self.w.task={'preview':preview,'voice_name':'设计声音','source':{}};self.w.done(result)
            self.assertEqual(save.call_count,1);self.assertEqual(save.call_args.kwargs['text'],'本次原文')
        self.assertTrue(self.w.current_record['preview']);self.assertFalse(self.w.save_button.isHidden())
    def test_persisted_history_restores_metadata_and_recovers_draft(self):
        from src.core import library
        from PySide6.QtWidgets import QDialog,QPushButton
        opts=validate_options({'mode':'official','instruction':'轻声说','speaker':'Ryan'})
        library.save_voice(title='已生成',audio_path=self.sample(),text='旧文案',engine='Qwen3-TTS',voice_name='Ryan',config_summary='官方音色',meta={'mode':'official','options':opts})
        self.w.text_edit.setPlainText('新草稿');before=self.w.draft()
        def restore(d):next(b for b in d.findChildren(QPushButton) if b.text()=='恢复文案与设置').click()
        with patch.object(QDialog,'exec',restore):self.w.history()
        self.assertEqual(self.w.mode,'official');self.assertEqual(self.w.speaker,'Ryan');self.assertEqual(self.w.direction.toPlainText(),'轻声说')
        def recover(d):next(b for b in d.findChildren(QPushButton) if b.text()=='找回刚才的草稿').click()
        with patch.object(QDialog,'exec',recover):self.w.history()
        self.assertEqual(self.w.draft(),before)
    def test_settings_save_failure_does_not_apply(self):
        from PySide6.QtWidgets import QDialog,QPushButton,QDoubleSpinBox
        def change(d):d.findChild(QDoubleSpinBox).setValue(1.5);next(b for b in d.findChildren(QPushButton) if b.text()=='完成').click();d.reject()
        with patch.object(QDialog,'exec',change),patch.object(self.page.cfg,'save',return_value=False):self.w.edit_settings()
        self.assertEqual(self.w.options['clone']['temperature'],.9)
    def test_clone_fallback_settings_are_explicit_and_reversible(self):
        from PySide6.QtWidgets import QDialog,QPushButton
        from src.ui.control_foundation import LuluCheckBox
        def interact(checked,action):
            def run(d):
                option=d.findChild(LuluCheckBox)
                self.assertIsNotNone(option)
                option.setChecked(checked)
                next(b for b in d.findChildren(QPushButton) if b.text()==action).click()
                d.reject()
            return run
        self.assertFalse(self.w.no_text.isChecked())
        with patch.object(QDialog,'exec',interact(True,'取消')):self.w.edit_settings()
        self.assertFalse(self.w.no_text.isChecked())
        with patch.object(QDialog,'exec',interact(True,'完成')),patch.object(self.page.cfg,'save',return_value=False):self.w.edit_settings()
        self.assertFalse(self.w.no_text.isChecked())
        with patch.object(QDialog,'exec',interact(True,'完成')):self.w.edit_settings()
        self.assertTrue(self.w.generation_options()['x_vector_only_mode'])
        self.assertTrue(self.page.cfg.qwen3_generation_options['clone']['x_vector_only_mode'])
        self.assertIn('已调整',self.w.settings_button.text())
        def reset(d):
            for action in ('恢复默认','完成'):next(b for b in d.findChildren(QPushButton) if b.text()==action).click()
        with patch.object(QDialog,'exec',reset):self.w.edit_settings()
        self.assertFalse(self.w.generation_options()['x_vector_only_mode'])
        self.assertIn('默认',self.w.settings_button.text())
        self.w.change_mode('official')
        def official(d):self.assertIsNone(d.findChild(LuluCheckBox));d.reject()
        with patch.object(QDialog,'exec',official):self.w.edit_settings()

    def test_editor_geometry_matches_local_engines_for_every_source(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        for width in (1100,720):
            self.page.resize(width,950);baseline=None
            for engine,prefix in ((0,'indextts2'),(3,'indextts2'),(1,'voxcpm2'),(4,'qwen3')):
                self.page._switch_panel(engine)
                for source in ('custom','library','upload'):
                    self.page._src_set_mode(prefix,source);QTest.qWait(50)
                    stack=self.page._src[prefix]['src_stack'];card=stack.parentWidget()
                    actual=(card.height(),stack.height(),stack.mapTo(self.page,QPoint()).y())
                    if baseline is None:baseline=actual
                    self.assertEqual(actual,baseline,(width,engine,source))
        self.assertFalse(self.w.editor_card.isAncestorOf(self.w.language))
        self.assertTrue(self.w.sound_card.isAncestorOf(self.w.language))
        self.w.language.set_value('English')
        self.assertEqual(self.w.generation_options()['language'],'English')

    def test_save_design_adds_real_reference_and_selects_clone(self):
        from PySide6.QtWidgets import QDialog,QPushButton
        from src.core.synthesis import voices
        path=self.sample();self.w.current_record={'wav_path':str(path),'text':'设计声音的准确原文'}
        def save(d):next(b for b in d.findChildren(QPushButton) if b.text()=='保存音色').click()
        with patch.object(QDialog,'exec',save):self.w.save_voice()
        voice=voices.get_voice(self.w.voice_combo.currentData());self.assertEqual(voice.reference_text,'设计声音的准确原文');self.assertEqual(Path(voice.reference_audio_path).read_bytes(),path.read_bytes());self.assertEqual(self.w.mode,'clone');self.assertEqual(voice.meta['origin'],'qwen3_voice_design')
    def test_export_keeps_existing_files(self):
        path=self.sample();self.w.current_record={'wav_path':str(path)};self.w.export();self.fixture._wait_until(lambda:not self.w.export_worker.isRunning());self.w.export();self.fixture._wait_until(lambda:not self.w.export_worker.isRunning())
        self.assertEqual(len(list(Path(self.page.cfg.tts_output_dir).glob('*.wav'))),2)
    def test_official_sample_uses_selected_voice_without_replacing_draft(self):
        from tests.test_voice_page_model_shell import _FakeMediaPlayer
        from PySide6.QtWidgets import QDialog,QPushButton
        players=[]
        def factory(*args):
            player=_FakeMediaPlayer(*args);players.append(player);return player
        self.w.change_mode('official');self.w.text_edit.setPlainText('我的完整文案');self.w.direction.setPlainText('兴奋地表达')
        original_speaker=self.w.speaker
        def listen(dialog):
            def button(name):return next(b for b in dialog.findChildren(QPushButton) if b.accessibleName()==name)
            player=players[-1]
            button('试听 Vivian').click()
            self.assertEqual(player._state,player.PlayingState)
            self.assertEqual(Path(player.source().toLocalFile()).name,'Vivian.wav')
            self.assertEqual(self.w.speaker,original_speaker)
            self.assertEqual(dialog.result(),0)
            button('暂停 Vivian').click();self.assertEqual(player._state,player.PausedState)
            button('试听 Serena').click();self.assertEqual(player._state,player.PlayingState)
            self.assertEqual(Path(player.source().toLocalFile()).name,'Serena.wav')
            dialog.reject();self.assertEqual(player._state,player.StoppedState)
        with patch.object(QDialog,'exec',listen),patch.object(self.module,'QMediaPlayer',side_effect=factory) as mocked_player,patch.object(self.module,'SynthesisWorker') as worker,patch('src.ui.qwen3_workbench.qwen3_runtime.readiness',return_value=(False,'未安装')):
            mocked_player.PlayingState=_FakeMediaPlayer.PlayingState
            self.w.pick_speaker();worker.assert_not_called()
        self.assertEqual(self.w.text_edit.toPlainText(),'我的完整文案')
        self.assertEqual(self.w.direction.toPlainText(),'兴奋地表达')
        self.assertEqual(self.w.speaker,original_speaker)
        self.assertIsNone(self.w.worker);self.assertFalse(self.w.records)
        self.assertTrue(players[-1].source().isEmpty())

    def test_missing_sample_never_falls_back_to_generation(self):
        from PySide6.QtWidgets import QDialog,QPushButton,QLabel
        def listen(dialog):
            next(b for b in dialog.findChildren(QPushButton) if b.accessibleName()=='试听 Vivian').click()
            self.assertTrue(any('样音文件缺失' in l.text() for l in dialog.findChildren(QLabel)))
            dialog.reject()
        with patch.object(QDialog,'exec',listen),patch.object(self.module,'SynthesisWorker') as worker,patch('src.ui.qwen3_workbench.OFFICIAL_SAMPLES',self.fixture.root/'missing'):
            self.w.pick_speaker();worker.assert_not_called()

    def test_picker_filters_and_requires_explicit_confirmation(self):
        from PySide6.QtWidgets import QDialog, QPushButton, QLineEdit, QFrame
        original = self.w.speaker
        def inspect(dialog, accept=False):
            dialog.show(); self.fixture.app.processEvents()
            buttons = dialog.findChildren(QPushButton)
            next(b for b in buttons if b.accessibleName().startswith('选择 Dylan，')).click()
            self.assertEqual(self.w.speaker, original)
            search = dialog.findChild(QLineEdit)
            search.setText('不存在的声音')
            frames = dialog.findChildren(QFrame, 'speakerRow')
            self.assertTrue(all(f.isHidden() for f in frames))
            search.clear()
            next(b for b in buttons if b.text() == '英文').click()
            self.assertEqual(sum(not f.isHidden() for f in frames), 2)
            if accept:
                next(b for b in buttons if b.accessibleName() == '使用 Dylan').click()
            else:
                dialog.reject()
        with patch.object(QDialog, 'exec', lambda d: inspect(d)):
            self.w.pick_speaker()
        self.assertEqual(self.w.speaker, original)
        with patch.object(QDialog, 'exec', lambda d: inspect(d, True)):
            self.w.pick_speaker()
        self.assertEqual(self.w.speaker, 'Dylan')

class BundledSamplesTests(unittest.TestCase):
    def test_all_speakers_have_verified_playable_packaged_samples(self):
        import hashlib,json
        from src.ui.qwen3_workbench import OFFICIAL_SAMPLES
        manifest=json.loads((OFFICIAL_SAMPLES/'manifest.json').read_text())['samples']
        self.assertEqual(set(manifest),set(SPEAKERS))
        for name,record in manifest.items():
            path=OFFICIAL_SAMPLES/(name+'.wav')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),record['sha256'])
            with wave.open(str(path)) as sample:
                self.assertEqual(sample.getframerate(),24000)
                self.assertAlmostEqual(sample.getnframes()/sample.getframerate(),record['duration_seconds'])
                self.assertGreater(sample.getnframes(),0)
