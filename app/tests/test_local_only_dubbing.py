"""Removed cloud dubbing cannot be selected, configured or invoked remotely."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock
from PySide6.QtWidgets import QPushButton
from tests import test_voice_page_model_shell as fixture
from src.ui.voice_wave_slider import VoiceWaveSlider
from src.core.synthesis import minimax_tts as legacy
from src.core import models_manager as mm
from tests.model_settings_fixtures import ControllerCase

class LocalOnlyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VoicePageModelShellTests();self.f.setUp()
        self.model_root=patch.object(mm,'models_root',return_value=self.f.root/'models')
        self.data_root=patch.object(mm,'app_data_dir',return_value=self.f.root/'data')
        self.model_root.start();self.data_root.start()
        self.addCleanup(self.model_root.stop);self.addCleanup(self.data_root.stop)
    def tearDown(self):self.f.tearDown()
    def test_dropdown_four_local_engines_and_old_cloud_id_falls_back(self):
        with patch.object(legacy,'get_config',side_effect=AssertionError('must not read retired config')):
            p=fixture.VoicePage(fixture._Cfg(self.f.root),fixture._Main())
            self.assertEqual(list(p.engine_select.options), [('3','IndexTTS 2'),('0','IndexTTS 2.5'),('1','VoxCPM2'),('4','Qwen3-TTS')])
            for value,label in p.engine_select.options:
                p.engine_select.value_changed.emit(value)
                self.assertEqual(p._active_model_name(),label)
            self.assertFalse(any('MiniMax' in label for _,label in p.engine_select.options))
            self.assertFalse(hasattr(p,'minimax_text_edit'))
            for i in (4,2):p._switch_panel(i)
            self.assertEqual(p._active_model_name(),'IndexTTS 2.5')
            self.assertFalse(p.page_privacy_badge.property('cloud'))
            p.set_text('旧文案仍可使用');self.assertEqual(p.indextts2_text_edit.toPlainText(),'旧文案仍可使用')
            p.shutdown_workers()
            for slider in p.findChildren(VoiceWaveSlider):slider.stop_decode()
            p.close()
    def test_legacy_calls_stop_before_network_and_credentials(self):
        names=('save_config','request_from_config','synthesize','clone_voice','upload_clone_audio','refresh_voice_catalog','delete_uploaded_file','delete_remote_voice','delete_cloned_voice')
        with patch('src.core.secrets.get_config',side_effect=AssertionError('credential read')),patch('src.core.secrets.set_config',side_effect=AssertionError('credential write')),patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            self.assertFalse(legacy.is_configured())
            for name in names:
                fn=getattr(legacy,name);sig=inspect.signature(fn)
                kwargs={n:None for n,p in sig.parameters.items() if p.default is inspect.Parameter.empty}
                if 'post' in sig.parameters:kwargs['post']=Mock(side_effect=AssertionError('transport'))
                with self.subTest(name=name),self.assertRaises(legacy.MiniMaxTtsError) as ctx:fn(**kwargs)
                self.assertEqual(ctx.exception.code,'CLOUD_TTS_RETIRED')

class LocalOnlyConfigurationTests(ControllerCase):
    def test_model_configuration_dubbing_has_only_local_models(self):
        with self.public_state():value=self.c._read_state()
        self.assertEqual([x['modelId'] for x in value['localModels']['voice']],['local_indextts2_0','local_indextts2','local_voxcpm2','local_qwen3_tts'])
        self.service.execute.assert_not_called()
