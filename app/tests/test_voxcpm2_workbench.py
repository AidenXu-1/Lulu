"""Vox integration boundaries. All model, credentials and storage effects isolated."""
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import nullcontext
from src.core.synthesis.base import VoiceProfile
from src.core.synthesis.voxcpm2_options import generation_inputs, validate_options

class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.ref=self.root/'ref.wav';self.ref.write_bytes(b'reference')
        self.voice=VoiceProfile('test','声音',str(self.ref),'准确的参考文字。')
    def tearDown(self):self.tmp.cleanup()
    def test_complete_clone_keeps_text_and_reference_pair(self):
        text=' 今天我们聊聊声音里的细节。\n\n不改动空白，也不拆分。 '
        kw=generation_inputs(text,self.voice,{'instruction':'应被忽略'})
        self.assertEqual(kw['text'],text);self.assertEqual(kw['prompt_wav_path'],kw['reference_wav_path'])
        self.assertEqual(kw['prompt_text'],self.voice.reference_text)
        for key in ('normalize','retry_badcase','denoise'):self.assertFalse(kw[key])
        self.assertNotIn('seed',kw)
    def test_modes_do_not_leak_reference_or_prompt(self):
        kw=generation_inputs('你好。',self.voice,{'mode':'directed','instruction':'轻声说'})
        self.assertEqual(kw['text'],'(轻声说)你好。');self.assertNotIn('prompt_text',kw)
        kw=generation_inputs('你好。',None,{'mode':'design','instruction':'温暖的成年男性'})
        self.assertEqual(kw['text'],'(温暖的成年男性)你好。');self.assertNotIn('reference_wav_path',kw)
    def test_invalid_inputs_stop_before_model(self):
        for raw in ({'mode':'bad'},{'inference_timesteps':31},{'cfg_value':float('nan')},{'seed':-1},{'normalize':'yes'},{'denoise':True},{'mode':'design'}):
            with self.subTest(raw=raw),self.assertRaises(ValueError):validate_options(raw)
        self.voice.reference_text=''
        with self.assertRaisesRegex(ValueError,'参考文字'):generation_inputs('原文',self.voice,{})
    def test_pipeline_no_voice_only_for_design(self):
        from src.core.synthesis import pipeline
        fake=SimpleNamespace(synthesize=lambda req,*args:req)
        with patch.object(pipeline,'get_voice',return_value=None),patch.object(pipeline,'get_synthesis_engine',return_value=fake),patch('src.core.synthesis.resource_lock.voxcpm2_resource',return_value=nullcontext()):
            req=pipeline.synthesize_text('你好','',self.root/'out.wav',engine='local_voxcpm2',engine_options={'mode':'design','instruction':'温柔'})
            self.assertIsNone(req.voice)
            with self.assertRaises(RuntimeError):pipeline.synthesize_text('你好','',self.root/'out.wav',engine='local_indextts2')

