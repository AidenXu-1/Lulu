import hashlib
import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractSlider, QLabel, QPushButton, QSlider, QTextBrowser, QWidget

from src.core import library, secrets
from src.config import AppConfig
from src.core.collect.models import CollectedItem
from src.core.models import Segment, Transcript
from src.core.recording import RecordingResult, RecordingSource, RecordingTrack, RecordingTrackKind
from src.core.task import TaskState
from src.core.synthesis import glossary, style_presets, voices
from src.core.synthesis.base import VoiceProfile
from src import main as app_main
from src.ui import icons, library_page, models_page, theme as T
from src.ui.control_foundation import PaperTextureFrame
from src.ui.main_window import (
    ANIM_MS,
    BRAND_LOGO_SIZE,
    CENTER_MIN_W,
    MainWindow,
    PANE_TOGGLE_FALLBACK_Y,
    SIDEBAR_TOTAL_TOP,
    TITLEBAR_TOTAL_H,
)
from src.ui.voice_page import VoicePage
from src.ui.widgets import ElideLabel


class _FakePresentWindow:
    def __init__(self):
        self.calls = []
        self._state = Qt.WindowState.WindowMinimized

    def windowState(self):  # noqa: N802
        self.calls.append("windowState")
        return self._state

    def setWindowState(self, state):  # noqa: N802
        self.calls.append(("setWindowState", state))
        self._state = state

    def showNormal(self):  # noqa: N802
        self.calls.append("showNormal")

    def show(self):
        self.calls.append("show")

    def raise_(self):
        self.calls.append("raise_")

    def activateWindow(self):  # noqa: N802
        self.calls.append("activateWindow")


class _FakeSystemAudioSession:
    def __init__(self, request):
        self.request = request
        self.cancelled = False
        self.active = True

    def is_active(self) -> bool:
        return self.active

    def stop(self, progress=None):
        self.active = False
        self.request.output_path.write_bytes(b"fake-system-audio")
        track = RecordingTrack(
            kind=RecordingTrackKind.SYSTEM_AUDIO,
            path=self.request.output_path,
            is_silent=False,
        )
        return RecordingResult(
            source=RecordingSource.SYSTEM_AUDIO,
            output_path=self.request.output_path,
            tracks=(track,),
        )

    def cancel(self) -> None:
        self.cancelled = True
        self.active = False
        if self.request.output_path.exists():
            self.request.output_path.unlink()


class _FakeSystemAudioBackend:
    def __init__(self):
        self.session = None

    def start(self, request, progress=None):
        self.session = _FakeSystemAudioSession(request)
        return self.session


class _FailingSystemAudioBackend:
    def start(self, request, progress=None):
        raise RuntimeError("系统声音权限未打开")


