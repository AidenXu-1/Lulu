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