class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        from tests import test_voice_page_model_shell as fixture
        self.fixture=fixture.VoicePageModelShellTests();self.fixture.setUp();self.module=fixture.voice_page_module
        self.models=patch.object(self.module.local_models,'is_downloaded',return_value=False);self.models.start()
        self.page=fixture.VoicePage(fixture._Cfg(self.fixture.root),fixture._Main());self.w=self.page._approved_vox
        self.page.panel_stack.setCurrentIndex(1);self.page.resize(900,950);self.page.show();self.fixture.app.processEvents()
    def tearDown(self):
        from src.ui.voice_wave_slider import VoiceWaveSlider
        for slider in self.page.findChildren(VoiceWaveSlider):slider.stop_decode()
        self.page.close();self.models.stop();self.fixture.tearDown()
    def test_modes_fixed_height_and_drafts(self):
        self.w.direction.setPlainText('轻松分享');self.w.description.setPlainText('沉稳女声')
        for mode in ('hifi','directed','design','hifi'):
            self.w.change_mode(mode);self.fixture.app.processEvents()
            self.assertEqual(self.w.body.height(),200);self.assertEqual(self.w.voice_bar.isHidden(),mode=='design')
        self.assertEqual(self.w.direction.toPlainText(),'轻松分享');self.assertEqual(self.w.description.toPlainText(),'沉稳女声')

    def test_expression_preset_tracks_edits_and_can_be_undone(self):
        self.w.change_mode('directed')
        self.w.direction.setPlainText('我自己的表达要求')
        prompt = '开心地分享，语气轻松'
        button = self.w.direction_presets[prompt]
        button.click()
        self.assertEqual(self.w.generation_options()['instruction'], prompt)
        self.assertTrue(button.isChecked())
        button.click()
        self.assertTrue(button.isChecked())
        self.w.direction.undo()
        self.assertEqual(self.w.direction.toPlainText(), '我自己的表达要求')
        self.assertFalse(any(b.isChecked() for b in self.w.direction_presets.values()))
        self.w.direction.redo()
        self.assertTrue(button.isChecked())
        self.w.direction.insertPlainText('，慢一点')
        self.assertFalse(any(b.isChecked() for b in self.w.direction_presets.values()))
        self.w.change_mode('design'); self.w.change_mode('directed')
        self.assertIn('慢一点', self.w.generation_options()['instruction'])
    def test_failure_cancel_keep_result(self):
        path=self.fixture.root/'result.wav'
        with wave.open(str(path),'wb') as audio:
            audio.setparams((1,2,8000,0,'NONE',''));audio.writeframes(b'\0\0'*800)
        r={'output_path':str(path),'summary':'完整克隆','duration':.1,'meta':{'mode':'hifi'}}
        self.w.select_result(r);self.w.set_busy(True)
        with patch.object(self.module.dialogs,'alert'):self.w.failed('失败')
        self.assertEqual(self.page._last_output,str(path));self.assertIs(self.w.current_record,r)
        self.w.set_busy(True);self.w.cancelled();self.assertEqual(self.page._last_output,str(path))
        self.assertFalse(self.page.voxcpm2_player_bar.isHidden())
    def test_design_request_freezes_inputs_without_fake_voice(self):
        from tests.test_voice_page_model_shell import _SignalProbe
        class Worker:
            def __init__(worker,*args,**kw):
                worker.args=args;worker.kw=kw
                worker.progress=_SignalProbe();worker.finished_ok=_SignalProbe();worker.failed=_SignalProbe();worker.cancelled=_SignalProbe()
            def start(worker):pass
            def isRunning(worker):return False
        self.w.change_mode('design');self.w.description.setPlainText('温暖的声音');self.page.text_edit.setPlainText('整篇原文。\n保持自然段。')
        with patch.object(self.module,'SynthesisWorker',Worker):self.w.generate()
        worker=self.page._worker
        self.assertEqual(worker.args[0],'整篇原文。\n保持自然段。');self.assertEqual(worker.args[1],'');self.assertIsNone(worker.kw['voice_profile'])
        self.w.description.setPlainText('之后修改');self.assertEqual(worker.kw['engine_options']['instruction'],'温暖的声音')
        self.assertFalse(self.w.mode_buttons['hifi'].isEnabled());self.w.cancelled();self.page._worker=None
    def test_reference_correction_leaves_audio(self):
        from src.core.synthesis import voices
        path=self.fixture.root/'ref.wav';path.write_bytes(b'unchanged');voice=voices.add_voice('测试',str(path),'旧文字')
        changed=voices.update_reference_text(voice.id,'校对的文字')
        self.assertEqual(voices.get_voice(voice.id).reference_text,'校对的文字');self.assertEqual(Path(changed.reference_audio_path).read_bytes(),b'unchanged')
    def test_export_does_not_overwrite(self):
        path=self.fixture.root/'source.wav';path.write_bytes(b'original');self.w.current_record={'output_path':str(path)}
        self.w.export();self.w.export();folder=Path(self.page.cfg.tts_output_dir)
        self.assertEqual(len(list(folder.glob('*.wav'))),2);self.assertEqual(path.read_bytes(),b'original')

    def test_full_result_is_saved_but_selected_preview_is_not(self):
        from src.core.synthesis.base import SynthesisResult
        path=self.fixture.root/'render.wav'
        with wave.open(str(path),'wb') as audio:
            audio.setparams((1,2,8000,0,'NONE',''));audio.writeframes(b'\0\0'*800)
        result=SynthesisResult(output_path=path,wav_path=path,duration_seconds=.1,
            engine='local_voxcpm2',voice_id='',text='已经提交的原文。',
            meta={'mode':'design','options':validate_options({'mode':'design','instruction':'沉稳女声'})})
        self.page.text_edit.setPlainText('生成期间编辑的新文案。')
        with patch.object(self.page,'_save_voice_safely',return_value=(True,'')) as save:
            for preview in (False,True):
                self.w._task={'preview':preview,'voice_name':'设计声音','source':{}}
                self.w.set_busy(True);self.w.done(result)
            self.assertEqual(save.call_count,1)
            self.assertEqual(save.call_args.kwargs['text'],'已经提交的原文。')
        self.assertEqual(self.page.text_edit.toPlainText(),'生成期间编辑的新文案。')
        self.assertEqual(len(self.w.records),2)
        self.assertTrue(self.w.current_record['preview'])
        self.assertFalse(self.w.save_voice_button.isHidden())

    def test_playback_is_exclusive_and_progress_has_no_invented_percentage(self):
        self.w.voice_player.play()
        self.w._play_icon(True)
        self.assertEqual(self.w.voice_player.playbackState(),self.w.voice_player.PausedState)
        self.w.progress('正在生成声音…',.15)
        self.assertEqual(self.page.progress.maximum(),0)
        self.assertNotIn('%',self.page.status.text())

    def test_real_qt_playback_state_updates_icon(self):
        from PySide6.QtMultimedia import QMediaPlayer
        with patch.object(self.module,'QMediaPlayer',QMediaPlayer):
            self.page.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(self.page.btn_play.accessibleName(),'暂停本次结果')
            self.assertEqual(self.page.btn_play.text(),'')
            self.page.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)
            self.assertEqual(self.page.btn_play.accessibleName(),'播放本次结果')

    def test_clear_cancel_confirm_preserves_result(self):
        self.page.text_edit.setPlainText('保留原文')
        self.w.current_record={'output_path':'old.wav'}
        with patch('src.ui.voxcpm2_workbench.dialogs.confirm',return_value=False):self.w.clear_text()
        self.assertEqual(self.page.text_edit.toPlainText(),'保留原文')
        with patch('src.ui.voxcpm2_workbench.dialogs.confirm',return_value=True):self.w.clear_text()
        self.assertEqual(self.page.text_edit.toPlainText(),'')
        self.assertFalse(self.w.clear.isEnabled())
        self.assertEqual(self.w.current_record['output_path'],'old.wav')

    def test_settings_failed_save_and_cancel_preserve_values(self):
        from PySide6.QtWidgets import QDialog,QPushButton,QSpinBox,QLabel
        def interact(dialog):
            dialog.findChild(QSpinBox).setValue(12)
            next(b for b in dialog.findChildren(QPushButton) if b.text()=='保存设置').click()
            self.assertTrue(any('保存失败' in l.text() for l in dialog.findChildren(QLabel)))
            dialog.reject()
        with patch.object(QDialog,'exec',interact),patch.object(self.page.cfg,'save',return_value=False):self.w.edit_settings()
        self.assertEqual(self.w.options['hifi']['inference_timesteps'],10)

    def test_pronunciation_save_failure_keeps_previous(self):
        from PySide6.QtWidgets import QDialog,QPushButton,QLineEdit
        def interact(dialog):
            entries=dialog.findChildren(QLineEdit);entries[0].setText('重庆');entries[1].setText('chong2 qing4')
            next(b for b in dialog.findChildren(QPushButton) if b.text()=='保存读音').click()
            dialog.reject()
        with patch.object(QDialog,'exec',interact),patch.object(self.page.cfg,'save',return_value=False):self.w.edit_pronunciations()
        self.assertEqual(self.w.pronunciations,[])
        self.assertEqual(self.page.cfg.voxcpm2_pronunciations,[])

    def test_history_restores_and_recovers_complete_draft(self):
        from PySide6.QtWidgets import QDialog,QPushButton
        self.page.text_edit.setPlainText('我的草稿');self.w.change_mode('directed');self.w.direction.setPlainText('轻声')
        self.w.options['directed']['cfg_value']=1.5
        self.w.pronunciations=[{'source':'重庆','reading':'chong2 qing4'}]
        before=self.w._draft()
        self.w.records=[{'text':'旧文案','output_path':'/missing.wav','meta':{'options':{'mode':'design','instruction':'成年女性','inference_timesteps':10,'cfg_value':2,'normalize':False,'seed':1234}},'summary':'设计声音'}]
        def restore(d):
            buttons=d.findChildren(QPushButton)
            self.assertFalse(next(b for b in buttons if b.text()=='查看结果').isEnabled())
            next(b for b in buttons if b.text()=='用此文案与设置').click()
        with patch.object(QDialog,'exec',restore):self.w.history()
        self.assertEqual(self.page.text_edit.toPlainText(),'旧文案');self.assertEqual(self.w.mode,'design')
        def recover(d):next(b for b in d.findChildren(QPushButton) if b.text()=='找回刚才的草稿').click()
        with patch.object(QDialog,'exec',recover):self.w.history()
        self.assertEqual(self.w._draft(),before)