class _FakeMicRecorder(QObject):
    tick = Signal(int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.path = None
        self.recording = False

    def has_input(self) -> bool:
        return True

    def is_recording(self) -> bool:
        return self.recording

    def start(self, path: str) -> None:
        self.path = path
        self.recording = True

    def stop(self) -> None:
        path = Path(self.path) if self.path else None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-microphone-audio")
        self.recording = False
        self.finished.emit(str(path or ""))


class _NoInputMicRecorder(_FakeMicRecorder):
    def has_input(self) -> bool:
        return False


class FirstVersionShellTests(unittest.TestCase):
    def wait_library(self, page):
        from PySide6.QtCore import QEventLoop
        for _ in range(500):
            if not (page._query_worker or page._pending_refresh):
                return
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        self.fail("library query did not finish")
    def setUp(self):
        prewarm = patch.object(MainWindow, "_prepare_library")
        prewarm.start(); self.addCleanup(prewarm.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for guard in (patch("src.core.models_manager.app_data_dir", return_value=self.root / "data"),
                      patch("src.core.models_manager.is_ready", return_value=False),
                      patch("src.ui.model_settings.controller.ModelController.refresh")):
            guard.start()
            self.addCleanup(guard.stop)
        self.old_theme_name = T.ACTIVE.name
        self.test_theme = os.environ.get("LULU_TEST_THEME", "warm").strip().lower()
        if self.test_theme not in T.THEMES:
            self.fail(f"unsupported LULU_TEST_THEME={self.test_theme!r}")
        self.test_palette = T.apply_theme(self.test_theme)
        self.old_config_load = AppConfig.__dict__["load"]
        def load_explicit_test_theme(_cls):
            # 离屏回归绝不读取用户真实配置；每例从默认配置开始。
            cfg = AppConfig()
            cfg.theme = self.test_theme
            return cfg

        AppConfig.load = classmethod(load_explicit_test_theme)
        self.old_library_dir = library.LIBRARY_DIR
        library.LIBRARY_DIR = self.root / "library"
        self.old_secret_backend = secrets._backend
        secrets._backend = lambda: None  # type: ignore[assignment]
        self.old_config_save = AppConfig.save
        AppConfig.save = lambda _self: True  # type: ignore[assignment]
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
        from PySide6.QtCore import QCoreApplication, QEvent
        self.app.processEvents()
        for window in QApplication.topLevelWidgets():
            if not isinstance(window, MainWindow):
                continue
            window._runtime_preferences_timer.stop()
            window._task_queue_sync_timer.stop()
            window.extraction_workspace.shutdown()
            if window.library_page is not None and hasattr(window.library_page, "shutdown"):
                window.library_page.shutdown()
            if window.models_page is not None:
                window.models_page.shutdown_workers(500)
                window.models_page.page.setWebChannel(None)
            window.hide()
            self.app.processEvents()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        voices.list_voices = self.old_list_voices  # type: ignore[assignment]
        voices.get_voice = self.old_get_voice  # type: ignore[assignment]
        VoicePage._refresh_indextts2_status = self.old_refresh_status  # type: ignore[assignment]
        style_presets.style_presets_path = self.old_style_path  # type: ignore[assignment]
        glossary.glossary_path = self.old_glossary_path  # type: ignore[assignment]
        library.LIBRARY_DIR = self.old_library_dir
        secrets._backend = self.old_secret_backend  # type: ignore[assignment]
        AppConfig.save = self.old_config_save  # type: ignore[assignment]
        AppConfig.load = self.old_config_load  # type: ignore[assignment]
        T.apply_theme(self.old_theme_name)
        self.tmp.cleanup()

    def _labels(self, window: MainWindow) -> set[str]:
        return {label.text() for label in window.findChildren(QLabel)}

    def _visible_text(self, root: QWidget) -> str:
        return "\n".join(
            label.text()
            for label in root.findChildren(QLabel)
            if not label.isHidden() and label.text()
        )

    def _click_copywriting_button(self, window: MainWindow, text: str, *, panel) -> None:
        for button in panel.findChildren(QPushButton):
            if button.text().strip() == text and not button.isHidden():
                button.click()
                self.app.processEvents()
                return
        self.fail(f"未找到文案处理按钮:{text}")


    def test_immersive_titlebar_separator_follows_only_the_sample_page(self):
        window = MainWindow()
        window._native_titlebar_active = True

        with patch("src.ui.macos.set_page_titlebar_immersive", return_value=True) as native:
            window._sync_workbench_paper_scope(0)
            native.assert_called_with(window, True)
            self.assertTrue(window._immersive_titlebar_ready)

            window._sync_workbench_paper_scope(3)
            native.assert_called_with(window, False)
            self.assertTrue(window._immersive_titlebar_ready)
            self.assertEqual(native.call_count, 2)

    def _widget_top(self, window: MainWindow, widget) -> int:
        return widget.mapTo(window, QPoint(0, 0)).y()


    def _assert_copywriting_result_empty(self, page) -> None:
        self.assertFalse(page.result_panel.isHidden())
        self.assertIs(page.result_stack.currentWidget(), page.result_empty)
        self.assertEqual(page.result_ctx.text(), "等待处理")
        self.assertEqual(page.result_src.text(), "")
        self.assertTrue(page.result_src.isHidden())
        self.assertEqual(page.result_edit.toPlainText(), "")
        self.assertTrue(page.result_action_bar.isHidden())
        self.assertEqual(page.result_row.count(), 0)
        self.assertEqual(page.result_save_row.count(), 0)

    def _assert_pane_rails_ready(self, window: MainWindow) -> None:
        for rail in (window.rail_left,):
            self.assertFalse(rail.isHidden())
            self.assertTrue(rail.isEnabled())
            self.assertEqual(rail.objectName(), "luluButton")
            self.assertEqual(rail.property("legacyObjectName"), "paneToggleRail")
            self.assertEqual(rail.property("controlVariant"), "pane")
            self.assertEqual(bool(rail.property("consumerAccentIcon")), rail.isChecked())
            self.assertGreaterEqual(rail.width(), 24)
            self.assertGreaterEqual(rail.height(), 22)

    def _assert_button_icon_color(self, button, expected: str) -> None:
        image = button.icon().pixmap(button.iconSize()).toImage()
        pixels = [
            image.pixelColor(x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        ]
        self.assertTrue(pixels)
        sample = max(pixels, key=lambda color: color.alpha())
        target = QColor(expected)
        self.assertLessEqual(
            max(
                abs(sample.red() - target.red()),
                abs(sample.green() - target.green()),
                abs(sample.blue() - target.blue()),
            ),
            3,
            (sample.name(), target.name()),
        )

    def test_present_main_window_restores_and_activates_window(self):
        window = _FakePresentWindow()
        app_main._present_main_window(window)

        state_calls = [call for call in window.calls if isinstance(call, tuple) and call[0] == "setWindowState"]
        self.assertEqual(len(state_calls), 1)
        self.assertEqual(state_calls[0][1], Qt.WindowActive)
        self.assertEqual(window.calls[-4:], ["show", "showNormal", "raise_", "activateWindow"])

    def test_transcription_done_surfaces_library_save_failure(self):
        window = MainWindow()
        window._add_task("/tmp/input.wav", "file", "待保存任务")
        task = window.tasks[-1]
        old_save = library.save

        def fail_save(*_args, **_kwargs):
            raise OSError("disk full")

        try:
            library.save = fail_save  # type: ignore[assignment]
            window._on_done(task.id, Transcript("转写正文"))
        finally:
            library.save = old_save  # type: ignore[assignment]

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.message, "结果待保存")
        self.assertIsNone(task.library_id)
        self.assertEqual(task.transcript.text, "转写正文")
        self.assertTrue(window.has_unsaved_work())
        self.assertEqual(task.message, "结果待保存")

    def test_transcription_readback_failure_retries_same_entry_once(self):
        window = MainWindow()
        window._add_task("/tmp/input.wav", "file", "待复读任务")
        task = window.tasks[-1]
        real_load = library.load

        with patch.object(MainWindow, "_notify_task_completed") as notify:
            with patch("src.core.library.load", return_value=None):
                window._on_done(task.id, Transcript("完整转录"))

            pending_id = task._pending_library_id
            self.assertTrue(pending_id)
            self.assertEqual(task.state, TaskState.FAILED)
            self.assertEqual(len(list(library.LIBRARY_DIR.glob("*.json"))), 1)
            notify.assert_not_called()

            with patch("src.core.library.load", side_effect=real_load):
                window._rerun_task(task.id)

            self.assertEqual(task.state, TaskState.DONE)
            self.assertEqual(task.library_id, pending_id)
            self.assertEqual(len(list(library.LIBRARY_DIR.glob("*.json"))), 1)
            notify.assert_called_once()

    def test_transcription_is_done_only_after_complete_note_is_readable(self):
        window = MainWindow()
        window._add_task("/tmp/input.wav", "file", "完整成稿任务")
        task = window.tasks[-1]
        transcript = Transcript(
            "第一句。最后一句。",
            segments=[
                Segment("第一句。", start=0.0, end=1.0),
                Segment("最后一句。", start=131.0, end=132.0),
            ],
        )

        window._on_done(task.id, transcript)

        self.assertEqual(task.state, TaskState.DONE)
        body = library.load_markdown(task.library_id)
        self.assertIn("## 逐字稿", body)
        self.assertIn("[00:00]", body)
        self.assertIn("[02:11]", body)

    def test_quick_preferences_roll_back_controls_and_directory_on_save_failure(self):
        window = MainWindow()
        messages = []
        window._toast = messages.append
        fields = {name: getattr(window.cfg, name) for name in ("keep_media", "prefer_subtitles", "media_dir")}
        with patch.object(AppConfig, "save", return_value=False):
            self.assertFalse(window._commit_quick_preference("keep_media", not fields["keep_media"]))
            self.assertFalse(window._commit_quick_preference("prefer_subtitles", not fields["prefer_subtitles"]))
            self.assertFalse(window._commit_quick_preference("media_dir", str(self.root / "must-not-commit")))
        for name, value in fields.items():
            self.assertEqual(getattr(window.cfg, name), value)
        self.assertEqual(messages.count("设置没有保存，本次仍按原设置执行"), 3)


    def test_titlebar_safe_area_keeps_content_below_the_shared_control_band(self):
        window = MainWindow()
        window.resize(1200, 760)
        window.show()
        self.app.processEvents()

        self.assertEqual(window.center_layout.contentsMargins().top(), TITLEBAR_TOTAL_H)
        self.assertEqual(window.sidebar_layout.contentsMargins().top(), SIDEBAR_TOTAL_TOP)
        self.assertEqual(window.rail_left.y(), PANE_TOGGLE_FALLBACK_Y)

        window._native_titlebar_active = True
        window._apply_titlebar_safe_area(28)
        self.app.processEvents()
        self.assertEqual(window.center_layout.contentsMargins().top(), TITLEBAR_TOTAL_H)
        self.assertEqual(window.sidebar_layout.contentsMargins().top(), SIDEBAR_TOTAL_TOP)
        self.assertGreater(window.stack.mapTo(window, QPoint(0, 0)).y(),
                           window.titlebar_paper_center.geometry().bottom())
        self.assertEqual(window._titlebar_safe_top, 28)
        self.assertEqual(window.rail_left.y(), round((TITLEBAR_TOTAL_H - 28) / 2))

        window._apply_titlebar_safe_area(65)
        self.assertEqual(window.center_layout.contentsMargins().top(), TITLEBAR_TOTAL_H)
        self.assertEqual(window.sidebar_layout.contentsMargins().top(), SIDEBAR_TOTAL_TOP)
        self.assertEqual(window._titlebar_safe_top, 0)
        self.assertEqual(window.rail_left.y(), PANE_TOGGLE_FALLBACK_Y)

        window._apply_titlebar_safe_area(0)
        self.assertEqual(window.center_layout.contentsMargins().top(), TITLEBAR_TOTAL_H)
        self.assertEqual(window.rail_left.y(), PANE_TOGGLE_FALLBACK_Y)

    def test_titlebar_activation_state_keeps_pane_controls_stable(self):
        window = MainWindow()
        window.resize(1200, 760)
        window.show()
        self.app.processEvents()

        window.isActiveWindow = lambda: False
        window.changeEvent(QEvent(QEvent.Type.ActivationChange))
        self.assertFalse(window._window_is_active)
        self.assertFalse(window.rail_left.icon().isNull())

        window.isActiveWindow = lambda: True
        window.changeEvent(QEvent(QEvent.Type.ActivationChange))
        self.assertTrue(window._window_is_active)
        self.assertEqual(window.rail_left.size().width(), 30)
        self.assertEqual(window.rail_left.size().height(), 28)

    def test_pane_and_settings_icons_use_accent_only_for_active_state(self):
        window = MainWindow()
        window.show()
        self.app.processEvents()
        accent = T.consumer_theme(self.test_theme).accent
        inactive = T.consumer_theme(self.test_theme).ink_soft

        self.assertTrue(window.rail_left.isChecked())
        self.assertFalse(window.settings_button.isChecked())
        self._assert_button_icon_color(window.rail_left, accent)
        self._assert_button_icon_color(window.settings_button, inactive)

        window._toggle_pane("left")
        self.app.processEvents()
        self.assertFalse(window.rail_left.isChecked())
        self._assert_button_icon_color(window.rail_left, inactive)

        self.app.processEvents()

        window._open_update_settings()
        self.app.processEvents()
        self.assertTrue(window.settings_button.isChecked())
        self._assert_button_icon_color(window.settings_button, accent)
        window.close()

    def test_unified_titlebar_mirrors_window_title_in_the_control_row(self):
        window = MainWindow()
        window.resize(1200, 760)
        window.show()
        self.app.processEvents()

        self.assertEqual(window.window_title_label.text(), "Lulu")
        self.assertEqual(window.window_title_label.geometry(), QRect(420, PANE_TOGGLE_FALLBACK_Y, 360, 28))
        self.assertTrue(
            window.window_title_label.testAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
        )

        window.setWindowTitle("Lulu · 文案提取主题预览 v10")
        self.assertEqual(
            window.window_title_label.text(), "Lulu · 文案提取主题预览 v10"
        )
        window._native_titlebar_active = True
        window._apply_titlebar_safe_area(28)
        self.assertEqual(window.window_title_label.geometry(), QRect(420, round((TITLEBAR_TOTAL_H - 28) / 2), 360, 28))


    def test_titlebar_drag_band_moves_window_but_never_consumes_pane_controls(self):
        window = MainWindow()
        window.resize(1200, 760)
        window.move(100, 100)
        window.show()
        self.app.processEvents()
        window._native_titlebar_active = True
        window._apply_titlebar_safe_area(28)

        class MouseEvent:
            def __init__(self, event_type, local, global_, *, buttons=Qt.MouseButton.NoButton):
                self._event_type = event_type
                self._local = QPointF(local)
                self._global = QPointF(global_)
                self._buttons = buttons

            def type(self):
                return self._event_type

            def button(self):
                return Qt.MouseButton.LeftButton

            def buttons(self):
                return self._buttons

            def position(self):
                return self._local

            def globalPosition(self):  # noqa: N802
                return self._global

        press = MouseEvent(QEvent.Type.MouseButtonPress, QPoint(200, 14), QPoint(500, 300))
        self.assertTrue(window._can_start_window_drag(window, press))
        self.assertFalse(window._can_start_window_drag(window.rail_left, press))
        self.assertTrue(window.eventFilter(window, press))

        move = MouseEvent(
            QEvent.Type.MouseMove,
            QPoint(220, 34),
            QPoint(520, 320),
            buttons=Qt.MouseButton.LeftButton,
        )
        self.assertTrue(window.eventFilter(window, move))
        self.assertEqual(window.pos(), QPoint(120, 120))
        release = MouseEvent(QEvent.Type.MouseButtonRelease, QPoint(220, 34), QPoint(520, 320))
        self.assertTrue(window.eventFilter(window, release))
        self.assertFalse(window._window_dragging)


    def test_main_window_constructs_before_keyring_config_read(self):
        old_get_config = secrets.get_config
        calls: list[str] = []

        def boom(key: str):
            calls.append(key)
            raise RuntimeError("keyring blocked")

        secrets.get_config = boom  # type: ignore[assignment]
        try:
            window = MainWindow()
            self.assertEqual(window.windowTitle(), "Lulu")
            self.assertIsNone(window.models_page)
            self.assertEqual(calls, [])
            window._switch_page(1)
            self.assertIsNotNone(window.models_page)
        finally:
            secrets.get_config = old_get_config  # type: ignore[assignment]

    def test_settings_changes_sync_back_to_loaded_workbench_controls(self):
        window = MainWindow()
        window._switch_page(6)
        page = window.settings_page
        self.app.processEvents()

        media_dir = str(self.root / "settings-media")
        recording_dir = str(self.root / "settings-recordings")
        tts_dir = str(self.root / "settings-voices")
        self.assertTrue(page._save_value("media_dir", media_dir))
        self.assertTrue(page._save_value("recording_dir", recording_dir))
        self.assertTrue(page._save_value("tts_output_dir", tts_dir))

        self.assertEqual(window.cfg.media_dir, media_dir)
        self.assertEqual(window.cfg.recording_dir, recording_dir)
        self.assertEqual(window.cfg.tts_output_dir, tts_dir)

    def test_copywriting_model_choice_notifies_main_window_callback(self):
        window = MainWindow()
        with patch.object(window.extraction_workspace, "publish") as publish:
            window._on_models_changed()
        self.assertEqual(window.extraction_workspace._models_at, 0)
        publish.assert_called_once_with(force=True)



    def test_system_audio_recording_enters_transcription_queue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        window._on_rec_source_selected("system_audio")

        window._toggle_record()
        self.assertIsNotNone(window._system_rec_session)
        self.assertEqual(window._rec_source, "system_audio")

        window._toggle_record()
        self.assertEqual(len(window.tasks), 1)
        self.assertIn("系统声音录音", window.tasks[0].title)
        self.assertEqual(window.tasks[0].kind, "file")
        self.assertTrue(Path(window.tasks[0].source).exists())

    def test_recording_source_buttons_are_clear_and_do_not_cross_select(self):
        window = MainWindow()
        with patch.object(window.extraction_workspace, "publish") as publish:
            for source in ("system_audio", "mixed", "microphone"):
                window._on_rec_source_selected(source)
                self.assertEqual(window._rec_source, source)
            with self.assertRaises(ValueError):
                window._on_rec_source_selected("unknown")
            self.assertEqual(window._rec_source, "microphone")
        self.assertEqual(publish.call_count, 3)
        self.assertFalse(hasattr(window, "rec_mic_btn"))





    def test_save_folder_selectors_refresh_visible_current_paths_immediately(self):
        window = MainWindow()
        directory = str(self.root / "保存的录音")
        with patch("src.ui.main_window.QFileDialog.getExistingDirectory", return_value=directory), patch.object(window.extraction_workspace, "publish") as publish:
            window._pick_recording_dir()
        self.assertEqual(window.cfg.recording_dir, directory)
        publish.assert_called_once()



    def test_system_audio_recording_cancel_does_not_enqueue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        window._on_rec_source_selected("system_audio")

        window._toggle_record()
        output_path = backend.session.request.output_path
        output_path.write_bytes(b"partial")
        window._cancel_recording()

        self.assertEqual(window.tasks, [])
        self.assertFalse(output_path.exists())
        self.assertTrue(backend.session.cancelled)

    def test_mixed_recording_enters_transcription_queue_and_cleans_tracks(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        mic = _FakeMicRecorder()
        window._recorder_obj = mic
        mic.tick.connect(window._on_rec_tick)
        mic.finished.connect(window._on_rec_finished)
        mic.failed.connect(window._on_rec_failed)
        window._analyze_recording_file = lambda path: {"is_silent": False, "duration": 1.0}  # type: ignore[method-assign]

        def fake_mix(mic_path, system_path, output_path):
            self.assertTrue(Path(mic_path).exists())
            self.assertTrue(Path(system_path).exists())
            Path(output_path).write_bytes(b"fake-mixed-audio")
            return {"is_silent": False, "duration": 1.0}

        window._mix_recording_tracks = fake_mix  # type: ignore[method-assign]

        window._on_rec_source_selected("mixed")
        window._toggle_record()
        mic_path = window._mixed_mic_path
        system_path = window._mixed_system_path
        output_path = window._mixed_output_path
        self.assertTrue(window._mixed_rec_active)
        self.assertEqual(window._rec_source, "mixed")

        window._toggle_record()

        self.assertEqual(len(window.tasks), 1)
        self.assertIn("混合录音", window.tasks[0].title)
        self.assertEqual(window.tasks[0].kind, "file")
        self.assertEqual(window.tasks[0].source, str(output_path))
        self.assertTrue(Path(window.tasks[0].source).exists())
        self.assertFalse(Path(mic_path).exists())
        self.assertFalse(Path(system_path).exists())

    def test_mixed_recording_cancel_does_not_enqueue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        mic = _FakeMicRecorder()
        window._recorder_obj = mic
        mic.tick.connect(window._on_rec_tick)
        mic.finished.connect(window._on_rec_finished)
        mic.failed.connect(window._on_rec_failed)
        window._analyze_recording_file = lambda path: {"is_silent": False}  # type: ignore[method-assign]
        window._mix_recording_tracks = lambda *_args: {"is_silent": False}  # type: ignore[method-assign]

        window._on_rec_source_selected("mixed")
        window._toggle_record()
        mic_path = window._mixed_mic_path
        system_path = window._mixed_system_path
        window._cancel_recording()

        self.assertEqual(window.tasks, [])
        self.assertFalse(Path(mic_path).exists())
        self.assertFalse(Path(system_path).exists())
        self.assertTrue(backend.session.cancelled)

    def test_mixed_recording_without_microphone_input_does_not_start(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        window._recorder_obj = _NoInputMicRecorder()
        window._system_audio_backend = _FakeSystemAudioBackend()

        window._on_rec_source_selected("mixed")
        window._toggle_record()

        self.assertEqual(window.tasks, [])
        self.assertFalse(window._mixed_rec_active)
        self.assertIsNone(window._system_rec_session)

    def test_mixed_recording_system_audio_start_failure_does_not_enqueue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        mic = _FakeMicRecorder()
        window._recorder_obj = mic
        window._system_audio_backend = _FailingSystemAudioBackend()

        window._on_rec_source_selected("mixed")
        window._toggle_record()

        self.assertEqual(window.tasks, [])
        self.assertFalse(window._mixed_rec_active)
        self.assertFalse(mic.is_recording())

    def test_mixed_recording_silent_microphone_does_not_enqueue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        mic = _FakeMicRecorder()
        window._recorder_obj = mic
        mic.finished.connect(window._on_rec_finished)
        window._analyze_recording_file = lambda path: {"is_silent": True}  # type: ignore[method-assign]
        window._mix_recording_tracks = lambda *_args: self.fail("silent mic must not be mixed")  # type: ignore[method-assign]

        window._on_rec_source_selected("mixed")
        window._toggle_record()
        window._toggle_record()

        self.assertEqual(window.tasks, [])
        self.assertFalse(window._mixed_rec_active)

    def test_mixed_recording_mix_failure_does_not_enqueue(self):
        window = MainWindow()
        window.cfg.recording_dir = str(self.root / "recordings")
        backend = _FakeSystemAudioBackend()
        window._system_audio_backend = backend
        mic = _FakeMicRecorder()
        window._recorder_obj = mic
        mic.finished.connect(window._on_rec_finished)
        window._analyze_recording_file = lambda path: {"is_silent": False}  # type: ignore[method-assign]

        def fail_mix(*_args):
            raise RuntimeError("mix failed")

        window._mix_recording_tracks = fail_mix  # type: ignore[method-assign]

        window._on_rec_source_selected("mixed")
        window._toggle_record()
        window._toggle_record()

        self.assertEqual(window.tasks, [])
        self.assertFalse(window._mixed_rec_active)

    def test_microphone_recording_finish_still_enters_queue(self):
        window = MainWindow()
        mic_path = self.root / "recordings" / "mic.m4a"
        mic_path.parent.mkdir(parents=True, exist_ok=True)
        mic_path.write_bytes(b"fake-mic-audio")
        window._rec_active_source = "microphone"
        window._rec_title = "麦克风录音 07-02 02:30"

        window._on_rec_finished(str(mic_path))

        self.assertEqual(len(window.tasks), 1)
        self.assertEqual(window.tasks[0].title, "麦克风录音 07-02 02:30")
        self.assertEqual(window.tasks[0].source, str(mic_path))


    def test_library_long_markdown_scroll_reaches_final_timestamp(self):
        relative_cover = Path("douyin") / "long-scroll-cover.jpg"
        cover = self.root / "collection" / "covers" / relative_cover
        cover.parent.mkdir(parents=True)
        image = QImage(440, 330, QImage.Format.Format_RGB32)
        image.fill(QColor("#e6c2a1"))
        image.save(str(cover))
        app_data_patch = patch(
            "src.core.models_manager.app_data_dir",
            return_value=self.root,
        )
        app_data_patch.start()
        self.addCleanup(app_data_patch.stop)
        long_text = "\n\n".join(
            f"**[{index // 60:02d}:{index % 60:02d}]** 第 {index} 段正文"
            for index in range(92)
        )
        transcript = Transcript(
            text=long_text,
            segments=[],
            language="zh",
            engine="local_whisper_mlx",
            source="unit-source.wav",
            title="长逐字稿滚动到底测试",
            meta={
                "platform": "douyin",
                "work_id": "long-scroll",
                "webpage_url": "https://www.douyin.com/video/long-scroll",
                "original_url": "https://www.douyin.com/video/long-scroll",
                "media_url": "https://video.example.test/path?" + ("token=abcdef&" * 80),
                "duration": 115,
                "upload_date": "20260807",
                "cover_platform": "douyin",
                "cover_work_id": "long-scroll",
                "cover_url": "https://p3.example.test/cover.jpg",
                "cover_status": "downloaded",
                "cover_local_path": relative_cover.as_posix(),
                "stats": {"like": 1846, "favorite": 763, "share": 470, "comment": 91},
                "author": {"name": "测试作者", "fans": 2854000, "likes_total": 18857961, "works": 1252},
            },
        )
        entry_id = library.save(transcript, "长逐字稿滚动到底测试")

        window = MainWindow()
        window.resize(1400, 900)
        window.show()
        self.app.processEvents()
        window.open_library("transcript")
        window._view_entry(entry_id)
        self.app.processEvents()

        # V3.7 reads library documents in the permanent editor. Verify the same
        # user outcome (the final timestamp really becomes visible) there.
        from tests.test_agent_workspace_ui import AgentWorkspaceUiTests
        probe = AgentWorkspaceUiTests()
        probe.app, probe.widget = self.app, window.library_page
        try:
            probe.until(lambda: probe.widget.ready)
            probe.until(lambda: probe.js('Boolean(document.querySelector(".ProseMirror"))'))
            self.assertTrue(probe.js('document.querySelector(".ProseMirror").innerText.includes("第 91 段正文")'))
            self.assertTrue(probe.js('document.querySelector("#document-scroll").scrollHeight > document.querySelector("#document-scroll").clientHeight'))
            probe.js('document.querySelector("#document-scroll").scrollTop = document.querySelector("#document-scroll").scrollHeight')
            probe.until(lambda: probe.js('''(() => {
                const scroll = document.querySelector('#document-scroll').getBoundingClientRect();
                const last = document.querySelector('.ProseMirror').lastElementChild;
                const rect = last.getBoundingClientRect();
                return last.innerText.includes('[01:31]') && last.innerText.includes('第 91 段正文')
                    && rect.top >= scroll.top && rect.bottom <= scroll.bottom;
            })()'''))
        finally:
            probe.widget.shutdown()
            window.close()
            window.deleteLater()
            self.app.sendPostedEvents(None, QEvent.DeferredDelete)


    def test_every_active_global_shell_nav_uses_consumer_accent(self):
        window = MainWindow()
        window.show()
        self.app.processEvents()

        navs = {
            label.text(): (index, kind, row, icon, label, icon_name)
            for index, kind, row, icon, label, icon_name in window._page_navs
        }
        transcription = navs["文案提取"][2]
        copywriting = navs["文稿库"][2]
        self.assertTrue(transcription.property("selected"))
        self.assertTrue(transcription.property("consumerAccent"))
        self.assertFalse(bool(copywriting.property("consumerAccent")))

        window._switch_page(3)
        self.app.processEvents()
        self.assertFalse(transcription.property("selected"))
        self.assertFalse(transcription.property("consumerAccent"))
        self.assertTrue(copywriting.property("selected"))
        self.assertTrue(copywriting.property("consumerAccent"))

        window._switch_page(0)
        self.app.processEvents()
        self.assertTrue(transcription.property("selected"))
        self.assertTrue(transcription.property("consumerAccent"))
        self.assertIn(
            'QFrame#sidebar[consumerThemeScope="globalShell"] '
            'QFrame[luluControl="navRow"][consumerAccent="true"]',
            T.QSS,
        )
        window.close()

    def test_library_search_stays_inside_current_left_category(self):
        transcript = Transcript(
            text="苹果转录文本",
            segments=[],
            language="zh",
            engine="test",
            source="unit",
            title="苹果转录",
        )
        library.save(transcript, "苹果转录")
        library.save_voice(
            title="香蕉配音",
            audio_path=self.root / "missing.wav",
            text="香蕉配音文本",
            engine="IndexTTS2",
            voice_name="测试音色",
            config_summary="情感模板:自然",
            source_type="manual",
        )

        from src.core.agent_workspace.service import WorkspaceService
        service = WorkspaceService()
        self.assertEqual(service.list(kind="voice", search="苹果")["entries"], [])
        matches = service.list(kind="voice", search="香蕉")["entries"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title"], "香蕉配音")


    def test_workbench_pages_share_top_layout_and_visible_pane_rails(self):
        window = MainWindow()
        window.resize(1360, 880)
        window.show()
        self.app.processEvents()

        for index in (0, 4):
            window._switch_page(index)
            self.app.processEvents()
            self._assert_pane_rails_ready(window)

        for _ in range(3):
            window.rail_left.click()
            QTest.qWait(320)
            self.app.processEvents()
            self._assert_pane_rails_ready(window)

        window._switch_page(0)
        self.app.processEvents()
        workbench_top = self._widget_top(window, window.stack.widget(0))
        window._switch_page(4)
        self.app.processEvents()
        voice_top = self._widget_top(window, window.voice_page)
        self.assertLessEqual(abs(workbench_top - voice_top), 12)


    def test_homepage_batch_transcript_merges_author_and_interaction_facts_before_save(self):
        item = CollectedItem(
            aweme_id="735002",
            author_name="作者甲",
            title="批量作品",
            like=12,
            collect=8,
            comment=3,
            share=2,
            author_id="author-1",
            extra_stats={"author_fans": "1万+"},
        )
        transcript = Transcript(
            text="批量转录文稿",
            meta={"stats": {"view": 500}, "author": {"verify": "已认证"}},
        )
        task = SimpleNamespace(
            transcript=transcript,
            stable_key=item.stable_key,
            execution_policy=SimpleNamespace(origin="homepage_batch_transcribe"),
        )
        fake_window = SimpleNamespace(
            collection_page=SimpleNamespace(_items=[item]),
        )

        MainWindow._merge_collection_transcript_facts(fake_window, task)

        self.assertEqual(
            transcript.meta["stats"],
            {"view": 500, "like": 12, "favorite": 8, "comment": 3, "share": 2},
        )
        self.assertEqual(transcript.meta["author"]["name"], "作者甲")
        self.assertEqual(transcript.meta["author"]["fans"], "1万+")
        self.assertEqual(transcript.meta["author"]["verify"], "已认证")
        self.assertEqual(
            transcript.source,
            "https://www.douyin.com/video/735002",
        )


if __name__ == "__main__":
    unittest.main()
