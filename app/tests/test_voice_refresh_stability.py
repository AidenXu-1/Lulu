"""Page refresh must not unload an unchanged reference or rebuild its selector."""
import unittest
from unittest.mock import patch

from tests import test_voice_page_model_shell as fixture
from src.ui.voice_wave_slider import VoiceWaveSlider


class VoiceRefreshStabilityTests(unittest.TestCase):
    def setUp(self):
        fixture.VoicePageModelShellTests.setUp(self)
        self.models = patch.object(fixture.voice_page_module.local_models, 'is_downloaded', return_value=False)
        self.models.start()
        self.waveform = patch.object(VoiceWaveSlider, 'load_file', autospec=True)
        self.waveform.start()
        self.items = []
        for key in ('a', 'b', 'c'):
            audio = self.root / f'{key}.wav'
            audio.touch()
            self.items.append({'id': key, 'name': f'Voice {key}', 'reference_audio_path': str(audio)})
        fixture.voices._write(self.items)
        self.page = fixture.VoicePage(fixture._Cfg(self.root), fixture._Main())
        self.page.refresh()
        self.page.indextts2_voice_combo.setCurrentIndex(2)
        self.player = self.page._approved_index.voice_player

    def tearDown(self):
        self.page.shutdown_workers()
        self.page.close()
        self.waveform.stop()
        self.models.stop()
        fixture.VoicePageModelShellTests.tearDown(self)

    def test_unchanged_refresh_preserves_reference_playback_and_selector(self):
        combo = self.page.indextts2_voice_combo
        selector = self.page._voice_sel[id(combo)]['selector']
        self.player.play()
        self.player.setPosition(321)
        with patch.object(self.player, 'setSource', wraps=self.player.setSource) as source:
            for _ in range(3):
                self.page.refresh()
            source.assert_not_called()
        self.assertIs(self.page._voice_sel[id(combo)]['selector'], selector)
        self.assertEqual(combo.currentData(), 'c')
        self.assertEqual(self.player.position(), 321)
        self.assertEqual(self.player.playbackState(), self.player.PlayingState)

    def test_changed_library_updates_names_and_keeps_selected_reference(self):
        self.items[2]['name'] = 'Renamed voice'
        self.items.append({'id': 'd', 'name': 'New voice', 'reference_audio_path': self.items[0]['reference_audio_path']})
        fixture.voices._write(self.items)
        with patch.object(self.player, 'setSource', wraps=self.player.setSource) as source:
            self.page.refresh()
            source.assert_not_called()
        for combo in (self.page.voice_combo, self.page.indextts2_voice_combo):
            self.assertEqual(combo.count(), 4)
            self.assertEqual(combo.itemText(combo.findData('c')), 'Renamed voice')
        self.assertEqual(self.page.indextts2_voice_combo.currentData(), 'c')
        self.assertIn('Renamed voice', self.page._approved_index.follow_name.text())

    def test_removed_selection_loads_only_final_fallback_then_empty_library(self):
        fixture.voices._write(self.items[:2])
        with patch.object(self.player, 'setSource', wraps=self.player.setSource) as source:
            self.page.refresh()
            source.assert_called_once()
        self.assertEqual(self.page.indextts2_voice_combo.currentData(), 'a')
        self.assertEqual(self.player.source().toLocalFile(), self.items[0]['reference_audio_path'])
        fixture.voices._write([])
        self.page.refresh()
        self.assertEqual(self.page.indextts2_voice_combo.count(), 0)
        self.assertTrue(self.player.source().isEmpty())
        self.assertFalse(self.page._approved_index.voice_play.isEnabled())

    def test_reference_path_change_with_same_name_is_still_refreshed(self):
        replacement = self.root / 'replacement.wav'
        replacement.touch()
        self.items[2]['reference_audio_path'] = str(replacement)
        fixture.voices._write(self.items)
        with patch.object(self.player, 'setSource', wraps=self.player.setSource) as source:
            self.page.refresh()
            source.assert_called_once()
        self.assertEqual(self.player.source().toLocalFile(), str(replacement))
        self.assertEqual(self.page.indextts2_voice_combo.currentData(), 'c')
