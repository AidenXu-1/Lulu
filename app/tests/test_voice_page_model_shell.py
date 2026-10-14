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

    def test_voice_page_title_uses_section_name(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page_titles = [label.text() for label in page.findChildren(QLabel) if label.objectName() == "pageTitle"]

        self.assertIn("文案配音", page_titles)
        self.assertNotIn("配音", page_titles)

    def test_25_actions_stay_visible_when_workbench_scrolls(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        page.resize(1200, 820)
        page.show()
        self.app.processEvents()
        self.app.processEvents()
        self.assertTrue(page._approved_index.isVisible())
        self.assertEqual(page.indextts2_language_combo.count(), 5)
        self.assertEqual(page.indextts2_speed_slider.value(), 100)
        self.assertFalse(page.indextts2_advanced_toggle.isChecked())
        page._approved_index.sync_busy(True)
        cancel = page._approved_index.busy_cancel
        for width in (1200, 640):
            page.resize(width, 820)
            self.app.processEvents()
            before = cancel.mapTo(page, QPoint())
            page.page_scroll.verticalScrollBar().setValue(page.page_scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertEqual(before, cancel.mapTo(page, QPoint()))
            self.assertTrue(page.rect().contains(cancel.mapTo(page, cancel.rect().bottomRight())))

    def test_25_preview_uses_selection_and_keeps_input_and_previous_result(self):
        voice_page_module.IndexTTS2SynthesisWorker = _FakeIndexTTS2Worker
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        page._indextts2_status = _ready_status()
        page._indextts2_voice_ready = lambda: True
        page._indextts2_background_status_snapshot = lambda: {"alive": False}
        text = "先朗读这一句。下一句留给全文生成。"
        page.indextts2_text_edit.setPlainText(text)
        cursor = page.indextts2_text_edit.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(8, cursor.MoveMode.KeepAnchor)
        page.indextts2_text_edit.setTextCursor(cursor)
        page._indextts2_last_output = str(self.root / "previous.wav")
        page.indextts2_language_combo.setCurrentIndex(1)
        page.indextts2_speed_slider.setValue(120)
        page._update_indextts2_generate_state()
        page.btn_indextts2_preview.click()
        fake = _FakeIndexTTS2Worker.instances[-1]
        self.assertEqual(fake.args[0], text[:8])
        self.assertEqual(fake.kwargs["language"], "EN")
        self.assertEqual(fake.kwargs["speed"], 1.2)
        self.assertTrue(fake.kwargs["preview"])
        self.assertEqual(page.indextts2_text_edit.toPlainText(), text)
        page._on_indextts2_cancelled()
        self.assertEqual(page._indextts2_last_output, str(self.root / "previous.wav"))

    def test_25_preview_requires_selection_and_preserves_reading_annotation(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        text = '文' * 77 + '<行|XING2>走，后文保留。'
        page.indextts2_text_edit.setPlainText(text)
        self.assertEqual(page._indextts2_preview_text(), '')
        page.indextts2_text_edit.selectAll()
        self.assertEqual(page._indextts2_preview_text(), text)

    def test_25_switching_engines_releases_idle_background_only(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        page._indextts2_background_status_snapshot = lambda: {'alive': True}
        with patch.object(page, '_release_indextts2_background') as release:
            page._switch_panel(1)
            release.assert_called_once()
            page._indextts2_worker = SimpleNamespace(isRunning=lambda: True)
            page._switch_panel(0)
            page._switch_panel(1)
            release.assert_called_once()
        page._indextts2_worker = None

    def test_25_preview_plays_when_visible_without_saving_library(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        page.show()
        self.app.processEvents()
        result = SimpleNamespace(output_path=self.root / 'preview.wav', duration_seconds=1.0,
            voice_id='', text='试听', meta={'preview': True, 'model_version': '2.5'})
        with patch.object(page, '_save_voice_safely') as save:
            page._on_indextts2_done(result)
            save.assert_not_called()
        self.assertEqual(page.indextts2_player.playbackState(), _FakeMediaPlayer.PlayingState)

    def test_25_hidden_result_waits_for_thread_exit_before_releasing_background(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.close)
        page._indextts2_worker = SimpleNamespace(isRunning=lambda: True)
        page._indextts2_background_status_snapshot = lambda: {'alive': True}
        result = SimpleNamespace(output_path=self.root / 'preview.wav', duration_seconds=1.0,
            voice_id='', text='试听', meta={'preview': True, 'model_version': '2.5'})
        with patch.object(page, '_release_indextts2_background') as release:
            page._on_indextts2_done(result)
            release.assert_not_called()
            page._indextts2_worker = None
            page._on_indextts2_thread_finished()
            release.assert_called_once()

    def test_25_glossary_edits_explicit_reading_without_changing_source(self):
        page = VoicePage(_Cfg(self.root), _Main())
        panel = page.indextts2_glossary_panel
        panel.source_input.setText("行")
        panel.replacement_input.setText("XING2")
        panel.kind_combo.setCurrentIndex(1)
        panel.save_btn.click()
        self.assertEqual(panel.apply_to_text("行走").text, "<行|XING2>走")
        self.assertEqual(panel.preview_text("行走").text, "<行|XING2>走")

    def test_25_audio_only_voice_needs_no_transcription_or_hidden_engine_preparation(self):
        page = VoicePage(_Cfg(self.root), _Main())
        reference = self.root / 'reference.wav'
        reference.write_bytes(b'isolated reference fixture')
        def save_dialog(dialog):
            from PySide6.QtWidgets import QPushButton
            next(button for button in dialog.findChildren(QPushButton) if button.text() == '保存音色').click()
            return voice_page_module.QDialog.Accepted
        with patch.object(voice_page_module.QDialog, 'exec', save_dialog), \
             patch.object(voice_page_module, 'ReferenceTranscribeWorker') as transcribe:
            page._new_voice_from_path(str(reference), target_combo=page.indextts2_voice_combo)
            self.assertTrue(page._indextts2_voice_ready())
            transcribe.assert_not_called()
            page._switch_panel(1)
        page.shutdown_workers()
        page.close()

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

    def test_picking_transcript_material_strips_document_metadata(self):
        page = VoicePage(_Cfg(self.root), _Main())
        st = page._src["indextts2"]
        st["picked"] = {"id": "entry-1", "title": "作品标题", "kind": "transcript"}
        document = "# 作品标题\n\n## 简介\n\n简介。\n\n## 逐字稿\n\n真正正文。"
        transcript = SimpleNamespace(text=document)

        with patch.object(voice_page_module.library, "load", return_value=transcript):
            page._src_sync_picked_text("indextts2")

        self.assertEqual(st["text_edit"].toPlainText(), "真正正文。")
        self.assertEqual(page._voice_source["source_text_snapshot"], "真正正文。")

    def test_picking_invalid_transcript_clears_previous_voice_text(self):
        page = VoicePage(_Cfg(self.root), _Main())
        st = page._src["indextts2"]
        st["text_edit"].setPlainText("上一条有效文案。")
        page._set_voice_source({
            "source_type": "manual",
            "source_title": "上一条",
            "source_text_snapshot": "上一条有效文案。",
        })
        st["picked"] = {"id": "broken", "title": "缺少正文", "kind": "transcript"}
        transcript = SimpleNamespace(text="# 作品标题\n\n## 简介\n\n没有逐字稿正文。")

        with (
            patch.object(voice_page_module.library, "load", return_value=transcript),
            patch.object(voice_page_module.dialogs, "alert"),
        ):
            page._src_sync_picked_text("indextts2")

        self.assertEqual(st["text_edit"].toPlainText(), "")
        self.assertEqual(page._voice_source["source_id"], "broken")
        self.assertEqual(page._voice_source["source_text_snapshot"], "")

    def test_invalid_structured_transcript_clears_previous_voice_text(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.set_text("上一条有效文案。")

        accepted = page.set_text(
            "# 作品标题\n\n## 简介\n\n没有逐字稿正文。",
            source={"source_type": "transcript", "source_id": "broken"},
        )

        self.assertFalse(accepted)
        self.assertEqual(page.indextts2_text_edit.toPlainText(), "")
        self.assertEqual(page.text_edit.toPlainText(), "")

    def test_voice_library_is_centered_theme_modal_and_all_dismiss_paths_restore_shell(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                voice_page_module.T.apply_theme(theme_name)
                shell = QWidget()
                shell.resize(1040, 760)
                host = QWidget(shell)
                shell.show()
                self.app.processEvents()

                def shown_dialog():
                    dialog = voice_page_module._VoiceLibraryModal(host)
                    dialog.setAttribute(Qt.WA_DeleteOnClose, False)
                    dialog.show()
                    self.app.processEvents()
                    return dialog

                dialog = shown_dialog()
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.isModal())
                self.assertLess(dialog.width(), shell.width())
                self.assertLess(dialog.height(), shell.height())
                shell_center = shell.mapToGlobal(shell.rect().center())
                dialog_center = dialog.frameGeometry().center()
                self.assertLessEqual(abs(dialog_center.x() - shell_center.x()), 1)
                self.assertLessEqual(abs(dialog_center.y() - shell_center.y()), 1)
                self.assertIsNotNone(shell.graphicsEffect())
                self.assertTrue(dialog.backdrop.isVisible())
                self.assertEqual(dialog.backdrop.size(), shell.size())
                surface_image = dialog.surface.grab().toImage()
                surface_pixel = surface_image.pixelColor(
                    surface_image.width() // 2,
                    surface_image.height() // 2,
                )
                self.assertEqual(surface_pixel.alpha(), 255)
                self.assertEqual(surface_pixel.name().lower(), voice_page_module.T.CARD.lower())

                QTest.keyClick(dialog, Qt.Key_Escape)
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertIsNone(shell.graphicsEffect())

                dialog = shown_dialog()
                QTest.mouseClick(dialog.close_button, Qt.LeftButton)
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertIsNone(shell.graphicsEffect())

                dialog = shown_dialog()
                QTest.mouseClick(dialog.backdrop, Qt.LeftButton, pos=QPoint(8, 8))
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertIsNone(shell.graphicsEffect())
                shell.close()

        voice_page_module.T.apply_theme("warm")
        shell = QWidget()
        shell.resize(520, 420)
        host = QWidget(shell)
        shell.show()
        dialog = voice_page_module._VoiceLibraryModal(host)
        dialog.setAttribute(Qt.WA_DeleteOnClose, False)
        dialog.show()
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), shell.width() - 36)
        self.assertLessEqual(dialog.height(), shell.height() - 36)
        dialog.reject()
        shell.close()

    def test_voice_library_small_window_keeps_voice_list_inside_scroll_and_preserves_selection(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.resize(520, 420)
        page.show()
        sample_voices = [
            SimpleNamespace(
                id=f"voice-{index}",
                name=f"本机音色 {index + 1}",
                reference_text="",
                reference_audio_path="",
            )
            for index in range(18)
        ]
        original_index = page.indextts2_voice_combo.currentIndex()
        with patch("src.ui.voice_page.voices.list_voices", return_value=sample_voices):
            page._open_voice_picker(page.indextts2_voice_combo)
            self.app.processEvents()
            dialog = page._voice_library_dialog
            self.assertIsNotNone(dialog)
            self.assertLessEqual(dialog.width(), page.width() - 36)
            self.assertLessEqual(dialog.height(), page.height() - 36)
            self.assertTrue(dialog.voice_scroll.widgetResizable())
            self.assertEqual(
                dialog.voice_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarAlwaysOff,
            )
            self.assertGreater(dialog.voice_scroll.verticalScrollBar().maximum(), 0)
            QTest.keyClick(dialog, Qt.Key_Escape)
            self.app.processEvents()
        self.assertEqual(page.indextts2_voice_combo.currentIndex(), original_index)
        self.assertIsNone(page.graphicsEffect())
        page.close()

    def test_default_engine_is_indextts2_and_panels_switch_cleanly(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page.show()
        self.app.processEvents()

        # 默认引擎为 IndexTTS 2.5(mock4 设计:情感配音在前且选中)
        self.assertEqual(page._active_model_name(), "IndexTTS 2.5")
        self.assertEqual(page.panel_stack.currentWidget().objectName(), "indextts2ModelPanel")

        # 切到 VoxCPM2 裸克隆面板
        page._switch_panel(1)
        self.app.processEvents()
        self.assertEqual(page._active_model_name(), "VoxCPM2")
        self.assertEqual(page.panel_stack.currentWidget().objectName(), "voxcpm2ModelPanel")
        self.assertTrue(hasattr(page, "text_edit"))

        page._switch_panel(2)
        self.app.processEvents()
        self.assertEqual(page._active_model_name(), "MiniMax")
        self.assertEqual(page.panel_stack.currentWidget().objectName(), "minimaxModelPanel")
        self.assertTrue(hasattr(page, "minimax_text_edit"))

        # 切回 IndexTTS 2.5
        page._switch_panel(0)
        self.app.processEvents()
        self.assertEqual(page.panel_stack.currentWidget().objectName(), "indextts2ModelPanel")

    def test_engine_selector_is_responsive_accessible_and_keyboard_driven(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.setStyleSheet(voice_page_module.T.QSS + page._extra_qss())
        page.resize(668, 900)  # 24px×2 页边距后正好是 620px 有效中栏
        page.show()
        self.app.processEvents()

        selector = page.engine_select
        self.assertEqual(selector.accessibleName(), "配音引擎")
        self.assertEqual(selector.trigger.objectName(), "consumerSelectField")
        self.assertEqual(
            selector.options,
            (
                ("0", "自然情感 · IndexTTS 2.5"),
                ("3", "经典情感 · IndexTTS 2"),
                ("1", "声音克隆 · VoxCPM2"),
                ("2", "云端音色 · MiniMax"),
                ("4", "多语言声音 · Qwen3-TTS"),
            ),
        )
        self.assertEqual(selector.currentData(), "0")
        self.assertEqual(selector.corner_radius, 15)
        self.assertEqual(selector.popup_radius, 18)
        self.assertTrue(selector.clip_popup_corners)
        self.assertEqual(selector.height(), 44)
        self.assertEqual(selector.property("voiceSelectVisual"), "softRoundedCard")
        self.assertEqual(len(page.engine_buttons), 5)
        self.assertTrue(all(button.isHidden() for button in page.engine_buttons))
        self.assertLessEqual(selector.geometry().right(), page.engine_switch_box.rect().right())

        selector.trigger.setFocus(Qt.TabFocusReason)
        QTest.keyClick(selector.trigger, Qt.Key_Space)
        self.app.processEvents()
        self.assertTrue(selector.popup.isVisible())
        self.assertFalse(selector.popup.mask().isEmpty())
        QTest.keyClick(selector.popup, Qt.Key_Down)
        QTest.keyClick(selector.popup, Qt.Key_Return)
        self.app.processEvents()
        self.assertEqual(page.panel_stack.currentIndex(), 0)
        self.assertEqual(page._index_version, "2.0")
        self.assertEqual(selector.currentData(), "3")
        self.assertTrue(page.engine_buttons[3].isChecked())

        selector.set_value("2", emit=True)
        self.assertEqual(page.panel_stack.currentIndex(), 2)
        self.assertEqual(selector.currentData(), "2")

    def test_page_trust_badge_tracks_active_engine_privacy(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.show()
        self.app.processEvents()

        for index in (0, 1):
            page._switch_panel(index)
            self.assertEqual(page.page_privacy_badge.text(), "● 本地离线 · 不上传")
            self.assertFalse(bool(page.page_privacy_badge.property("cloud")))

        page._switch_panel(2)
        self.assertEqual(page.page_privacy_badge.text(), "● 云端生成 · 文本会上传")
        self.assertTrue(bool(page.page_privacy_badge.property("cloud")))

        page._switch_panel(0)
        self.assertEqual(page.page_privacy_badge.text(), "● 本地离线 · 不上传")

    def test_engine_readiness_uses_semantic_status_without_changing_truth(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.assertEqual(page.indextts2_ready_chip.property("statusKind"), "info")
        self.assertEqual(page.voxcpm2_ready_chip.property("statusKind"), "info")
        self.assertEqual(page.minimax_ready_chip.property("statusKind"), "info")

        page._render_indextts2_status(_unready_status(runtime_ready=True))
        self.assertEqual(page.indextts2_ready_chip.text(), "● 未就绪")
        self.assertEqual(page.indextts2_ready_chip.property("statusKind"), "warning")
        self.assertEqual(page.indextts2_status_banner.property("statusKind"), "warning")
        self.assertEqual(page.indextts2_runtime_status.property("statusKind"), "success")
        self.assertEqual(page.indextts2_model_status.property("statusKind"), "warning")

        page._render_indextts2_status(_ready_status())
        self.assertEqual(page.indextts2_ready_chip.text(), "● AI大模型就绪")
        self.assertEqual(page.indextts2_ready_chip.property("statusKind"), "success")
        self.assertEqual(page.indextts2_status_banner.property("statusKind"), "success")
        self.assertTrue(page.indextts2_status_banner.isHidden())

        page._on_indextts2_status_failed("模拟检查失败")
        self.assertEqual(page.indextts2_ready_chip.property("statusKind"), "error")
        self.assertEqual(page.indextts2_status_banner.property("statusKind"), "error")
        self.assertIn("模拟检查失败", page.indextts2_status_detail.text())

    def test_invalid_panel_indices_clamp_to_known_engine_and_privacy(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.shutdown_workers)
        self.addCleanup(page.close)
        page._switch_panel(999)
        self.assertEqual(page._active_model_name(), "Qwen3-TTS")
        self.assertIn("不上传", page.page_privacy_badge.text())
        page._switch_panel(-1)
        self.assertEqual(page.panel_stack.currentWidget().objectName(), "indextts2ModelPanel")
        self.assertIn("不上传", page.page_privacy_badge.text())

    def test_model_context_follows_remaining_voice_engines_and_emits_changes(self):
        page = VoicePage(_Cfg(self.root), _Main())
        events = []
        page.model_context_changed.connect(events.append)

        page._indextts2_status = _ready_status()
        page._switch_panel(0)
        self.assertEqual(events[-1].model_name, "IndexTTS 2.5")
        self.assertEqual(events[-1].status_text, "本地模型已就绪")

        with (
            patch("src.ui.voice_page.local_models.is_downloaded", return_value=False),
            patch("src.core.synthesis.runtime_packages.status", return_value="not_installed"),
        ):
            page._switch_panel(1)
            self.assertEqual(events[-1].model_name, "VoxCPM2")
            self.assertEqual(events[-1].status_text, "本地模型未就绪")

        with patch("src.ui.voice_page.minimax_tts.is_configured", return_value=True):
            page._switch_panel(2)
            page._request_minimax_config_check()
            self._wait_until(lambda: page._minimax_config_state == "configured")
            self.assertEqual(events[-1].model_name, page.minimax_model_combo.currentData())
            self.assertEqual(events[-1].status_text, "大模型 API 已配置")
            self.assertEqual((events[-1].purpose_index, events[-1].sub_index), (1, 1))
        page.shutdown_workers()

    def test_voxcpm2_context_requires_both_weights_and_runtime(self):
        page = VoicePage(_Cfg(self.root), _Main())
        self.addCleanup(page.shutdown_workers)

        with (
            patch("src.ui.voice_page.local_models.is_downloaded", return_value=True),
            patch("src.core.synthesis.runtime_packages.status", return_value="ready"),
        ):
            page._switch_panel(1)
            self._wait_until(lambda:not page._model_status_reads)
            self.assertEqual(page.current_model_context().status_text, "本地模型已就绪")

        with (
            patch("src.ui.voice_page.local_models.is_downloaded", return_value=True),
            patch("src.core.synthesis.runtime_packages.status", return_value="runtime_only"),
        ):
            page.invalidate_model_status();page.current_model_context()
            self._wait_until(lambda:not page._model_status_reads)
            self.assertEqual(page.current_model_context().status_text, "本地模型需修复")

        with (
            patch("src.ui.voice_page.local_models.is_downloaded", return_value=True),
            patch("src.core.synthesis.runtime_packages.status", side_effect=RuntimeError("runtime damaged")),
        ):
            page.invalidate_model_status();page.current_model_context()
            self._wait_until(lambda:not page._model_status_reads)
            self.assertEqual(page.current_model_context().status_text, "本地模型需修复")

    def test_stage2b2_shared_sources_are_accessible_and_keyboard_driven(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.resize(688, 900)
        page.show()
        self.app.processEvents()
        opened = []
        picked_files = []
        page._src_open_picker = lambda prefix: opened.append(prefix)  # type: ignore[method-assign]
        page._src_pick_file = lambda prefix: picked_files.append(prefix)  # type: ignore[method-assign]

        for panel_index, prefix in enumerate(("indextts2", "voxcpm2", "minimax")):
            page._switch_panel(panel_index)
            self.app.processEvents()
            state = page._src[prefix]
            self.assertFalse(state["segment"].isHidden())
            self.assertEqual(state["segment"].objectName(), "luluSegment")
            self.assertEqual(state["segment"].property("sourceTabsVisual"), "consumerSegment")
            self.assertEqual(state["segment"].property("luluControl"), "segment")
            self.assertEqual(tuple(state["buttons"]), ("library", "upload", "custom"))
            self.assertTrue(all(not button.isHidden() for button in state["buttons"].values()))
            self.assertTrue(
                all(
                    button.objectName() == "luluSegmentItem"
                    for button in state["buttons"].values()
                )
            )
            self.assertTrue(
                all(
                    button.property("luluControl") == "segmentItem"
                    for button in state["buttons"].values()
                )
            )
            self.assertTrue(all(button.isCheckable() for button in state["buttons"].values()))
            self.assertTrue(all(button.accessibleName() for button in state["buttons"].values()))
            self.assertEqual(state["text_edit"].property("luluControl"), "textArea")
            self.assertTrue(state["text_edit"].accessibleName())
            self.assertEqual(state["drop_area"].property("surfaceVariant"), "paperDocumentBox")
            self.assertEqual(
                state["drop_area"].property("uploadVisual"),
                "topDownPaperDocumentBox",
            )
            self.assertEqual(
                state["drop_area"].property("uploadMaterial"),
                "layeredMatteCardboard",
            )
            self.assertEqual(state["drop_area"].property("uploadScaling"), "cachedHeightThreeSlice")
            self.assertTrue(state["drop_area"].property("paperBoxEdgeContinuity"))
            self.assertEqual(
                state["drop_area"].property("uploadCenterRendering"),
                "highResolutionPaperLayer",
            )
            self.assertTrue(state["drop_area"].property("uploadAssetLoaded"))
            pieces = state["drop_area"]._paper_box_height_slices(150)
            self.assertEqual(len(pieces), 3)
            self.assertIs(pieces, state["drop_area"]._paper_box_height_slices(150))
            self.assertTrue(all(piece.height() == 150 for piece in pieces))
            self.assertTrue(state["drop_area"].accessibleName())
            trigger = state["material_trigger"]
            self.assertIsInstance(trigger, voice_page_module._PaperEnvelopePicker)
            self.assertEqual(trigger.objectName(), "luluSurface")
            self.assertEqual(trigger.property("luluControl"), "surface")
            self.assertEqual(trigger.property("surfaceVariant"), "sourcePicker")
            self.assertEqual(trigger.property("sourceVisual"), "responsivePaperEnvelope")
            self.assertEqual(trigger.property("sourceMaterial"), "blueCottonPaperNaturalGrain")
            self.assertEqual(trigger.property("sourceStyle"), "referencePaperEnvelope")
            self.assertEqual(trigger.property("sourceScaling"), "stretchBodyFixedCentreTie")
            self.assertEqual(trigger.height(), 152 if prefix == "indextts2" else voice_page_module.VOICE_SOURCE_CANVAS_HEIGHT)
            self.assertEqual(state["library_picker_title"].text(), "选择一篇文稿")
            self.assertEqual(state["library_picker_meta"].text(), "从文稿库中选择内容进行配音")
            self.assertIs(trigger, state["pick_empty"])
            self.assertTrue(trigger.accessibleName())

            state["buttons"]["library"].click()
            self.app.processEvents()
            self.assertEqual(state["src_stack"].currentIndex(), 0)
            trigger.setFocus(voice_page_module.Qt.TabFocusReason)
            QTest.keyClick(trigger, voice_page_module.Qt.Key_Return)
            self.assertEqual(opened[-1], prefix)

            state["buttons"]["upload"].click()
            self.assertEqual(state["src_stack"].currentIndex(), 1)
            self.assertTrue(state["drop_area"].isVisible())
            QTest.mouseClick(state["drop_area"], Qt.LeftButton)
            self.assertEqual(picked_files[-1], prefix)

            state["buttons"]["custom"].click()
            self.assertEqual(state["src_stack"].currentIndex(), 2)
            self.assertTrue(state["text_edit"].isVisible())
            sample = f"{prefix} 键盘文本"
            state["text_edit"].setPlainText(sample)
            self.assertEqual(state["text_edit"].toPlainText(), sample)

        self.assertEqual(page.indextts2_emoaudio_drop.property("surfaceVariant"), "paperDocumentBox")
        self.assertEqual(page.indextts2_emoaudio_drop.title.text(), "拖入情感参考音频")
        self.assertTrue(page.indextts2_emoaudio_drop.accessibleName())

    def test_library_envelope_opens_nonblocking_picker_blurs_full_shell_and_closes_with_escape(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.resize(668, 900)
        page.show()
        self.app.processEvents()

        state = page._src["indextts2"]
        page._src_set_mode("indextts2", "library")
        with patch("src.ui.voice_page.library.list_entries", return_value=[]):
            QTest.mouseClick(state["material_trigger"], Qt.LeftButton)
            self.app.processEvents()
        dialog = state["picker_dialog"]
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.isModal())
        self.assertIsNotNone(page.window().graphicsEffect())
        self.assertIsNotNone(state["picker_backdrop"])

        QTest.keyClick(dialog, Qt.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(page.window().graphicsEffect())
        self.assertIsNone(state["picker_dialog"])
        self.assertIsNone(state["picker_backdrop"])

    def test_voice_consumer_surfaces_are_opaque_and_fit_620_in_paper(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                voice_page_module.T.apply_theme(theme_name)
                page = VoicePage(_Cfg(self.root), _Main())
                page.setStyleSheet(voice_page_module.T.QSS + page._extra_qss())
                page.resize(668, 900)
                page.show()
                self.app.processEvents()

                self.assertEqual(
                    page.engine_select.trigger.palette().color(QPalette.Button).alpha(),
                    255,
                )
                self.assertEqual(page.page_scroll.horizontalScrollBar().maximum(), 0)
                page._src_set_mode("indextts2", "library")
                self.app.processEvents()
                for widget in (
                    page._src["indextts2"]["material_trigger"],
                    page._voice_sel[id(page.indextts2_voice_combo)]["root"],
                    *page.indextts2_emotion_mode_buttons.values(),
                ):
                    image = widget.grab().toImage()
                    self.assertFalse(image.isNull())
                    center = image.pixelColor(max(0, image.width() // 2), max(0, image.height() // 2))
                    self.assertEqual(center.alpha(), 255)
                page.close()

    def test_stage2b2_voice_and_minimax_fields_preserve_values_and_caret(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.resize(688, 900)
        page.show()
        self.app.processEvents()

        long_name = "这是一个用于验证单行省略与完整提示的超长本机音色名称-不会改变真实音色ID"
        for panel_index, combo in (
            (0, page.indextts2_voice_combo),
            (1, page.voice_combo),
        ):
            combo.addItem(long_name, f"voice-{panel_index}")
            combo.setCurrentIndex(combo.count() - 1)
            page._switch_panel(panel_index)
            self.app.processEvents()
            refs = page._voice_sel[id(combo)]
            self.assertEqual(combo.currentData(), f"voice-{panel_index}")
            selector = refs["selector"]
            self.assertEqual(selector.currentData(), f"voice-{panel_index}")
            self.assertEqual(selector.currentText(), long_name)
            self.assertEqual(selector.trigger.objectName(), "consumerSelectField")
            self.assertTrue(selector.trigger.toolTip())
            self.assertEqual(refs["manage"].property("luluControl"), "button")
            self.assertEqual(refs["manage"].accessibleName(), "打开音色库")
            self.assertEqual(refs["root"].height(), 42)
            self.assertEqual(refs["root"].property("voiceState"), "selected")

            alternate_id = f"voice-alt-{panel_index}"
            combo.addItem("备选音色", alternate_id)
            page._update_voice_selector(combo)
            refs["selector"].set_value(alternate_id, emit=True)
            self.assertEqual(combo.currentData(), alternate_id)

        page._switch_panel(2)
        self.app.processEvents()
        combo = page.minimax_official_voice_combo
        original = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
        self.assertGreaterEqual(len(original), 2)
        self.assertEqual(combo.property("luluControl"), "selectField")
        self.assertEqual(combo.accessibleName(), "MiniMax官方音色")
        caret = combo.findChild(QLabel, "luluSelectChevron")
        self.assertIsNotNone(caret)
        self.assertTrue(caret.pixmap() is not None and not caret.pixmap().isNull())

        changes = []
        combo.currentIndexChanged.connect(lambda index: changes.append(index))
        combo.setFocus(voice_page_module.Qt.TabFocusReason)
        QTest.keyClick(combo, voice_page_module.Qt.Key_Home)
        QTest.keyClick(combo, voice_page_module.Qt.Key_Down)
        self.app.processEvents()
        self.assertTrue(changes)
        self.assertEqual([(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())], original)
        combo.showPopup()
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        QTest.keyClick(combo.view(), voice_page_module.Qt.Key_Escape)
        self.app.processEvents()
        self.assertFalse(combo.view().isVisible())

        page.btn_minimax_voice_custom.click()
        page.minimax_voice_id.setText("custom-voice-id")
        self.assertEqual(page.minimax_voice_stack.currentIndex(), 2)
        self.assertEqual(page.minimax_voice_id.property("luluControl"), "textField")
        self.assertEqual(page.minimax_voice_id.text(), "custom-voice-id")


    def test_voice_library_manage_button_opens_adaptive_modal_and_all_dismiss_paths_restore_shell(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.resize(668, 560)
        page.show()
        self.app.processEvents()
        refs = page._voice_sel[id(page.indextts2_voice_combo)]
        many_voices = [
            SimpleNamespace(
                id=f"voice-{index}",
                name=f"测试音色 {index + 1}",
                reference_text="",
                reference_audio_path="",
            )
            for index in range(14)
        ]

        def open_dialog():
            with patch("src.ui.voice_page.voices.list_voices", return_value=many_voices):
                QTest.mouseClick(refs["manage"], Qt.LeftButton)
            self.app.processEvents()
            dialog = page._voice_library_dialog
            self.assertIsNotNone(dialog)
            self.assertTrue(dialog.isVisible())
            self.assertTrue(dialog.isModal())
            self.assertIsNotNone(page.window().graphicsEffect())
            self.assertLessEqual(dialog.surface.width(), dialog.width() - 44)
            self.assertLessEqual(dialog.surface.height(), dialog.height() - 44)
            self.assertEqual(dialog.voice_scroll.horizontalScrollBar().maximum(), 0)
            self.assertGreater(dialog.voice_scroll.verticalScrollBar().maximum(), 0)
            return dialog

        dialog = open_dialog()
        QTest.mouseClick(dialog.close_button, Qt.LeftButton)
        self.app.processEvents()
        self.assertIsNone(page.window().graphicsEffect())

        dialog = open_dialog()
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(page.window().graphicsEffect())

        dialog = open_dialog()
        QTest.mouseClick(dialog, Qt.LeftButton, pos=QPoint(5, 5))
        self.app.processEvents()
        self.assertIsNone(page.window().graphicsEffect())

        existing_effect = voice_page_module.QGraphicsBlurEffect(page)
        existing_effect.setBlurRadius(2.0)
        page.setGraphicsEffect(existing_effect)
        dialog = open_dialog()
        self.assertIs(page.graphicsEffect(), existing_effect)
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.app.processEvents()
        self.assertIs(page.graphicsEffect(), existing_effect)
        page.setGraphicsEffect(None)
        page.close()

    def test_voice_library_modal_and_selector_remain_opaque_in_paper(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                voice_page_module.T.apply_theme(theme_name)
                page = VoicePage(_Cfg(self.root), _Main())
                page.resize(668, 560)
                page.show()
                self.app.processEvents()
                refs = page._voice_sel[id(page.indextts2_voice_combo)]
                selector_image = refs["selector"].trigger.grab().toImage()
                self.assertEqual(
                    selector_image.pixelColor(selector_image.width() // 2, selector_image.height() // 2).alpha(),
                    255,
                )
                QTest.mouseClick(refs["manage"], Qt.LeftButton)
                self.app.processEvents()
                dialog = page._voice_library_dialog
                surface_image = dialog.surface.grab().toImage()
                self.assertEqual(
                    surface_image.pixelColor(surface_image.width() // 2, surface_image.height() // 2).alpha(),
                    255,
                )
                QTest.keyClick(dialog, Qt.Key_Escape)
                self.app.processEvents()
                page.close()

    def test_idle_generation_result_tracks_are_hidden_until_real_progress(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.show()
        self.app.processEvents()
        bars = (
            page.indextts2_progress,
            page.progress,
            page.minimax_progress,
        )
        self.assertTrue(all(bar.isHidden() for bar in bars))

        page._set_voice_progress_running(page.indextts2_progress, page.indextts2_status, "生成中", 0.2)
        self.app.processEvents()
        self.assertTrue(page.indextts2_progress.isVisible())
        page._set_voice_progress_stopped(page.indextts2_progress)
        self.assertTrue(page.indextts2_progress.isHidden())
        page.close()

    def test_stage2b2_source_controls_fit_the_620_component_contract(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.setStyleSheet(voice_page_module.T.QSS + page._extra_qss())
        page.resize(668, 900)
        page.show()
        self.app.processEvents()
        snapshots = []
        for panel_index, prefix in ((0, "indextts2"), (1, "voxcpm2"), (2, "minimax")):
            page._switch_panel(panel_index)
            page._src_set_mode(prefix, "library")
            self.app.processEvents()
            stack = page._src[prefix]["src_stack"]
            panel = page.panel_stack.widget(panel_index)
            snapshots.append(
                (
                    panel.width(),
                    stack.mapTo(panel, QPoint(0, 0)).x(),
                    stack.width(),
                    stack.height(),
                    stack.minimumSizeHint().width(),
                )
            )
            trigger = page._src[prefix]["material_trigger"]
            self.assertGreaterEqual(trigger.geometry().left(), 0)
            self.assertLessEqual(trigger.geometry().right(), stack.geometry().right())

        self.assertEqual(
            {height for _panel_width, _x, _width, height, _minimum in snapshots},
            {152, voice_page_module.VOICE_SOURCE_CANVAS_HEIGHT},
        )
        for (panel_index, prefix), snapshot in zip(
            ((0, "indextts2"), (1, "voxcpm2"), (2, "minimax")), snapshots
        ):
            self.assertLessEqual(snapshot[0], 620, (panel_index, prefix, snapshot))
        self.assertLessEqual(max(minimum for _panel_width, _x, _width, _height, minimum in snapshots), 580)
        for index, (panel_width, x, width, _height, _minimum) in enumerate(snapshots):
            margin = 16 if index == 0 else 20
            self.assertGreaterEqual(x, margin)
            self.assertLessEqual(abs((x + width) - (panel_width - margin)), 1)
        material = page._src["indextts2"]["material_trigger"]
        segment = page._src["indextts2"]["segment"]
        self.assertEqual(material.height(), 152)
        self.assertEqual(material.height(), page._src["indextts2"]["src_stack"].height())
        self.assertIsInstance(material, voice_page_module._PaperEnvelopePicker)
        self.assertEqual(material.property("sourceVisual"), "responsivePaperEnvelope")
        for width in (580, 960):
            material.resize(width, 152)
            self.app.processEvents()
            image = material.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertEqual((image.width(), image.height()), (width, 152))
        page._src_set_mode("indextts2", "upload")
        self.app.processEvents()
        self.assertEqual(
            page._src["indextts2"]["drop_area"].height(),
            152,
        )
        self.assertGreaterEqual(
            page._src["indextts2"]["src_stack"].height(),
            page._src["indextts2"]["drop_area"].minimumHeight(),
        )
        self.assertEqual(segment.height(), 36)
        self.assertEqual(
            segment.mapTo(page, segment.rect().center()).y(),
            page.btn_indextts2_glossary.mapTo(page, page.btn_indextts2_glossary.rect().center()).y(),
        )

    def test_stage2b3_parameter_controls_preserve_values_mapping_and_keyboard_contract(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.setStyleSheet(voice_page_module.T.QSS + page._extra_qss())
        page.resize(688, 900)
        page.show()
        self.app.processEvents()

        # MiniMax：原文本解析/夹取和 model/format data 保持原值。
        self.assertEqual(page.minimax_model_combo.property("luluControl"), "selectField")
        self.assertEqual(page.minimax_format_combo.property("luluControl"), "selectField")
        self.assertEqual(page.minimax_speed.property("luluControl"), "textField")
        self.assertEqual(page.minimax_volume.property("luluControl"), "textField")
        self.assertEqual(page.minimax_speed.text(), "1.0")
        self.assertEqual(page.minimax_volume.text(), "1.0")
        for raw, expected in (("", 1.0), ("0.1", 0.5), ("1.0", 1.0), ("2.5", 2.0), ("bad", 1.0)):
            page.minimax_speed.setText(raw)
            page.minimax_volume.setText(raw)
            self.assertEqual(page._minimax_speed_value(), expected)
            self.assertEqual(page._minimax_volume_value(), expected)
        page.minimax_speed.setText("1.0")
        page.minimax_volume.setText("1.0")

        # IndexTTS 2.5：五种方式、整体情感权重和8维向量的值域/步长/载荷保持统一合同。
        self.assertEqual(tuple(page.indextts2_emotion_mode_buttons), EMOTION_MODE_ORDER)
        for key, button in page.indextts2_emotion_mode_buttons.items():
            self.assertEqual(button.objectName(), "voiceEmotionChoice")
            self.assertEqual(button.height(), 44 if key == "template" else 34)
            self.assertTrue(button.accessibleName())
        self.assertEqual(
            [button.text() for button in page.indextts2_emotion_mode_buttons.values()],
            ["跟随音色", "参考音频", "精细调节", "情绪模板", "文字描述"],
        )
        section_titles = [label.text() for label in page.findChildren(QLabel) if label.objectName() == "voiceSectionTitle"]
        self.assertIn("情绪表达", page._approved_index.emotion_title.text())
        all_label_text = [label.text() for label in page.findChildren(QLabel)]
        self.assertNotIn("选一种方式控制语气与情感", all_label_text)
        self.assertFalse(any("请确保你有权使用所选声音" in text for text in all_label_text))
        weight = page.indextts2_weight_slider
        self.assertEqual(weight.property("luluControl"), "slider")
        self.assertEqual((weight.minimum(), weight.maximum(), weight.singleStep(), weight.pageStep()), (0, 100, 5, 5))
        self.assertEqual(weight.height(), 28)
        self.assertEqual(page.indextts2_weight_title.text(), "整体情感权重")
        self.assertEqual(weight.accessibleName(), "IndexTTS 2.5整体情感权重")
        self.assertEqual(weight.value(), 80)
        page._set_indextts2_advanced_weight(0.1)
        self.assertEqual(page._current_indextts2_advanced_weight(), 0.1)
        page._set_indextts2_advanced_weight(1.4)
        self.assertEqual(page._current_indextts2_advanced_weight(), 1.0)
        page._set_indextts2_advanced_weight(0.8)
        self.assertEqual(
            page.indextts2_weight_value.text(),
            f"{int(round(page._current_indextts2_advanced_weight() * 100))}%",
        )
        self.assertEqual(page.indextts2_emo_surface.property("surfaceVariant"), "emotionStudio")
        self.assertEqual(page.indextts2_emo_surface.height(), 260)
        self.assertEqual(page.indextts2_emotion_stack.count(), 5)
        self.assertEqual(page.indextts2_follow_footer.height(), page.indextts2_weight_row.height())
        self.assertTrue(page.btn_indextts2_template_manage.accessibleName())
        self.assertEqual(page.indextts2_template_name.objectName(), "voiceTemplateName")
        for index, vector_slider in enumerate(page.indextts2_advanced_sliders):
            self.assertEqual(vector_slider.property("luluControl"), "slider")
            self.assertEqual((vector_slider.minimum(), vector_slider.maximum(), vector_slider.singleStep(), vector_slider.pageStep()), (0, 80, 1, 5))
            self.assertEqual(vector_slider.height(), 28)
            self.assertIn(voice_page_module.VECTOR_DISPLAY_LABELS.get(voice_page_module.EMOTION_VECTOR_LABELS[index], voice_page_module.EMOTION_VECTOR_LABELS[index]), vector_slider.accessibleName())
        page._set_indextts2_emotion_mode("follow")
        self.assertEqual(page._indextts2_emotion_engine_options(), {"emotion_mode": "follow"})
        page._set_indextts2_emotion_mode("emotext")
        page.indextts2_emotion_text_edit.setPlainText("克制但坚定")
        for expected_weight in (0.0, 0.8, 1.0):
            page._set_indextts2_advanced_weight(expected_weight)
            emotext = page._indextts2_emotion_engine_options()
            self.assertEqual(emotext["emotion_mode"], "emotext")
            self.assertEqual(emotext["emotion_text"], "克制但坚定")
            self.assertEqual(emotext["emotion_weight"], expected_weight)
        page._set_indextts2_advanced_weight(0.8)
        page._set_indextts2_emotion_mode("vector")
        page.indextts2_advanced_sliders[0].setValue(20)
        vector = page._indextts2_emotion_engine_options()
        self.assertEqual(vector["emotion_mode"], "vector")
        self.assertEqual(vector["emotion_vector"][0], 0.2)
        self.assertEqual(len(vector["emotion_vector"]), 8)
        self.assertEqual(page.indextts2_emotion_text_edit.property("luluControl"), "textArea")
        self.assertEqual(page.indextts2_emotion_text_edit.accessibleName(), "IndexTTS 2.5情感描述文本")

    def test_indextts2_saved_style_dirty_tracks_full_weight_range_and_round_trip(self):
        page = VoicePage(_Cfg(self.root), _Main())
        base_style = {
            "id": "style_weight_boundary",
            "name": "权重边界",
            "emotion_preset": "natural",
            "emotion_intensity": "standard",
            "emotion_vector": [0.0] * 8,
            "glossary_apply_enabled": True,
            "system": False,
        }

        for saved_weight in (0.3, 0.8, 1.1):
            page._apply_indextts2_style(
                {**base_style, "emotion_weight": saved_weight},
                persist=False,
            )
            self.assertFalse(page._indextts2_style_dirty)
            self.assertFalse(page._current_indextts2_style_dirty())

        page._set_indextts2_advanced_weight(1.0)
        self.assertFalse(page._current_indextts2_style_dirty())
        page.indextts2_weight_slider.setValue(95)
        self.assertTrue(page._indextts2_style_dirty)
        self.assertTrue(page._current_indextts2_style_dirty())
        page.indextts2_weight_slider.setValue(100)
        self.assertFalse(page._indextts2_style_dirty)
        self.assertFalse(page._current_indextts2_style_dirty())

    def test_stage2b3_parameter_controls_fit_effective_620_without_overlap(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page.setStyleSheet(voice_page_module.T.QSS + page._extra_qss())
        page.resize(668, 900)
        page.show()
        self.app.processEvents()

        for panel_index in range(3):
            page._switch_panel(panel_index)
            self.app.processEvents()
            panel = page.panel_stack.widget(panel_index)
            self.assertLessEqual(panel.width(), 620)

        page._switch_panel(0)
        page._set_indextts2_emotion_mode("vector")
        self.app.processEvents()
        self.assertEqual(page.indextts2_emotion_stack.height(), 108)
        for slider in (page.indextts2_weight_slider, *page.indextts2_advanced_sliders):
            rect = slider.rect()
            self.assertEqual(rect.height(), 28)
            self.assertGreater(rect.width(), 0)
            self.assertTrue(slider.isVisible())
        self.assertFalse(page.btn_indextts2_save_style.isVisible())

    def test_minimax_cloud_panel_is_opt_in_and_length_gated(self):
        old_is_configured = voice_page_module.minimax_tts.is_configured
        try:
            voice_page_module.minimax_tts.is_configured = lambda: False  # type: ignore[assignment]
            page = VoicePage(_Cfg(self.root), _Main())
            page.show()
            self._wait_until(
                lambda: page._minimax_config_state == "unconfigured"
            )
            self.assertEqual(page.panel_stack.currentWidget().objectName(), "indextts2ModelPanel")

            page._switch_panel(2)
            self.app.processEvents()
            self.assertFalse(page.btn_minimax_generate.isEnabled())
            self.assertIn("API Key", page.minimax_status.text())

            voice_page_module.minimax_tts.is_configured = lambda: True  # type: ignore[assignment]
            page._request_minimax_config_check()
            self._wait_until(lambda: page._minimax_config_state == "configured")
            page.minimax_text_edit.setPlainText("你好,这是一段云端配音测试。")
            page._update_minimax_generate_state()
            self.app.processEvents()
            self.assertTrue(page.btn_minimax_generate.isEnabled())
            self.assertIn("文本会上传到 MiniMax", page.minimax_status.text())

            page.minimax_text_edit.setPlainText("你" * (voice_page_module.minimax_tts.MAX_SYNC_CHARS + 1))
            page._update_minimax_generate_state()
            self.assertFalse(page.btn_minimax_generate.isEnabled())
            self.assertIn("3000 字以内", page.minimax_status.text())
            page.shutdown_workers()
        finally:
            voice_page_module.minimax_tts.is_configured = old_is_configured  # type: ignore[assignment]

    def test_minimax_config_check_is_single_background_call_and_main_thread_stays_fast(self):
        entered = threading.Event()
        release = threading.Event()
        calls: list[threading.Thread] = []

        class BlockingBackend:
            def get_password(self, _service, _provider):
                calls.append(threading.current_thread())
                entered.set()
                release.wait(2.0)
                return None

        secrets._backend = lambda: BlockingBackend()  # type: ignore[assignment]
        page = VoicePage(_Cfg(self.root), _Main())
        self.assertEqual(calls, [])

        started = time.perf_counter()
        page.refresh()
        elapsed = time.perf_counter() - started

        self.assertLessEqual(elapsed, 0.250)
        self.assertEqual(page._minimax_config_state, "checking")
        self.assertEqual(page.minimax_ready_chip.text(), "● 检查中")
        self.assertFalse(page.btn_minimax_generate.isEnabled())
        self.assertTrue(entered.wait(1.0))
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0], threading.main_thread())

        worker = page._minimax_config_check_thread
        page.refresh()
        page._reload_minimax_voice_options()
        self.app.processEvents()
        self.assertIs(page._minimax_config_check_thread, worker)
        self.assertEqual(len(calls), 1)

        release.set()
        self._wait_until(
            lambda: (
                page._minimax_config_state == "unconfigured"
                and page._minimax_config_check_thread is None
            )
        )
        self.assertEqual(page.minimax_ready_chip.text(), "● 未配置")
        page.shutdown_workers()

    def test_minimax_config_check_discards_hidden_generation_and_restarts_once(self):
        first_entered = threading.Event()
        first_release = threading.Event()
        lock = threading.Lock()
        calls = 0
        active = 0
        max_active = 0

        class ControlledBackend:
            def get_password(self, _service, _provider):
                nonlocal calls, active, max_active
                with lock:
                    calls += 1
                    current = calls
                    active += 1
                    max_active = max(max_active, active)
                try:
                    if current == 1:
                        first_entered.set()
                        first_release.wait(2.0)
                        return '{"api_key":"configured-only-in-stale-generation"}'
                    return None
                finally:
                    with lock:
                        active -= 1

        secrets._backend = lambda: ControlledBackend()  # type: ignore[assignment]
        page = VoicePage(_Cfg(self.root), _Main())
        page.show()
        self.assertTrue(first_entered.wait(1.0))

        page.hide()
        self.app.processEvents()
        self.assertEqual(page._minimax_config_state, "unknown")
        page.show()
        self.app.processEvents()
        self.assertEqual(page._minimax_config_state, "checking")

        first_release.set()
        self._wait_until(
            lambda: (
                calls == 2
                and page._minimax_config_state == "unconfigured"
                and page._minimax_config_check_thread is None
            )
        )
        self.assertEqual(max_active, 1)
        self.assertEqual(page.minimax_ready_chip.text(), "● 未配置")
        page.shutdown_workers()

    def test_minimax_config_check_failure_is_sanitized_and_fails_closed(self):
        secrets._backend = (  # type: ignore[assignment]
            lambda: (_ for _ in ()).throw(RuntimeError("backend-private-sentinel"))
        )
        page = VoicePage(_Cfg(self.root), _Main())
        page.refresh()
        self._wait_until(lambda: page._minimax_config_state == "failed")

        self.assertEqual(page.minimax_ready_chip.text(), "● 检查失败")
        self.assertIn("暂时无法检查", page.minimax_status.text())
        self.assertNotIn("backend-private-sentinel", page.minimax_status.text())
        self.assertFalse(page.btn_minimax_generate.isEnabled())
        page.shutdown_workers()

    def test_minimax_done_saves_cloud_metadata_without_remote_url(self):
        old_is_configured = voice_page_module.minimax_tts.is_configured
        old_save_voice = voice_page_module.library.save_voice
        saved = []
        try:
            voice_page_module.minimax_tts.is_configured = lambda: True  # type: ignore[assignment]
            voice_page_module.library.save_voice = lambda **kwargs: saved.append(kwargs) or "voice-1"  # type: ignore[assignment]
            main = _Main()
            page = VoicePage(_Cfg(self.root), main)
            page.show()
            self.app.processEvents()
            page._switch_panel(2)
            page.minimax_text_edit.setPlainText("你好,这是一段云端配音测试。")
            out = self.root / "minimax.mp3"
            out.write_bytes(b"audio")
            result = SimpleNamespace(
                output_path=out,
                duration_seconds=1.2,
                model="speech-2.8-turbo",
                voice_id="voice-abc123456",
                output_format="mp3",
                usage_characters=14,
                meta={
                    "engine": "minimax",
                    "provider": "MiniMax",
                    "cloud": True,
                    "upload_notice_ack": True,
                    "model": "speech-2.8-turbo",
                    "voice_id": "voice-abc123456",
                },
            )

            page._on_minimax_done(result)

            self.assertEqual(len(saved), 1)
            record = saved[0]
            self.assertEqual(record["engine"], "minimax")
            self.assertIn("MiniMax", record["config_summary"])
            self.assertEqual(record["voice_id"], "voice-abc123456")
            self.assertEqual(record["audio_path"], out)
            self.assertEqual(record["meta"]["engine"], "minimax")
            self.assertTrue(record["meta"]["cloud"])
            self.assertTrue(record["meta"]["upload_notice_ack"])
            self.assertNotIn("url", json.dumps(record["meta"], ensure_ascii=False).lower())
            self.assertEqual(page._minimax_last_output, str(out))
            self.assertEqual(main.completed[-1], {"page_index": 4})
        finally:
            voice_page_module.minimax_tts.is_configured = old_is_configured  # type: ignore[assignment]
            voice_page_module.library.save_voice = old_save_voice  # type: ignore[assignment]

    def test_voxcpm2_library_save_failure_restores_ui_and_keeps_output(self):
        old_save_voice = voice_page_module.library.save_voice
        old_alert = voice_page_module.dialogs.alert
        alerts = []
        try:
            voice_page_module.library.save_voice = lambda **_kwargs: (_ for _ in ()).throw(OSError("read only"))  # type: ignore[assignment]
            voice_page_module.dialogs.alert = lambda *_args, **_kwargs: alerts.append(_args)  # type: ignore[assignment]
            page = VoicePage(_Cfg(self.root), _Main())
            page.show()
            self.app.processEvents()
            output = self.root / "generated.wav"
            output.write_bytes(b"audio")
            result = SimpleNamespace(
                output_path=output,
                duration_seconds=1.25,
                voice_id="",
                text="失败兜底测试",
                meta={},
            )

            page._on_done(result)

            self.assertTrue(output.exists())
            self.assertTrue(page.btn_generate.isEnabled())
            self.assertFalse(page.btn_cancel.isVisible())
            self.assertEqual(page._last_output, str(output))
            self.assertIn(str(output.resolve()), page.status.text())
            self.assertIn("音频已生成", page.status.text())
            self.assertEqual(len(alerts), 1)
        finally:
            voice_page_module.library.save_voice = old_save_voice  # type: ignore[assignment]
            voice_page_module.dialogs.alert = old_alert  # type: ignore[assignment]

    def test_minimax_failure_status_persists_after_dialog(self):
        old_is_configured = voice_page_module.minimax_tts.is_configured
        old_alert = voice_page_module.dialogs.alert
        try:
            voice_page_module.minimax_tts.is_configured = lambda: True  # type: ignore[assignment]
            voice_page_module.dialogs.alert = lambda *_args, **_kwargs: None  # type: ignore[assignment]
            main = _Main()
            page = VoicePage(_Cfg(self.root), main)
            page.show()
            self.app.processEvents()
            page._switch_panel(2)
            page.minimax_text_edit.setPlainText("你好,这是一段云端配音测试。")
            page._on_minimax_failed("MiniMax API Key 无效或无权限，请检查后重试。")
            self.app.processEvents()

            self.assertTrue(page.btn_minimax_generate.isEnabled())
            self.assertIn("上次 MiniMax 生成失败", page.minimax_status.text())
            self.assertIn("API Key 无效", page.minimax_status.text())
            self.assertEqual(page._minimax_last_output, None)
            self.assertEqual(main.failed, 1)
        finally:
            voice_page_module.minimax_tts.is_configured = old_is_configured  # type: ignore[assignment]
            voice_page_module.dialogs.alert = old_alert  # type: ignore[assignment]

    def test_emotion_control_mode_segmented_switches_panels(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page.resize(1024, 900)
        page.show()
        self.app.processEvents()
        QTest.qWait(50)

        # Legacy template data remains but the approved UI exposes four modes.
        self.assertEqual(tuple(page.indextts2_emotion_mode_buttons.keys()), EMOTION_MODE_ORDER)

        stack_index = {"follow": 0, "emoaudio": 1, "vector": 3, "emotext": 4}
        surface_top = None
        surface = page._approved_index.emotion_card
        surface_height = surface.height()
        for mode in stack_index:
            page._on_indextts2_emotion_mode_selected(mode)
            self.app.processEvents()
            self.assertEqual(page._indextts2_emotion_mode, mode)
            is_follow = mode == "follow"
            self.assertTrue(surface.isVisible())
            self.assertEqual(page.indextts2_weight_slider.isVisible(), not is_follow)
            self.assertEqual(page.indextts2_emotion_stack.currentIndex(), stack_index[mode])
            self.assertEqual(surface.height(), surface_height)
            current_top = surface.mapTo(page, QPoint(0, 0)).y()
            surface_top = current_top if surface_top is None else surface_top
            self.assertEqual(current_top, surface_top)

    def test_emotext_and_emoaudio_gate_until_inputs_provided(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        # 模拟可用音色 + 有文本
        page._indextts2_voice_ready = lambda: True  # type: ignore[assignment]
        page.indextts2_text_edit.setPlainText("你好,这是一段测试文本。")
        page.show()
        self.app.processEvents()

        page._on_indextts2_emotion_mode_selected("emotext")
        self.assertEqual(page._indextts2_gate_reason(), "请输入情感描述文本")
        page.indextts2_emotion_text_edit.setPlainText("委屈巴巴")
        self.assertEqual(page._indextts2_gate_reason(), "")

        page._on_indextts2_emotion_mode_selected("emoaudio")
        self.assertEqual(page._indextts2_gate_reason(), "请选择情感参考音频")

    def test_empty_emotext_only_blocks_emotext_mode(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page._indextts2_voice_ready = lambda: True  # type: ignore[assignment]
        page.indextts2_text_edit.setPlainText("你好,这是一段测试文本。")
        page.indextts2_emotion_text_edit.setPlainText("")
        page.show()
        self.app.processEvents()

        page._on_indextts2_emotion_mode_selected("emotext")
        self.assertEqual(page._indextts2_gate_reason(), "请输入情感描述文本")

        page._on_indextts2_emotion_mode_selected("template")
        self.assertEqual(page._indextts2_gate_reason(), "")

        page._on_indextts2_emotion_mode_selected("follow")
        self.assertEqual(page._indextts2_gate_reason(), "")

    def test_voice_sample_recording_reuses_new_voice_flow(self):
        page = VoicePage(_Cfg(self.root), _Main())
        sample = self.root / "sample.m4a"
        sample.write_bytes(b"recorded voice sample")
        calls = []

        page._record_audio_for_voice_use = lambda purpose: (str(sample), {"source": "麦克风"})  # type: ignore[method-assign]

        def fake_new_voice_from_path(path, *, target_combo=None, cleanup_source=False):
            calls.append((path, target_combo, cleanup_source))

        page._new_voice_from_path = fake_new_voice_from_path  # type: ignore[method-assign]

        page._record_voice_sample(page.indextts2_voice_combo)

        self.assertEqual(calls, [(str(sample), page.indextts2_voice_combo, True)])

    def test_emotion_reference_recording_binds_current_state_and_clears(self):
        main = _Main()
        page = VoicePage(_Cfg(self.root), main)
        page._indextts2_status = _ready_status()
        sample = self.root / "emotion_ref.m4a"
        sample.write_bytes(b"recorded emotion reference")
        page._record_audio_for_voice_use = lambda purpose: (  # type: ignore[method-assign]
            str(sample),
            {"source": "麦克风", "stats": {"duration": 3.2, "is_silent": False}},
        )
        page.show()
        self.app.processEvents()

        page._on_indextts2_emotion_mode_selected("emoaudio")
        page._record_indextts2_emotion_audio()

        self.assertEqual(page._indextts2_emotion_audio_path, str(sample))
        self.assertTrue(page._indextts2_emotion_audio_recorded)
        self.assertEqual(page.indextts2_emoaudio_title.text(), "已选择情感参考音频")
        self.assertIn("麦克风", page.indextts2_emoaudio_drop.sub.text())
        self.assertIn("3.2s", page.indextts2_emoaudio_drop.sub.text())
        self.assertIn("仅本次使用", page.indextts2_emoaudio_drop.sub.text())
        self.assertIn("已作为本次情感参考音频", main.toasts)

        page._clear_indextts2_emotion_audio()

        self.assertEqual(page._indextts2_emotion_audio_path, "")
        self.assertFalse(page._indextts2_emotion_audio_recorded)
        self.assertEqual(page._indextts2_emotion_audio_meta, {})
        self.assertEqual(page.indextts2_emoaudio_title.text(), "拖入情感参考音频")

    def test_cancelled_recording_does_not_bind_voice_or_emotion_reference(self):
        page = VoicePage(_Cfg(self.root), _Main())
        calls = []
        page._record_audio_for_voice_use = lambda purpose: None  # type: ignore[method-assign]
        page._new_voice_from_path = lambda *args, **kwargs: calls.append((args, kwargs))  # type: ignore[method-assign]
        page.show()
        self.app.processEvents()

        page._record_voice_sample(page.indextts2_voice_combo)
        page._record_indextts2_emotion_audio()

        self.assertEqual(calls, [])
        self.assertEqual(page._indextts2_emotion_audio_path, "")


    def test_runtime_install_cancel_emits_cancelled_without_finished(self):
        from src.core.synthesis import runtime_packages
        from src.ui.worker import TTSRuntimeInstallWorker

        worker = TTSRuntimeInstallWorker("local_voxcpm2")
        emitted: list[str] = []
        worker.cancelled.connect(lambda: emitted.append("cancelled"))
        worker.finished_ok.connect(lambda: emitted.append("finished"))

        def fake_install(_model_id, *, progress, is_cancelled):
            self.assertTrue(is_cancelled())
            raise runtime_packages.CancelledError()

        worker.request_cancel()
        with patch.object(runtime_packages, "install", side_effect=fake_install):
            worker.run()

        self.assertEqual(emitted, ["cancelled"])

    def test_voice_recording_dialog_exposes_three_sources(self):
        page = VoicePage(_Cfg(self.root), _Main())
        dlg = voice_page_module._VoiceRecordingDialog(page, purpose="voice_sample")

        self.assertEqual(set(dlg._buttons.keys()), {"microphone", "system_audio", "mixed"})
        self.assertIn("10-30 秒", dlg._purpose_hint())
        dlg._set_source("mixed")
        self.assertIn("混合音频不推荐", dlg.hint.text())

    def test_voice_library_change_cancel_keeps_current_location(self):
        cfg = _Cfg(self.root)
        page = VoicePage(cfg, _Main())
        before_cfg = cfg.voice_library_dir
        before_dir = str(voices.current_voice_library_dir())
        old_picker = voice_page_module.QFileDialog.getExistingDirectory
        voice_page_module.QFileDialog.getExistingDirectory = lambda *_args, **_kwargs: ""  # type: ignore[assignment]
        try:
            self.assertFalse(page._change_voice_library_dir())
        finally:
            voice_page_module.QFileDialog.getExistingDirectory = old_picker  # type: ignore[assignment]

        self.assertEqual(cfg.voice_library_dir, before_cfg)
        self.assertEqual(str(voices.current_voice_library_dir()), before_dir)


    def test_voice_generation_progress_shows_frac_percent_and_activity_state(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page.show()
        self.app.processEvents()

        page._switch_panel(1)
        self.app.processEvents()
        page._set_voice_progress_stopped(page.progress)
        page.progress.setVisible(True)
        page._set_voice_progress_running(page.progress, page.status, "生成第 2/5 段", 0.42)
        self.app.processEvents()
        self.assertEqual(page.progress.minimum(), 0)
        self.assertEqual(page.progress.maximum(), 100)
        self.assertEqual(page.progress.property("voiceProgressTarget"), 42)
        self.assertLess(page.progress.value(), 42)
        self.assertEqual(page.progress.format(), "42%")
        self.assertIn("生成第 2/5 段", page.status.text())
        self.assertIn("已到真实进度 42%", page.status.text())
        self.assertIn("模型内部生成中", page.status.text())
        self.assertIs(page.progress.property("voiceRunningActivity"), True)
        self.assertEqual(page.progress.property("luluControl"), "progress")
        self.assertEqual(page.progress.property("statusKind"), "info")
        self.assertEqual(page.btn_generate.property("state"), "loading")
        self.assertIs(page._voice_progress_animations[page.progress].state(), QAbstractAnimation.Running)
        QTest.qWait(850)
        self.app.processEvents()
        self.assertEqual(page.progress.value(), 42)

        page._switch_panel(0)
        self.app.processEvents()
        page.indextts2_progress.setVisible(True)
        page._set_voice_progress_running(
            page.indextts2_progress,
            page.indextts2_status,
            "生成第 4/6 段",
            4 / 6,
            prefix="IndexTTS 2.5:",
        )
        self.app.processEvents()
        self.assertEqual(page.indextts2_progress.property("voiceProgressTarget"), 67)
        self.assertIn("IndexTTS 2.5:生成第 4/6 段", page.indextts2_status.text())
        self.assertIn("已到真实进度 67%", page.indextts2_status.text())
        page._set_voice_progress_running(
            page.indextts2_progress,
            page.indextts2_status,
            "旧进度回放",
            0.20,
            prefix="IndexTTS 2.5:",
        )
        self.app.processEvents()
        self.assertEqual(page.indextts2_progress.property("voiceProgressTarget"), 67)
        self.assertIn("已到真实进度 67%", page.indextts2_status.text())
        page._on_indextts2_progress("saving audio...", 1.0)
        self.app.processEvents()
        self.assertEqual(page.indextts2_progress.property("voiceProgressTarget"), 98)
        self.assertEqual(page.indextts2_progress.format(), "98%")
        self.assertIn("整体进度 98%", page.indextts2_status.text())
        self.assertIn("正在收尾，准备试听", page.indextts2_status.text())
        self.assertNotIn("100% · 模型内部生成中", page.indextts2_status.text())
        self.assertNotIn("模型内部生成中", page.indextts2_status.text())

        page._set_voice_progress_stopped(page.progress)
        page.progress.setVisible(True)
        page._set_voice_progress_running(
            page.progress,
            page.status,
            "首次加载模型或首次使用这个音色会慢一些，后续同会话通常会快一些。",
            -1,
        )
        self.app.processEvents()
        self.assertEqual(page.progress.minimum(), 0)
        self.assertEqual(page.progress.maximum(), 100)
        self.assertEqual(page.progress.property("voiceProgressTarget"), 0)
        self.assertEqual(page.progress.format(), "运行中")
        self.assertIn("首次加载模型或首次使用这个音色会慢一些", page.status.text())
        self.assertNotIn("%", page.status.text())
        QTest.qWait(1400)
        self.app.processEvents()
        self.assertEqual(page.progress.value(), 0)
        self.assertTrue(page.status.text().startswith("首次加载模型或首次使用这个音色会慢一些"))

        page._set_voice_progress_stopped(page.progress)
        self.assertTrue(page.progress.isHidden())
        self.assertEqual(page.progress.value(), 0)
        self.assertEqual(page.progress.property("statusKind"), "neutral")
        self.assertEqual(page.btn_generate.property("state"), "default")
        self.assertIs(page.progress.property("voiceRunningActivity"), False)
        self.assertFalse(page._voice_progress_timers[page.progress].isActive())

        page._set_voice_progress_complete(page.indextts2_progress)
        self.assertTrue(page.indextts2_progress.isVisible())
        self.assertEqual(page.indextts2_progress.value(), 100)
        self.assertEqual(page.indextts2_progress.format(), "100%")
        self.assertIs(page.indextts2_progress.property("voiceRunningActivity"), False)
        self.assertFalse(page._voice_progress_timers[page.indextts2_progress].isActive())

        page._set_voice_progress_running(page.indextts2_progress, page.indextts2_status, "新一轮准备", -1, prefix="IndexTTS 2.5:")
        self.app.processEvents()
        self.assertEqual(page.indextts2_progress.value(), 0)
        self.assertEqual(page.indextts2_progress.property("voiceProgressTarget"), 0)
        self.assertNotIn("100%", page.indextts2_status.text())

        page._set_voice_progress_running(
            page.indextts2_progress,
            page.indextts2_status,
            "saving audio...",
            1.0,
            prefix="IndexTTS 2.5:",
            finalizing_cap=98,
        )
        self.app.processEvents()
        page._set_voice_progress_stopped(page.indextts2_progress)
        self.assertTrue(page.indextts2_progress.isHidden())
        self.assertEqual(page.indextts2_progress.value(), 0)
        self.assertIs(page.indextts2_progress.property("voiceRunningActivity"), False)
        self.assertFalse(page._voice_progress_timers[page.indextts2_progress].isActive())

    def test_indextts2_reuses_background_status_without_starting_label(self):
        _FakeIndexTTS2Worker.instances.clear()
        voice_page_module.IndexTTS2SynthesisWorker = _FakeIndexTTS2Worker  # type: ignore[assignment]
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page._indextts2_voice_ready = lambda: True  # type: ignore[assignment]
        page._indextts2_background_status_snapshot = lambda: {  # type: ignore[assignment]
            "alive": True,
            "pid": 123,
            "idle_timeout_seconds": 600,
        }
        page.indextts2_text_edit.setPlainText("你好,这是一段测试文本。")
        page.show()
        self.app.processEvents()

        page._generate_indextts2()
        self.app.processEvents()

        self.assertEqual(len(_FakeIndexTTS2Worker.instances), 1)
        self.assertTrue(_FakeIndexTTS2Worker.instances[0].started)
        self.assertIn("正在本地生成", page.indextts2_bg_status.text())
        self.assertIn("复用已常驻", page.indextts2_status.text())
        self.assertNotIn("PID", page.indextts2_bg_status.text())
        self.assertNotIn("123", page.indextts2_bg_status.text())
        self.assertNotIn("启动后台中", page.indextts2_bg_status.text())
        self.assertNotIn("正在启动 IndexTTS 2.5", page.indextts2_status.text())
        self.assertFalse(page.btn_indextts2_release_bg.isEnabled())

        page._on_indextts2_lifecycle({"alive": True, "pid": 82509, "idle_timeout_seconds": 600})
        self.assertEqual(page.indextts2_bg_status.text(), "后台已就绪 · 连续生成更快")
        self.assertNotIn("PID", page.indextts2_bg_status.text())
        self.assertNotIn("82509", page.indextts2_bg_status.text())

        page._on_indextts2_background_released({"alive": False})
        self.assertEqual(page.indextts2_bg_status.text(), "后台已释放 · 下次生成会重新加载")
        self.assertNotIn("PID", page.indextts2_bg_status.text())

        page._on_indextts2_lifecycle({"alive": True, "pid": 82509, "idle_timeout_seconds": 600})
        self.assertEqual(page.indextts2_bg_status.text(), "后台已就绪 · 连续生成更快")
        self.assertIn("后台已就绪", page.indextts2_status.text())
        self.assertNotIn("后台已释放", page.indextts2_status.text())

    def test_indextts2_environment_gate_overrides_background_ready_copy(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._on_indextts2_status_ready(_unready_status(runtime_ready=False, model_ready=True, config_ready=True))
        page.show()
        self.app.processEvents()

        page._on_indextts2_lifecycle({"alive": True, "pid": 82509, "idle_timeout_seconds": 600})

        self.assertIn("runtime", page.indextts2_status_title.text())
        self.assertIn("当前还不能生成", page.indextts2_gate_hint.text())
        self.assertIn("本地环境未准备", page.indextts2_bg_status.text())
        self.assertNotIn("已就绪", page.indextts2_bg_status.text())
        self.assertNotIn("连续生成更快", page.indextts2_bg_status.text())
        self.assertNotIn("PID", page.indextts2_bg_status.text())
        self.assertNotIn("82509", page.indextts2_bg_status.text())

        page._on_indextts2_background_released({"alive": False})
        self.assertEqual(page.indextts2_bg_status.text(), "后台已释放 · 本地环境仍未准备")
        self.assertNotIn("已就绪", page.indextts2_bg_status.text())
        self.assertNotIn("PID", page.indextts2_bg_status.text())

    def test_indextts2_footer_controls_do_not_overlap_in_compact_width(self):
        page = VoicePage(_Cfg(self.root), _Main())
        page._indextts2_status = _ready_status()
        page.show()
        page.resize(620, 540)
        page.indextts2_bg_status.setText("后台已就绪 · 连续生成更快")
        page.indextts2_result_summary.setText("生成后会在这里显示模型、音色、情感方式与术语读音状态。" * 4)
        page.indextts2_result_summary.setVisible(True)
        self.app.processEvents()

        controls = [
            page.indextts2_bg_status,
            page.btn_indextts2_release_bg,
            page.btn_indextts2_cancel,
            page.btn_indextts2_generate,
        ]
        visible_controls = [widget for widget in controls if widget.isVisible()]
        for left_index, left in enumerate(visible_controls):
            left_rect = left.geometry()
            for right in visible_controls[left_index + 1 :]:
                self.assertFalse(left_rect.intersects(right.geometry()), (left.objectName(), right.objectName()))
        self.assertFalse(page.indextts2_bg_status.wordWrap())
        self.assertNotIn("PID", page.indextts2_bg_status.text())
        self.assertEqual(page.btn_indextts2_release_bg.size(), page.btn_indextts2_generate.size())
        self.assertEqual(page.btn_indextts2_release_bg.height(), 44)
        self.assertEqual(page.indextts2_result_empty.height(), 44)
        self.assertTrue(page.indextts2_result_empty.isVisible())
        page.page_scroll.verticalScrollBar().setValue(page.page_scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        result_top = page.indextts2_result_empty.mapTo(page, QPoint(0, 0)).y()
        generate_bottom = (
            page.btn_indextts2_generate.mapTo(page, QPoint(0, 0)).y()
            + page.btn_indextts2_generate.height()
        )
        self.assertGreater(result_top, generate_bottom)  # Approved B places results below actions.

    def test_voice_authorization_and_glossary_use_shared_checkbox_semantics(self):
        voice_source = (
            Path(voice_page_module.__file__).read_text(encoding="utf-8")
        )
        glossary_source = (
            Path(voice_page_module.__file__).with_name("glossary_panel.py")
            .read_text(encoding="utf-8")
        )
        self.assertIn("self.auth_check = LuluCheckBox(", voice_source)
        self.assertNotIn("self.auth_check = QCheckBox(", voice_source)
        self.assertIn("self.apply_check = LuluCheckBox(", glossary_source)
        self.assertNotIn("self.apply_check = QCheckBox(", glossary_source)


if __name__ == "__main__":
    unittest.main()
