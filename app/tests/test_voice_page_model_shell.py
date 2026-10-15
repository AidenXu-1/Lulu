import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractAnimation, QPoint, QUrl, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from src.core import secrets
from src.core.synthesis import glossary, style_presets, voices
from src.ui import voice_page as voice_page_module
from src.ui.voice_page import EMOTION_MODE_ORDER, VoicePage


class _Cfg:
    def __init__(self, root: Path):
        self.tts_output_dir = str(root / "voices")
        self.last_output_dir = str(root)
        self.tts_model_dir = ""
        self.voice_library_dir = str(root / "voice-library")

    def save(self) -> None:
        pass


class _Main:
    def __init__(self):
        self.toasts: list[str] = []
        self.completed: list[dict] = []
        self.failed = 0

    def _toast(self, _text: str) -> None:
        self.toasts.append(_text)

    def _notify_task_completed(self, **kwargs) -> None:
        self.completed.append(kwargs)

    def _notify_task_failed(self) -> None:
        self.failed += 1


class _SignalProbe:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _FakeAudioOutput:
    """Voice-page tests must never construct the real platform audio backend."""

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


class _FakeIndexTTS2Worker:
    instances = []

    def __init__(self, *args, **kwargs):
        self.progress = _SignalProbe()
        self.args = args
        self.kwargs = kwargs
        self.lifecycle = _SignalProbe()
        self.finished_ok = _SignalProbe()
        self.finished = _SignalProbe()
        self.failed = _SignalProbe()
        self.cancelled = _SignalProbe()
        self.started = False
        _FakeIndexTTS2Worker.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def request_cancel(self):
        pass


def _ready_status():
    return SimpleNamespace(
        runtime_ready=True,
        model_ready=True,
        config_ready=True,
        ready=True,
        missing_items=[],
        config_state="ready",
        runtime_size_bytes=0,
        model_size_bytes=0,
    )


def _unready_status(*, runtime_ready=False, model_ready=False, config_ready=False):
    missing = []
    if not runtime_ready:
        missing.append("runtime")
    if not model_ready:
        missing.append("model")
    if not config_ready:
        missing.append("config")
    return SimpleNamespace(
        runtime_ready=runtime_ready,
        model_ready=model_ready,
        config_ready=config_ready,
        ready=runtime_ready and model_ready and config_ready,
        missing_items=missing,
        config_state="missing",
        runtime_size_bytes=0,
        model_size_bytes=0,
    )


class VoicePageModelShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_library_dir = voice_page_module.library.LIBRARY_DIR
        voice_page_module.library.LIBRARY_DIR = self.root / 'library'
        self._old_style_path = style_presets.style_presets_path
        self._old_glossary_path = glossary.glossary_path
        self._old_voices_dir = voices.VOICES_DIR
        self._old_voices_index = voices.INDEX
        self._old_secret_backend = secrets._backend
        self._old_theme_name = voice_page_module.T.ACTIVE.name
        secrets._backend = lambda: None  # type: ignore[assignment]
        voices.configure_voice_library_dir(self.root / "voice-library")
        style_presets.style_presets_path = lambda: self.root / "synthesis_style_presets.json"  # type: ignore[assignment]
        glossary.glossary_path = lambda: self.root / "synthesis_glossary.json"  # type: ignore[assignment]
        self._old_refresh = VoicePage._refresh_indextts2_status
        VoicePage._refresh_indextts2_status = lambda self, force=False: None  # type: ignore[assignment]
        self._old_indextts2_worker = voice_page_module.IndexTTS2SynthesisWorker
        self._old_media_player = voice_page_module.QMediaPlayer
        self._old_audio_output = voice_page_module.QAudioOutput
        voice_page_module.QMediaPlayer = _FakeMediaPlayer  # type: ignore[assignment]
        voice_page_module.QAudioOutput = _FakeAudioOutput  # type: ignore[assignment]
        self.app = QApplication.instance() or QApplication([])

    def _wait_until(self, predicate, *, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("timed out waiting for VoicePage background state")

    def tearDown(self):
        VoicePage._refresh_indextts2_status = self._old_refresh  # type: ignore[assignment]
        voice_page_module.IndexTTS2SynthesisWorker = self._old_indextts2_worker  # type: ignore[assignment]
        voice_page_module.QMediaPlayer = self._old_media_player  # type: ignore[assignment]
        voice_page_module.QAudioOutput = self._old_audio_output  # type: ignore[assignment]
        voices.VOICES_DIR = self._old_voices_dir
        voices.INDEX = self._old_voices_index
        secrets._backend = self._old_secret_backend  # type: ignore[assignment]
        style_presets.style_presets_path = self._old_style_path  # type: ignore[assignment]
        glossary.glossary_path = self._old_glossary_path  # type: ignore[assignment]
        voice_page_module.T.apply_theme(self._old_theme_name)
        voice_page_module.library.LIBRARY_DIR = self._old_library_dir
        self.tmp.cleanup()


    def test_set_text_strips_transcript_document_metadata_and_aligns_source_snapshot(self):
        page = VoicePage(_Cfg(self.root), _Main())
        document = (
            "# 作品标题\n\n![封面](file:///Users/test/cover.webp)\n\n"
            "## 简介\n\n不应朗读的简介。\n\n"
            "## 逐字稿\n\n真正要配音的正文。\n\n"
            "## 文案拆解\n\n不应朗读的拆解。"
        )

        page.set_text(
            document,
            source={
                "source_type": "transcript",
                "source_id": "entry-1",
                "source_title": "作品标题",
                "source_text_snapshot": document,
                "source_chain": [],
            },
        )

        self.assertEqual(page.indextts2_text_edit.toPlainText(), "真正要配音的正文。")
        self.assertEqual(page.text_edit.toPlainText(), "真正要配音的正文。")
        self.assertEqual(page._voice_source["source_text_snapshot"], "真正要配音的正文。")


if __name__ == "__main__":
    unittest.main()
