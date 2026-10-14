"""Real local audio controls and recoverable file actions; no user files or sound output."""
import wave
import shutil
import threading
import unittest
from unittest.mock import patch

from tests import test_agent_workspace_ui as ui
from src.core import library
from PySide6.QtMultimedia import QMediaPlayer


class WorkspaceAudioTests(unittest.TestCase):
    setUpClass = ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    setUp = ui.AgentWorkspaceUiTests.setUp
    tearDown = ui.AgentWorkspaceUiTests.tearDown
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js

    def open_audio(self, *, missing=False, corrupt=False):
        self.widget.output.setMuted(True)
        path = self.root / '声音.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000 * 4)
        if corrupt:
            path.write_bytes(b'not a wave file')
        eid = library.save_voice(title='独立音频', audio_path=path, text='生成时的正文',
            engine='test', voice_name='测试音色', config_summary='隔离样例',
            source_id=self.eid, source_text_snapshot='生成时的正文')
        if missing:
            path.rename(self.root / '移动后的声音.wav')
        self.widget._set_kind('voice')
        self.widget.open_entry(eid)
        self.until(lambda: self.js('Boolean(document.getElementById("audio-play"))'))
        return eid, path

    def test_missing_audio_relink_cancel_and_reopen_keep_original_text(self):
        eid, path = self.open_audio(missing=True)
        self.assertTrue(self.js('document.getElementById("audio-play").disabled'))
        self.assertIn('重新定位', self.js('document.getElementById("audio-relink").textContent'))
        with patch('src.ui.agent_workspace.widget.QFileDialog.getOpenFileName', return_value=('', '')):
            self.js('document.getElementById("audio-relink").click()')
            for _ in range(8): self.tick()
        self.assertEqual(library.load_voice(eid)['audio_path'], str(path))
        moved = self.root / '移动后的声音.wav'
        with patch('src.ui.agent_workspace.widget.QFileDialog.getOpenFileName', return_value=(str(moved), '')):
            self.js('document.getElementById("audio-relink").click()')
            self.until(lambda: not library.load_voice(eid)['missing_flags']['audio_missing'])
            self.until(lambda: self.js('!document.getElementById("audio-play").disabled'))
        library.save_markdown(self.eid, '当前稿已经修改')
        self.widget.open_entry(eid)
        self.until(lambda: self.js('document.querySelector(".source-text").textContent') == '生成时的正文')
        self.assertEqual(library.load_voice(eid)['source_text_snapshot'], '生成时的正文')
        self.assertNotIn(str(self.root), self.js('document.getElementById("detail").innerText'))

    def test_play_pause_seek_and_revisit_reflect_the_actual_player(self):
        eid, _ = self.open_audio()
        self.js('document.getElementById("audio-play").click()')
        self.until(lambda: self.widget.player.duration() == 4000)
        self.until(lambda: self.js('document.getElementById("audio-play").getAttribute("aria-label")') == '暂停')
        self.js('document.getElementById("audio-play").click()')
        self.until(lambda: self.widget.player.playbackState() == QMediaPlayer.PausedState)
        self.js('document.getElementById("audio-seek").value=500;document.getElementById("audio-seek").dispatchEvent(new Event("change"))')
        self.until(lambda: self.widget.player.position() == 2000)
        self.widget.call('open', None)
        self.until(lambda: self.js('window.lulu.state.doc===null'))
        self.widget.open_entry(eid)
        self.until(lambda: self.js('document.getElementById("audio-time")?.textContent') == '00:02 / 00:04')
        self.assertEqual(self.js('Number(document.getElementById("audio-seek").value)'), 500)

    def test_corrupt_audio_shows_recovery_without_discarding_snapshot(self):
        self.open_audio(corrupt=True)
        self.js('document.getElementById("audio-play").click()')
        self.until(lambda: self.widget.player.error() != QMediaPlayer.NoError)
        self.until(lambda: self.js('document.getElementById("audio-status")?.textContent.includes("无法播放")'))
        self.assertFalse(self.js('document.getElementById("audio-relink").hidden'))
        self.assertEqual(self.js('document.querySelector(".source-text").textContent'), '生成时的正文')

    def test_export_failure_can_retry_without_overwriting_existing_audio(self):
        _, source = self.open_audio()
        folder = self.root / 'exports'
        folder.mkdir()
        target = folder / '声音.wav'
        target.write_bytes(b'old export')
        with patch('src.ui.agent_workspace.widget.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            with patch('shutil.copyfile', side_effect=OSError('disk full')):
                self.js('document.getElementById("audio-export").click()')
                self.until(lambda: self.js('document.getElementById("audio-status")?.textContent.includes("导出未完成")'))
            self.assertEqual(target.read_bytes(), b'old export')
            self.assertEqual(list(folder.iterdir()), [target])
            self.js('document.getElementById("audio-export").click()')
            self.until(lambda: self.js('document.getElementById("audio-status")?.textContent.startsWith("已导出")'))
        actual = next(p for p in folder.iterdir() if p != target)
        self.assertEqual(actual.read_bytes(), source.read_bytes())
        self.assertEqual(target.read_bytes(), b'old export')

    def test_export_runs_in_background_and_does_not_replace_a_new_selection(self):
        self.open_audio()
        target = self.root / '导出的声音.wav'
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        copy = shutil.copyfile
        def delayed_copy(*args, **kwargs):
            started.set()
            try:
                if not release.wait(5):
                    raise OSError('test did not release background copy')
                return copy(*args, **kwargs)
            finally:
                finished.set()
        try:
            with patch('src.ui.agent_workspace.widget.QFileDialog.getSaveFileName', return_value=(str(target), '')):
                with patch('shutil.copyfile', side_effect=delayed_copy):
                    self.js('document.getElementById("audio-export").click()')
                    self.until(started.is_set)
                    self.widget.call('setKind', 'documents', self.eid)
                    self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
                    self.assertFalse(finished.is_set())
                    release.set()
                    self.until(target.is_file)
                    self.assertEqual(self.js('window.lulu.state.doc?.id'), self.eid)
                    self.assertFalse(self.js('Boolean(document.getElementById("audio-status"))'))
        finally:
            release.set()

    def test_waveform_is_real_and_generation_info_keeps_snapshot(self):
        eid, path = self.open_audio()
        self.until(lambda: self.js('document.getElementById("audio-wave")?.dataset.state') == 'ready')
        self.assertEqual(self.js('document.querySelectorAll("#audio-wave i").length'), 110)
        self.assertEqual(self.js('Math.max(...Array.from(document.querySelectorAll("#audio-wave i"), n=>parseFloat(n.style.height)))'), 2)
        self.js('document.getElementById("audio-info").click()')
        info = self.js('document.getElementById("dialog-body").innerText')
        self.assertIn('测试音色', info)
        self.assertIn('隔离样例', info)
        self.assertIn(path.name, info)
        self.assertNotIn(str(self.root), info)
        self.js('document.getElementById("dialog-close").click()')
        for width, height in [(1448, 917), (1024, 717)]:
            self.widget.resize(width, height)
            self.tick()
            sizes = self.js('JSON.stringify(["audio-wave","audio-play","audio-time"].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.x,r.right,r.height]}))')
            import json
            wave_box, play_box, time_box = json.loads(sizes)
            self.assertEqual(wave_box[2], 140)
            self.assertEqual(play_box[2], 43)
            self.assertLessEqual(time_box[1], width)
        self.widget.call('setKind', 'documents', self.eid)
        self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
        self.assertIsNone(self.widget.waveform_id)
        self.js('window.lulu.waveformState({id:"'+eid+'",state:"ready",peaks:[1],description:"迟到波形"})')
        self.assertFalse(self.js('document.getElementById("detail").innerText.includes("迟到波形")'))

    def test_missing_or_corrupt_waveform_never_draws_sample_bars(self):
        self.open_audio(corrupt=True)
        self.until(lambda: self.js('document.getElementById("audio-wave")?.dataset.state') == 'failed')
        self.assertEqual(self.js('document.querySelectorAll("#audio-wave i").length'), 0)
        self.assertIn('仍可', self.js('document.getElementById("audio-wave").textContent'))
        self.assertFalse(self.js('document.getElementById("audio-play").disabled'))

    def test_index_model_version_uses_record_in_list_and_details(self):
        from src.core.library_query import query
        eid, _ = self.open_audio()
        for engine, version, expected in [('local_indextts2', '2.5', 'IndexTTS 2.5'),
                                           ('local_indextts2', '2.0', 'IndexTTS 2.0'),
                                           ('local_indextts2', None, 'IndexTTS'),
                                           ('IndexTTS 2.5', None, 'IndexTTS 2.5')]:
            with self.subTest(engine=engine, version=version):
                raw = library._load_raw(eid)
                raw['engine'] = engine
                raw['meta'] = {'model_version': version}
                library._write_raw(raw)
                self.assertEqual(query(kind='voice')['entries'][0]['model_version'], version)
                self.widget.refresh()
                self.widget.call('open', None)
                self.until(lambda: self.js('window.lulu.state.doc===null'))
                self.widget.open_entry(eid)
                self.until(lambda: self.js('window.lulu.state.doc?.id') == eid)
                self.until(lambda: self.js('document.querySelector(".voice-description")?.textContent') == '测试音色 · ' + expected)
                self.js('document.getElementById("audio-info").click()')
                self.assertEqual(self.js('document.querySelector("#dialog-body tr td").textContent'), expected)
                self.js('document.getElementById("dialog-close").click()')
