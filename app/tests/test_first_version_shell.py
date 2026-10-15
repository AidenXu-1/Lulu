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


if __name__ == "__main__":
    unittest.main()
