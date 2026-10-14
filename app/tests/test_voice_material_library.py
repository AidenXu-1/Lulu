import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from src.core import library, secrets
from src.core.models import Transcript
from src.core.synthesis.base import VoiceProfile
from src.core.synthesis import glossary, style_presets, voices
from src.ui import library_page as library_page_module
from src.ui.library_page import EntryCard, LibraryPage
from src.ui import voice_page as voice_page_module
from src.ui.voice_page import VoicePage


class _SignalProbe:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _FakeAudioOutput:
    def __init__(self, *_args, **_kwargs):
        pass


class _FakeMediaPlayer:
    StoppedState = 0
    PlayingState = 1
    PausedState = 2

    def __init__(self, *_args, **_kwargs):
        self.durationChanged = _SignalProbe()
        self.positionChanged = _SignalProbe()
        self.playbackStateChanged = _SignalProbe()
        self._source = QUrl()
        self._state = self.StoppedState
        self._position = 0
        self._duration = 0

    def setAudioOutput(self, _output):
        pass

    def setSource(self, source):
        self._source = source

    def source(self):
        return self._source

    def stop(self):
        self._state = self.StoppedState
        self.playbackStateChanged.emit(self._state)

    def play(self):
        self._state = self.PlayingState
        self.playbackStateChanged.emit(self._state)

    def pause(self):
        self._state = self.PausedState
        self.playbackStateChanged.emit(self._state)

    def playbackState(self):
        return self._state

    def position(self):
        return self._position

    def duration(self):
        return self._duration

    def setPosition(self, value):
        self._position = int(value)
        self.positionChanged.emit(self._position)


class _Cfg:
    def __init__(self, root: Path):
        self.tts_output_dir = str(root / "voices")
        self.last_output_dir = str(root)
        self.tts_model_dir = ""

    def save(self) -> None:
        pass


class _Main:
    def __init__(self):
        self.library_page = SimpleNamespace(refreshed=False, refresh=self._refresh)
        self.voice_library_opened = False

    def _refresh(self) -> None:
        self.library_page.refreshed = True

    def _toast(self, _text: str) -> None:
        pass

    def open_voice_library(self) -> None:
        self.voice_library_opened = True


class VoiceMaterialLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_library_dir = library.LIBRARY_DIR
        library.LIBRARY_DIR = self.root / "library"
        self.old_secret_backend = secrets._backend
        secrets._backend = lambda: None  # type: ignore[assignment]
        self.old_media_player = voice_page_module.QMediaPlayer
        self.old_audio_output = voice_page_module.QAudioOutput
        self.old_library_media_player = library_page_module.QMediaPlayer
        self.old_library_audio_output = library_page_module.QAudioOutput
        voice_page_module.QMediaPlayer = _FakeMediaPlayer  # type: ignore[assignment]
        voice_page_module.QAudioOutput = _FakeAudioOutput  # type: ignore[assignment]
        library_page_module.QMediaPlayer = _FakeMediaPlayer  # type: ignore[assignment]
        library_page_module.QAudioOutput = _FakeAudioOutput  # type: ignore[assignment]
        self.old_style_path = style_presets.style_presets_path
        self.old_glossary_path = glossary.glossary_path
        style_presets.style_presets_path = lambda: self.root / "synthesis_style_presets.json"  # type: ignore[assignment]
        glossary.glossary_path = lambda: self.root / "synthesis_glossary.json"  # type: ignore[assignment]
        self.old_refresh_status = VoicePage._refresh_indextts2_status
        VoicePage._refresh_indextts2_status = lambda self, force=False: None  # type: ignore[assignment]
        self.old_list_voices = voices.list_voices
        self.old_get_voice = voices.get_voice
        self.voice = VoiceProfile(
            id="voice-1",
            name="测试音色",
            reference_audio_path=str(self.root / "ref.wav"),
            reference_text="参考文本",
        )
        voices.list_voices = lambda: [self.voice]  # type: ignore[assignment]
        voices.get_voice = lambda voice_id: self.voice if voice_id == "voice-1" else None  # type: ignore[assignment]
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        voices.list_voices = self.old_list_voices  # type: ignore[assignment]
        voices.get_voice = self.old_get_voice  # type: ignore[assignment]
        voice_page_module.QMediaPlayer = self.old_media_player  # type: ignore[assignment]
        voice_page_module.QAudioOutput = self.old_audio_output  # type: ignore[assignment]
        library_page_module.QMediaPlayer = self.old_library_media_player  # type: ignore[assignment]
        library_page_module.QAudioOutput = self.old_library_audio_output  # type: ignore[assignment]
        secrets._backend = self.old_secret_backend  # type: ignore[assignment]
        VoicePage._refresh_indextts2_status = self.old_refresh_status  # type: ignore[assignment]
        style_presets.style_presets_path = self.old_style_path  # type: ignore[assignment]
        glossary.glossary_path = self.old_glossary_path  # type: ignore[assignment]
        library.LIBRARY_DIR = self.old_library_dir
        self.tmp.cleanup()

    def test_save_voice_keeps_audio_path_and_reports_missing_source(self):
        audio = self.root / "old-output" / "sample.wav"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"RIFF")

        entry_id = library.save_voice(
            title="手动配音",
            audio_path=audio,
            text="这是一条手动输入的配音文本",
            engine="IndexTTS2",
            voice_name="测试音色",
            config_summary="情感模板:自然",
            source_type="manual",
        )
        entry = library.load_voice(entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["audio_path"], str(audio))
        self.assertFalse(entry["missing_flags"]["audio_missing"])
        self.assertIn("这是一条", entry["text_preview"])

        audio.unlink()
        broken = library.list_voice_entries()[0]
        self.assertTrue(broken["missing_flags"]["audio_missing"])
        self.assertEqual(broken["source_label"], "来源:手动输入")

        missing_source_id = library.save_voice(
            title="改写来源已删",
            audio_path=self.root / "still-here.wav",
            text="来自已删除改写的文本",
            engine="VoxCPM2",
            voice_name="测试音色",
            config_summary="基础生成",
            source_type="rewrite",
            source_id="missing-rewrite",
            source_title="旧改写稿",
        )
        missing = next(e for e in library.list_entries() if e["id"] == missing_source_id)
        self.assertTrue(missing["missing_flags"]["source_missing"])
        self.assertEqual(missing["source_label"], "来源不可用")

    def test_library_write_is_atomic_and_bad_json_is_skipped(self):
        entry_id = library.save(Transcript("正常正文"), "正常素材")
        broken = library.LIBRARY_DIR / "broken.json"
        broken.write_text('{"id": "broken", "title": ', encoding="utf-8")

        entries = library.list_entries()
        self.assertEqual([entry["id"] for entry in entries], [entry_id])
        saved = library.LIBRARY_DIR / f"{entry_id}.json"
        self.assertTrue(saved.exists())
        self.assertFalse(list(library.LIBRARY_DIR.glob("*.tmp")))

    def test_library_page_voice_filter_exposes_voice_cards_and_missing_audio_guard(self):
        audio = self.root / "voice.wav"
        audio.write_bytes(b"RIFF")
        library.save_voice(
            title="素材库配音",
            audio_path=audio,
            text="素材库可见文本",
            engine="IndexTTS2",
            voice_name="测试音色",
            config_summary="情感模板:自然",
            source_type="manual",
        )
        page = LibraryPage(_Cfg(self.root))
        page._set_kind("voice")
        from PySide6.QtTest import QTest
        import time
        deadline = time.monotonic() + 3
        while page._query_worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(page._query_worker)
        self.assertEqual(page.model.rowCount(), 1)
        entry = page.model.rows[0]
        self.assertEqual(entry["kind"], "voice")
        self.assertEqual(entry["text_preview"], "素材库可见文本")
        self.assertEqual(page.findChildren(EntryCard), [])
        self.assertTrue(page.list_view.uniformItemSizes())

        audio.unlink()
        alerts: list[str] = []
        from src.ui import dialogs
        old_alert = dialogs.alert
        dialogs.alert = lambda _parent, _title, msg: alerts.append(msg)  # type: ignore[assignment]
        try:
            self.assertIsNone(page._voice_audio_path_or_alert(library.load_voice(entry["id"])))
        finally:
            dialogs.alert = old_alert  # type: ignore[assignment]
        self.assertTrue(any("音频文件不在原位置" in msg for msg in alerts))

    def test_entry_cards_hide_body_preview_for_transcript_rewrite_and_voice(self):
        transcript_id = library.save(
            Transcript(
                text="转录正文不应出现在卡片",
                segments=[],
                language="zh",
                engine="SenseVoice",
                source="unit",
                title="转录长标题" * 8,
            ),
            "转录长标题" * 8,
        )
        rewrite_id = library.save_rewrite("改写正文不应出现在卡片", "改写标题", source_title="转录长标题")
        voice_id = library.save_voice(
            title="配音标题",
            audio_path=self.root / "missing.wav",
            text="配音正文不应出现在卡片",
            engine="IndexTTS2",
            voice_name="测试音色",
            config_summary="情感模板:自然",
            source_type="manual",
        )

        entries = {entry["id"]: entry for entry in library.list_entries()}
        for entry_id, hidden_text, kind_label in (
            (transcript_id, "转录正文不应出现在卡片", "文稿"),
            (rewrite_id, "改写正文不应出现在卡片", "输出稿"),
            (voice_id, "配音正文不应出现在卡片", "配音素材"),
        ):
            card = EntryCard(entries[entry_id])
            labels_text = "\n".join(lab.text() for lab in card.findChildren(QLabel))
            self.assertIn(kind_label, labels_text)
            self.assertNotIn(hidden_text, labels_text)
            self.assertNotIn("来源:", labels_text)
            self.assertGreaterEqual(card.title_label.minimumHeight(), card.title_label.fontMetrics().height() + 6)
            self.assertIn("", {b.text().strip() for b in card.action_buttons})
            self.assertEqual(card.action_buttons[0].toolTip(), "修改标题")

    def test_rename_persists_for_transcript_rewrite_and_voice_and_rejects_blank(self):
        transcript_id = library.save(
            Transcript(
                text="转录正文",
                segments=[],
                language="zh",
                engine="test",
                source="unit",
                title="转录旧标题",
            ),
            "转录旧标题",
        )
        rewrite_id = library.save_rewrite("改写正文", "改写旧标题", source_title="转录旧标题")
        voice_id = library.save_voice(
            title="配音旧标题",
            audio_path=self.root / "missing.wav",
            text="配音正文",
            engine="IndexTTS2",
            voice_name="测试音色",
            config_summary="自然",
            source_type="manual",
        )

        self.assertEqual(library.rename(transcript_id, "转录新标题"), "转录新标题")
        self.assertEqual(library.rename(rewrite_id, "改写新标题"), "改写新标题")
        self.assertEqual(library.rename(voice_id, "配音新标题"), "配音新标题")

        entries = {entry["id"]: entry for entry in library.list_entries()}
        self.assertEqual(entries[transcript_id]["title"], "转录新标题")
        self.assertEqual(entries[rewrite_id]["title"], "改写新标题")
        self.assertEqual(entries[voice_id]["title"], "配音新标题")
        self.assertEqual(library.load(transcript_id).title, "转录新标题")
        self.assertEqual(library.load_voice(voice_id)["title"], "配音新标题")
        with self.assertRaises(ValueError):
            library.rename(voice_id, "   ")
        self.assertEqual(library.load_voice(voice_id)["title"], "配音新标题")


    def test_voxcpm_done_auto_saves_manual_voice_material(self):
        main = _Main()
        page = VoicePage(_Cfg(self.root), main)
        page.text_edit.setPlainText("手动输入后生成的配音")
        output = self.root / "voices" / "done.wav"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"RIFF")
        result = SimpleNamespace(
            output_path=output,
            duration_seconds=1.25,
            voice_id="voice-1",
            text="手动输入后生成的配音",
            meta={"engine": "fake"},
        )

        page._on_done(result)
        records = library.list_voice_entries()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "voice")
        self.assertEqual(records[0]["source_label"], "来源:手动输入")
        self.assertEqual(records[0]["audio_path"], str(output))
        self.assertTrue(main.library_page.refreshed)


if __name__ == "__main__":
    unittest.main()
