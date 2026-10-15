"""P3A 主页采集页接线的离屏、fake-first 测试。"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from src.core.collect import export  # noqa: E402
from src.core import feishu  # noqa: E402
from src.core.audio import extractor  # noqa: E402
from src.config import AppConfig, COLLECT_COVER_CONTRACT_VERSION  # noqa: E402
from src.core.collect.application import (  # noqa: E402
    CollectionApplicationService,
    CollectionCoverSummary,
    CollectionExecutionResult,
    XHS_RELEASE_DISABLED_MESSAGE,
    XhsBrowserStatus,
)
from src.core.collect.contracts import AdapterCapability, CollectResult  # noqa: E402
from src.core.collect.history import CollectionHistoryRepository, HistoryError  # noqa: E402
from src.core.collect.models import CollectedItem  # noqa: E402
from src.core.feishu_storage import WorkArchiveRecord  # noqa: E402
from src.core.collect.preferences import (  # noqa: E402
    MaterialPreferenceService,
    MaterialPreferenceSnapshot,
)
from src.core.collect.repository import CollectionRepository, RepositoryError  # noqa: E402
from src.core.task import CollectionItemRuntimeSnapshot  # noqa: E402
from src.ui import collection_page  # noqa: E402
from src.ui import theme as T  # noqa: E402
from src.ui.collection_page import CollectionPage  # noqa: E402
from src.ui.control_foundation import PaperTextureFrame  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402


DOUYIN_URL = "https://www.douyin.com/user/MS4w.fixture"
XHS_URL = "https://www.xiaohongshu.com/user/profile/public_brand"
BILI_URL = "https://space.bilibili.com/123"


def douyin_item(work_id: str = "100") -> CollectedItem:
    return CollectedItem(
        work_id,
        author_name="抖音作者",
        title="抖音视频",
        play_url=f"https://media.example/{work_id}.mp4",
    )


def xhs_item(
    work_id: str = "note_1",
    *,
    content_kind: str = "video",
    known: dict[str, int] | None = None,
) -> CollectedItem:
    values = dict(known or {})
    states = {f"_field_state_{name}": "known" for name in values}
    if content_kind == "image":
        values["duration_ms"] = 0
        states["_field_state_duration_ms"] = "not_applicable"
    return CollectedItem(
        "",
        platform="xiaohongshu",
        work_id=work_id,
        author_name="小红书作者",
        title="公开作品",
        work_url=f"https://www.xiaohongshu.com/explore/{work_id}",
        content_kind=content_kind,
        extra_stats=states,
        **values,
    )


class FakeApplicationService:
    def __init__(self, result: CollectResult | None = None) -> None:
        self._pure = CollectionApplicationService(
            adapter_resolver=lambda _platform: (_ for _ in ()).throw(
                AssertionError("UI inspection must not resolve adapters")
            ),
            session_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("UI inspection must not open a browser")
            ),
        )
        self.result = result or CollectResult.complete([xhs_item()])
        self.cover_summary = CollectionCoverSummary()
        self.commands = []
        self.closed = 0
        self.opened = 0
        self.released = 0
        self.browser_status = XhsBrowserStatus()
        self.close_result: XhsBrowserStatus | None = None

    def inspect(self, raw_input: str):
        return self._pure.inspect(raw_input)

    def collect(self, command, *, progress=None, is_cancelled=None):
        self.commands.append(command)
        if progress:
            progress("只读采集", len(self.result.items))
        if is_cancelled and is_cancelled():
            return CollectResult.cancelled()
        return self.result

    def collect_with_cover(self, command, *, progress=None, is_cancelled=None):
        return CollectionExecutionResult(
            self.collect(
                command,
                progress=progress,
                is_cancelled=is_cancelled,
            ),
            self.cover_summary,
        )

    def run_profile_login(self, _platform, *, progress=None, is_cancelled=None, **_kwargs):
        if progress:
            progress("请在专用窗口完成登录", 0)
        if is_cancelled and is_cancelled():
            return "cancelled"
        self.opened += 1
        self.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        return "ready"

    def xhs_browser_status(self, *, refresh=False):
        _ = refresh
        return self.browser_status

    def close_xhs_browser(self):
        self.closed += 1
        if self.close_result is not None:
            self.browser_status = self.close_result
        else:
            self.browser_status = XhsBrowserStatus()
        return self.browser_status

    def release_xhs_browser_control(self):
        self.released += 1
        running = self.browser_status.running
        self.browser_status = XhsBrowserStatus(
            "retained" if running else "absent",
            running,
            False,
            (
                "专用浏览器保持打开，Lulu 已释放本次控制权。"
                if running
                else "小红书专用浏览器尚未打开。"
            ),
        )
        return self.browser_status

    def xhs_task_active(self):
        return False

    def close_active_session(self):
        return True


class ReleaseDisabledApplicationService(FakeApplicationService):
    def __init__(self) -> None:
        super().__init__()
        self._pure = CollectionApplicationService(
            adapter_resolver=lambda _platform: (_ for _ in ()).throw(
                AssertionError("release-disabled UI must not resolve adapters")
            ),
            session_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("release-disabled UI must not open a browser")
            ),
            capability_resolver=lambda platform: (
                AdapterCapability("xiaohongshu", False, "release_disabled")
                if platform == "xiaohongshu"
                else self._pure_capability(platform)
            ),
        )
        self.status_reads = 0

    @staticmethod
    def _pure_capability(platform):
        from src.core.collect.contracts import adapter_capability

        return adapter_capability(platform)

    def xhs_browser_status(self, *, refresh=False):
        _ = refresh
        self.status_reads += 1
        raise AssertionError("release-disabled UI must not inspect browser status")


class BlockingApplicationService(FakeApplicationService):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.cancel_seen = threading.Event()

    def collect(self, command, *, progress=None, is_cancelled=None):
        self.commands.append(command)
        self.started.set()
        while not (is_cancelled and is_cancelled()):
            time.sleep(0.002)
        self.cancel_seen.set()
        return CollectResult.cancelled()


class DelayedLoginApplicationService(FakeApplicationService):
    def __init__(self) -> None:
        super().__init__()
        self.login_entered = threading.Event()
        self.login_ready = threading.Event()

    def run_profile_login(
        self,
        _platform,
        *,
        progress=None,
        is_cancelled=None,
        **_kwargs,
    ):
        self.opened += 1
        if progress:
            progress("正在等待同一个专用浏览器可信就绪…", 0)
        self.login_entered.set()
        if not self.login_ready.wait(1.0):
            raise AssertionError("fixture late readiness was not released")
        if is_cancelled and is_cancelled():
            return "cancelled"
        self.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        return "ready"


class FailingRecordsRepository:
    def __init__(self, delegate: CollectionRepository, code: str) -> None:
        self.delegate = delegate
        self.code = code

    def load(self):
        return self.delegate.load()

    def upsert(self, *_args, **_kwargs):
        raise RepositoryError(self.code)


class FailingHistoryRepository:
    def __init__(self, delegate: CollectionHistoryRepository, code: str) -> None:
        self.delegate = delegate
        self.code = code

    def load(self):
        return self.delegate.load()

    def add_record(self, *_args, **_kwargs):
        raise HistoryError(self.code)


class CollectionP3AUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        T.apply_theme("warm")
        self.service = FakeApplicationService()
        self.cfg = SimpleNamespace(
            collect_cover=False,
            feishu_target={},
            feishu_creation_target={},
            feishu_table_targets=[],
            feishu_storage_preferences={},
            last_output_dir=str(Path(tempfile.gettempdir()) / "lulu-test-output"),
            media_dir=str(Path(tempfile.gettempdir()) / "lulu-test-media"),
            save=lambda: True,
        )
        self.login_patch = patch("src.ui.collection_page.is_logged_in", return_value=False)
        self.login_patch.start()
        self.bitable_choice_patch = patch(
            "src.ui.collection_page.dialogs.choose",
            return_value=0,
        )
        self.bitable_choice_patch.start()
        self.page = CollectionPage(self.cfg, collection_service=self.service)
        self.page.resize(1200, 760)

    def tearDown(self):
        self.page.shutdown_workers()
        self.page.deleteLater()
        self.app.processEvents()
        self.bitable_choice_patch.stop()
        self.login_patch.stop()

    def _wait_feishu_preparation(self):
        deadline=time.monotonic()+2
        while getattr(self.page,'_feishu_prepare_worker',None) is not None and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(0.002)
        self.app.processEvents()
        self.assertIsNone(getattr(self.page,'_feishu_prepare_worker',None))

    def _wait_workers(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.page._collect_running():
                self.app.processEvents()
                return
            time.sleep(0.002)
        self.fail("collection worker did not stop")

    def _wait_material_worker(self, page, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if page._material_worker is None:
                self.app.processEvents()
                return
            time.sleep(0.002)
        self.fail("material worker did not stop")


    def test_homepage_media_fetch_retries_work_page_and_forces_video(self):
        current = replace(
            douyin_item("7663085744302476579"),
            play_url=(
                "https://v26-weba.douyinvod.com/video/fallback.mp4?"
                + "token="
                + "x" * 520
            ),
            work_url="https://www.douyin.com/video/7663085744302476579",
        )
        direct_calls = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            refreshed = replace(
                current,
                play_url="https://v26-weba.douyinvod.com/video/refreshed.mp4",
            )

            def direct(source, destination, _progress, *, work_id):
                direct_calls.append((source, Path(destination), work_id))
                if len(direct_calls) == 1:
                    raise RuntimeError("expired direct url")
                target = Path(destination) / "refreshed.mp4"
                target.write_bytes(b"video")
                return target

            with (
                patch(
                    "src.core.audio.extractor.download_douyin_video_cdn",
                    side_effect=direct,
                ),
                patch(
                    "src.core.collect.douyin_fetch.fetch_item",
                    return_value=refreshed,
                ) as refresh,
                patch("src.core.audio.extractor.resolve_source") as generic,
            ):
                fetched = collection_page._fetch_managed_media(
                    current,
                    "audio",
                    root,
                    lambda: False,
                )

            self.assertEqual(fetched.read_bytes(), b"video")
            refresh.assert_called_once_with(current.work_id)
            generic.assert_not_called()
            self.assertEqual(
                direct_calls,
                [
                    (current.play_url, root / "attempt-1", current.work_id),
                    (refreshed.play_url, root / "attempt-2", current.work_id),
                ],
            )


    def test_collection_mode_control_persists_and_rolls_back(self):
        class ModeConfig(SimpleNamespace):
            def save(self):
                self.save_calls += 1
                return self.save_result

        success = ModeConfig(
            collect_cover=False,
            feishu_target={},
            collection_mode="partial",
            save_calls=0,
            save_result=True,
        )
        page = CollectionPage(success, collection_service=FakeApplicationService())
        try:
            page.m_full_btn.click()
            self.app.processEvents()
            self.assertEqual(success.collection_mode, "full")
            self.assertEqual(success.save_calls, 1)
            self.assertTrue(page.quick_panel.isHidden())
            self.assertFalse(page.full_panel.isHidden())
            self.assertEqual(page.full_btn.text(), "先登录专用浏览器")
            self.assertIn("默认快速全量采集", page.collection_hint.text())
            self.assertNotIn("低风险", page.collection_hint.text())
            self.assertFalse(hasattr(page, "mode_low_btn"))
            with patch.object(page, "_on_login") as login:
                page.full_btn.click()
            login.assert_called_once_with()

            restored = CollectionPage(
                success,
                collection_service=FakeApplicationService(),
            )
            try:
                self.assertTrue(restored.m_full_btn.isChecked())
                self.assertTrue(restored.quick_panel.isHidden())
                self.assertEqual(restored.full_btn.text(), "先登录专用浏览器")
            finally:
                restored.shutdown_workers()
                restored.deleteLater()
        finally:
            page.shutdown_workers()
            page.deleteLater()

        failed = ModeConfig(
            collect_cover=False,
            feishu_target={},
            collection_mode="partial",
            save_calls=0,
            save_result=False,
        )
        page = CollectionPage(failed, collection_service=FakeApplicationService())
        try:
            page.m_full_btn.click()
            self.app.processEvents()
            self.assertEqual(failed.collection_mode, "partial")
            self.assertTrue(page.m_quick_btn.isChecked())
            self.assertFalse(page.quick_panel.isHidden())
            self.assertTrue(page.full_panel.isHidden())
            self.assertEqual(page.grab_btn.text(), "开始部分采集")
            self.assertIn("仍使用部分采集", page.status.text())
        finally:
            page.shutdown_workers()
            page.deleteLater()


    def _wait_page_workers(self, page, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not page._collect_running():
                self.app.processEvents()
                return
            time.sleep(0.002)
        self.fail("collection worker did not stop")


if __name__ == "__main__":
    unittest.main()
