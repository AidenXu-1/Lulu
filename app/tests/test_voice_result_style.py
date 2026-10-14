"""Result presentation checks with isolated config and fake media/network."""
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from tests import test_voice_page_model_shell as fixture
from src.ui.voice_wave_slider import VoiceWaveSlider
from src.ui.voice_result_style import OutputLocationButton
import unittest

class ResultStyleTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VoicePageModelShellTests();self.f.setUp()
        self.p=fixture.VoicePage(fixture._Cfg(self.f.root),fixture._Main())
    def tearDown(self):
        for slider in self.p.findChildren(VoiceWaveSlider):slider.stop_decode()
        self.p.close();self.f.tearDown()
    def test_long_location_preserves_prefix_and_shared_picker_updates_all(self):
        p=self.p;path=str(Path.home()/'Downloads'/('很长的文件夹名称'*16))
        with patch.object(fixture.voice_page_module.QFileDialog,'getExistingDirectory',return_value=path):p._pick_output_dir()
        for b in (p.out_dir_btn,p.btn_indextts2_output_dir,p._qwen3.output_dir_button):
            self.assertIsInstance(b,OutputLocationButton);b.resize(180,38)
            self.assertTrue(b.display_text().startswith('~/'))
            self.assertTrue(b.display_text().endswith('…'))
            self.assertFalse(b.icon().isNull());self.assertEqual(b.toolTip(),path)
    def test_common_geometry_and_special_actions_survive_at_two_widths(self):
        p=self.p
        # No real files means wave decoding never starts; media backend is fake.
        r={'output_path':'/missing.wav','wav_path':'/missing.wav','duration':8,'summary':'声音设置 '*50,'text':'原文','meta':{'mode':'design'}}
        for width in (1100,720):
            for panel in (0,3,1,4):
                p._switch_panel(panel);p.resize(width,1000);p.show()
                if panel in (0,3):
                    p._select_indextts2_result(r);play,wave,export,loc=p.btn_indextts2_play,p.indextts2_player_slider,p.btn_indextts2_export,p.btn_indextts2_output_dir
                elif panel==1:
                    p._approved_vox.select_result(r);play,wave,export,loc=p.btn_play,p.player_slider,p._approved_vox.export_button,p.out_dir_btn
                elif panel==4:
                    p._qwen3.select_result(r);play,wave,export,loc=p._qwen3.play_button,p._qwen3.wave,p._qwen3.export_button,p._qwen3.output_dir_button
                QTest.qWait(20)
                with self.subTest(width=width,panel=panel):
                    self.assertEqual((play.width(),play.height()),(44,44));self.assertEqual(export.height(),38);self.assertEqual(loc.height(),38)
                    self.assertGreater(export.mapTo(p,QPoint()).y(),wave.mapTo(p,QPoint()).y())
                    self.assertGreater(loc.mapTo(p,QPoint()).x(),export.mapTo(p,QPoint()).x())
                    self.assertLessEqual(loc.mapTo(p,QPoint()).x()+loc.width(),p.width())
        self.assertEqual(p._approved_index.studio_button.text(),'局部调整')
        self.assertTrue(p._approved_vox.save_voice_button.isEnabled());self.assertTrue(p._qwen3.save_button.isEnabled())
