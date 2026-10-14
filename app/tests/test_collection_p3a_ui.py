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

    def test_platform_entry_is_local_and_bilibili_fails_closed(self):
        self.assertEqual(
            self.page.page_meta.text(),
            "贴一个抖音或小红书公开主页链接，Lulu 会识别平台。采集数据只留在你电脑上。",
        )
        self.assertEqual(
            self.page.input.placeholderText(),
            "粘贴抖音或小红书公开主页链接",
        )
        self.assertEqual(self.page.method_title.text(), "采集方式")
        self.assertEqual(self.page.grab_btn.text(), "开始部分采集")
        self.assertEqual(
            self.page.quick_tip.text(),
            "公开方式 · 无需打开专用浏览器",
        )

        self.page.input.setText(XHS_URL)
        self.assertEqual(self.page.platform_status.text(), "小红书")
        self.assertTrue(self.page.method_segment.isHidden())
        self.assertFalse(self.page.xhs_login_btn.isHidden())
        self.assertTrue(self.page.grab_btn.isEnabled())

        self.page.input.setText(BILI_URL)
        self.assertIn("暂不可用", self.page.platform_status.text())
        self.assertFalse(self.page.grab_btn.isEnabled())
        self.page._on_grab()
        self.assertEqual(self.service.commands, [])

    def test_collection_mode_normalizes_persists_and_rolls_back_as_one_state(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "src.config._CONFIG_PATH",
            Path(raw) / "config.json",
        ):
            config_path = Path(raw) / "config.json"
            config_path.write_text(
                '{"collection_mode":"invalid","collect_cover":false}',
                encoding="utf-8",
            )
            loaded = AppConfig.load()
            self.assertEqual(loaded.collection_mode, "partial")
            loaded.collection_mode = "full"
            self.assertTrue(loaded.save())
            self.assertEqual(AppConfig.load().collection_mode, "full")

    def test_existing_bitable_preference_roundtrips_inside_feishu_target(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "src.config._CONFIG_PATH",
            Path(raw) / "config.json",
        ):
            config = AppConfig()
            config.feishu_target = {
                "space_id": "SPACE1",
                "node_token": "NODE1",
                "name": "内容中心",
                "bitable_mode": "existing",
                "bitable_url": "https://example.feishu.cn/base/APP1?table=TBL1",
                "bitable_name": "长期素材库",
            }
            self.assertTrue(config.save())
            loaded = AppConfig.load()

        self.assertEqual(loaded.feishu_target, config.feishu_target)

    def test_shared_material_preferences_sync_independently_and_rollback_both_pages(self):
        values = dict(self.cfg.__dict__)
        values.update(
            link_media_mode="audio",
            keep_media=False,
            collect_cover_contract_version=0,
            save_result=True,
        )
        cfg = SimpleNamespace(**values)
        cfg.save = lambda: cfg.save_result
        service = MaterialPreferenceService(cfg)
        first = CollectionPage(
            cfg,
            collection_service=FakeApplicationService(),
            cover_preference_service=service,
        )
        second = CollectionPage(
            cfg,
            collection_service=FakeApplicationService(),
            cover_preference_service=service,
        )
        try:
            first._on_save_media_toggled(True)
            self.assertTrue(first.save_media_cb.isChecked())
            self.assertTrue(second.save_media_cb.isChecked())
            self.assertFalse(first.cover_cb.isChecked())
            self.assertFalse(second.cover_cb.isChecked())

            cfg.save_result = False
            with patch("src.ui.collection_page.dialogs.alert") as alert:
                second._on_cover_preference_toggled(True)
            self.assertFalse(first.cover_cb.isChecked())
            self.assertFalse(second.cover_cb.isChecked())
            alert.assert_called_once_with(
                second,
                "设置未保存",
                "设置没有保存，两处仍按原设置执行。",
            )
        finally:
            first.shutdown_workers()
            second.shutdown_workers()
            first.deleteLater()
            second.deleteLater()

    def test_material_preference_responsive_order_and_zero_task_exits(self):
        self.assertFalse(hasattr(self.page, "material_managed_note"))
        self.assertFalse(hasattr(self.page, "material_mode_segment"))
        self.assertFalse(hasattr(self.page, "material_audio_btn"))
        self.assertFalse(hasattr(self.page, "material_video_btn"))
        self.assertNotIn(
            "素材由 Lulu 管理，可在“数据位置”查看。",
            "\n".join(label.text() for label in self.page.findChildren(QLabel)),
        )
        self.assertEqual(self.page.save_media_cb.text(), "音视频文件")
        self.assertEqual(self.page.cover_cb.text(), "封面文件")
        layout = self.page.material_preferences_layout
        widgets = (self.page.save_media_cb, self.page.cover_cb)
        for width in (720, 390, 320, 240):
            with self.subTest(width=width):
                self.page._layout_material_preferences(width)
                actual = tuple(
                    layout.getItemPosition(layout.indexOf(widget))[0]
                    for widget in widgets
                )
                self.assertEqual(actual, (0, 0))
        self.assertIs(
            self.page.save_media_cb.parentWidget(),
            self.page.material_preferences_wrap,
        )
        self.page.show()
        self.app.processEvents()
        self.assertLess(
            self.page.material_preferences_wrap.geometry().center().y(),
            self.page.quick_panel.geometry().top(),
        )

        # 文案提取页仍可保留「仅音频」偏好，但主页发起的素材始终是完整视频。
        self.page.cfg.link_media_mode = "audio"
        snapshot = self.page._material_download_snapshot()
        self.assertEqual(snapshot.download_mode, "video")

        self.page._selected_keys.clear()
        self.page._download_selected_materials()
        self.assertEqual(self.page.status.text(), "请先选择作品")
        self.assertIsNone(self.page._material_worker)

        selected = xhs_item("no_material")
        self.page._items = [selected]
        self.page._selected_keys = {selected.stable_key}
        self.page._download_selected_materials()
        self.assertEqual(self.page.status.text(), "请先选择要保存的素材")
        self.assertIsNone(self.page._material_worker)

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

    def test_homepage_media_fetch_uses_fixed_cdn_file_without_ytdlp(self):
        current = replace(
            douyin_item("7663085744302476579"),
            play_url=(
                "https://v26-weba.douyinvod.com/video/direct.mp4?"
                + "very_long_query="
                + "x" * 720
            ),
            work_url="https://www.douyin.com/video/7663085744302476579",
        )
        direct_calls = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def direct(source, destination, _progress, *, work_id):
                direct_calls.append((source, Path(destination), work_id))
                target = Path(destination) / "douyin-video.mp4"
                target.write_bytes(b"\x00\x00\x00\x18ftypmp42video")
                return target

            with (
                patch(
                    "src.core.audio.extractor.download_douyin_video_cdn",
                    side_effect=direct,
                ),
                patch("src.core.audio.extractor.resolve_source") as generic,
            ):
                fetched = collection_page._fetch_managed_media(
                    current,
                    "video",
                    root,
                    lambda: False,
                )

            self.assertEqual(fetched.name, "douyin-video.mp4")
            self.assertEqual(
                direct_calls,
                [(current.play_url, root / "attempt-1", current.work_id)],
            )
            generic.assert_not_called()

    def test_douyin_video_cdn_requires_strict_official_https_host(self):
        valid = "https://v26-weba.douyinvod.com/video/file.mp4?" + "x" * 700
        self.assertTrue(extractor.is_douyin_video_cdn(valid))
        for source in (
            "http://v26-weba.douyinvod.com/video/file.mp4",
            "https://user:secret@v26-weba.douyinvod.com/video/file.mp4",
            "https://v26-weba.douyinvod.com:443/video/file.mp4",
            "https://douyinvod.com.evil.test/video/file.mp4",
            "https://127.0.0.1/video/file.mp4",
        ):
            with self.subTest(source=source):
                self.assertFalse(extractor.is_douyin_video_cdn(source))

    def test_direct_cdn_downloader_uses_short_name_and_redirect_validator(self):
        source = "https://v26-weba.douyinvod.com/video/file.mp4?" + "q=" + "x" * 720
        captured = {}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def stream(url, destination, _progress, **kwargs):
                captured.update(url=url, destination=destination, kwargs=kwargs)
                destination.write_bytes(b"\x00\x00\x00\x18ftypmp42video")
                return destination

            with patch(
                "src.core.audio.extractor._stream_download",
                side_effect=stream,
            ):
                fetched = extractor.download_douyin_video_cdn(
                    source,
                    root,
                    lambda *_args: None,
                    work_id="7663085744302476579",
                )

        self.assertEqual(fetched.name, "douyin-video.mp4")
        self.assertEqual(captured["destination"].name, "douyin-video.mp4")
        self.assertEqual(captured["kwargs"]["max_retries"], 2)
        self.assertEqual(
            captured["kwargs"]["referer"],
            "https://www.douyin.com/video/7663085744302476579",
        )
        self.assertIs(
            captured["kwargs"]["url_validator"],
            extractor.is_douyin_video_cdn,
        )

    def test_direct_cdn_downloader_rejects_untrusted_work_identity_before_network(self):
        source = "https://v26-weba.douyinvod.com/video/file.mp4"
        for work_id in (
            "",
            "not-digits",
            "7663085744302476579\r\nX-Leak: true",
        ):
            with self.subTest(work_id=work_id), tempfile.TemporaryDirectory() as raw:
                with patch("src.core.audio.extractor._stream_download") as stream:
                    with self.assertRaisesRegex(RuntimeError, "作品身份"):
                        extractor.download_douyin_video_cdn(
                            source,
                            Path(raw),
                            work_id=work_id,
                        )
                stream.assert_not_called()

    def test_direct_cdn_redirect_cannot_leave_allowed_host(self):
        source = "https://v26-weba.douyinvod.com/video/source.mp4"
        handler = extractor._ValidatedMediaRedirectHandler(
            extractor.is_douyin_video_cdn
        )
        request = urllib.request.Request(
            source,
            headers={
                "Referer": "https://www.douyin.com/video/7663085744302476579"
            },
        )

        with self.assertRaisesRegex(RuntimeError, "非允许域名"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://127.0.0.1/private.mp4",
            )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://v3.douyinvod.com/video/next.mp4",
        )
        self.assertEqual(
            redirected.full_url,
            "https://v3.douyinvod.com/video/next.mp4",
        )
        self.assertEqual(
            redirected.get_header("Referer"),
            "https://www.douyin.com/video/7663085744302476579",
        )

    def test_homepage_media_fetch_rejects_outside_and_empty_video_results(self):
        current = replace(
            douyin_item("unsafe"),
            work_url="https://www.douyin.com/video/unsafe",
        )
        refreshed = replace(
            current,
            play_url="https://v26-weba.douyinvod.com/video/unsafe.mp4",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "downloads"
            root.mkdir()
            outside = Path(raw) / "outside.mp4"
            outside.write_bytes(b"outside")

            with (
                patch(
                    "src.core.collect.douyin_fetch.fetch_item",
                    return_value=refreshed,
                ),
                patch(
                    "src.core.audio.extractor.download_douyin_video_cdn",
                    return_value=outside,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "播放地址已过期"):
                    collection_page._fetch_managed_media(
                        current,
                        "video",
                        root,
                        lambda: False,
                    )

            def empty(_source, destination, *_args, **_kwargs):
                target = Path(destination) / "empty.mp4"
                target.write_bytes(b"")
                return target

            retry_root = Path(raw) / "empty-downloads"
            retry_root.mkdir()
            with (
                patch(
                    "src.core.collect.douyin_fetch.fetch_item",
                    return_value=refreshed,
                ),
                patch(
                    "src.core.audio.extractor.download_douyin_video_cdn",
                    side_effect=empty,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "播放地址已过期"):
                    collection_page._fetch_managed_media(
                        current,
                        "video",
                        retry_root,
                        lambda: False,
                    )

    def test_auto_collect_and_selected_retry_both_freeze_full_video(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            records = CollectionRepository(root / "records.json")
            history = CollectionHistoryRepository(root / "history.json")
            cfg = SimpleNamespace(
                collect_cover=False,
                collect_cover_contract_version=COLLECT_COVER_CONTRACT_VERSION,
                link_media_mode="audio",
                keep_media=True,
                feishu_target={},
                last_output_dir=str(root / "output"),
                media_dir=str(root / "transcript-media"),
                save=lambda: True,
            )
            service = FakeApplicationService()
            automatic = replace(
                douyin_item("automatic-video"),
                work_url="https://www.douyin.com/video/automatic-video",
            )
            service.result = CollectResult.complete([automatic])
            page = CollectionPage(
                cfg,
                collection_service=service,
                history_repository=history,
                records_repository=records,
            )
            modes = []

            def fetch(current, mode, destination, _cancel):
                modes.append((current.stable_key, mode))
                target = Path(destination) / f"{current.work_id}.mp4"
                target.write_bytes(b"video")
                return target

            try:
                page.input.setText(DOUYIN_URL)
                with patch(
                    "src.ui.collection_page._fetch_managed_media",
                    side_effect=fetch,
                ):
                    page._on_grab()
                    self._wait_page_workers(page)
                    self._wait_material_worker(page)

                    late = replace(
                        douyin_item("selected-video"),
                        work_url="https://www.douyin.com/video/selected-video",
                    )
                    records.upsert([late])
                    page._set_items([late])
                    page._selected_keys = {late.stable_key}
                    page._download_selected_materials()
                    self._wait_material_worker(page)

                self.assertEqual(
                    modes,
                    [
                        (automatic.stable_key, "video"),
                        (late.stable_key, "video"),
                    ],
                )
                saved = {item.stable_key: item for item in records.load().items}
                self.assertEqual(saved[automatic.stable_key].media_status, "downloaded")
                self.assertEqual(saved[late.stable_key].media_status, "downloaded")
                self.assertTrue(saved[automatic.stable_key].media_local_path.endswith(".mp4"))
                self.assertTrue(saved[late.stable_key].media_local_path.endswith(".mp4"))
            finally:
                page.shutdown_workers()
                page.deleteLater()
                self.app.processEvents()

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

    def test_full_mode_freezes_command_and_running_exit_as_one_state(self):
        service = BlockingApplicationService()
        config = SimpleNamespace(
            collect_cover=False,
            feishu_target={},
            collection_mode="full",
        )
        page = CollectionPage(config, collection_service=service)
        page.input.setText(DOUYIN_URL)
        try:
            with patch(
                "src.ui.collection_page.is_logged_in",
                return_value=True,
            ), patch(
                "src.ui.collection_page.dialogs.confirm",
                return_value=True,
            ):
                page._refresh_login_status()
                self.assertEqual(page.full_btn.text(), "开始全量采集")
                page.full_btn.click()
                self.assertTrue(service.started.wait(1.0))
                self.app.processEvents()
                self.assertEqual(page._active_collection_mode, "full")
                self.assertFalse(page.m_quick_btn.isEnabled())
                self.assertFalse(page.m_full_btn.isEnabled())
                self.assertEqual(page.full_btn.text(), "停止全量采集")
                self.assertEqual(service.commands[0].mode, "profile_all")
                self.assertEqual(service.commands[0].browser_mode, "efficiency")
                page.full_btn.click()
                self.assertEqual(page.full_btn.text(), "正在停止…")
                self._wait_page_workers(page)
            self.assertEqual(page._active_collection_mode, "")
            self.assertTrue(page.m_full_btn.isChecked())
            self.assertTrue(page.m_quick_btn.isEnabled())
            self.assertTrue(service.cancel_seen.is_set())
        finally:
            page.shutdown_workers()
            page.deleteLater()

    def test_release_disabled_xhs_hides_runtime_entries_without_side_effects(self):
        service = ReleaseDisabledApplicationService()
        page = CollectionPage(self.cfg, collection_service=service)
        page.resize(1200, 760)
        try:
            self.assertEqual(
                page.page_meta.text(),
                "贴一个抖音公开主页链接，Lulu 会识别并采集。采集数据只留在你电脑上。",
            )
            self.assertEqual(
                page.input.placeholderText(),
                "粘贴抖音公开主页链接",
            )
            self.assertEqual(page.method_title.text(), "抖音主页采集")
            self.assertEqual(page.quick_tip.text(), XHS_RELEASE_DISABLED_MESSAGE)
            self.assertEqual(page.grab_btn.text(), "开始抖音部分采集")
            self.assertTrue(page.grab_btn.isEnabled())
            self.assertFalse(page.grab_btn.isHidden())
            page._on_grab()
            self.assertEqual(service.commands, [])

            preserved = xhs_item("history_preserved")
            page._set_items([preserved])
            baseline_reads = service.status_reads
            page.input.setText(XHS_URL)

            self.assertEqual(page.platform_status.text(), "小红书 · 本版暂缓")
            self.assertEqual(page.quick_summary.text(), XHS_RELEASE_DISABLED_MESSAGE)
            self.assertFalse(page.grab_btn.isEnabled())
            self.assertTrue(page.grab_btn.isHidden())
            self.assertTrue(page.xhs_login_btn.isHidden())
            self.assertTrue(page.xhs_close_btn.isHidden())
            self.assertTrue(page.xhs_browser_status.isHidden())

            with patch(
                "src.ui.collection_page.dialogs.confirm",
                side_effect=AssertionError("release gate must precede dialogs"),
            ):
                page._on_grab()
                page._on_xhs_login()
                page._on_xhs_close()

            self.assertEqual(page.status.text(), XHS_RELEASE_DISABLED_MESSAGE)
            self.assertEqual(service.commands, [])
            self.assertEqual((service.opened, service.closed), (0, 0))
            self.assertEqual(service.status_reads, baseline_reads)
            self.assertEqual(
                [item.stable_key for item in page._items],
                [preserved.stable_key],
            )

            page.input.setText(DOUYIN_URL)
            self.assertFalse(page.grab_btn.isHidden())
            self.assertTrue(page.grab_btn.isEnabled())
            self.assertEqual(page.method_title.text(), "抖音主页采集")
            self.assertEqual(page.quick_tip.text(), XHS_RELEASE_DISABLED_MESSAGE)
            self.assertEqual(page.grab_btn.text(), "开始抖音部分采集")
        finally:
            page.shutdown_workers()
            page.deleteLater()
            self.app.processEvents()

    def test_xhs_command_uses_public_default_and_worker_finishes(self):
        self.page.input.setText(XHS_URL)
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_grab()
        self._wait_workers()

        self.assertEqual(len(self.service.commands), 1)
        command = self.service.commands[0]
        self.assertEqual((command.mode, command.profile_id, command.limit),
                         ("profile_public", "default", 20))
        self.assertEqual([item.stable_key for item in self.page._items],
                         ["xiaohongshu:note_1"])
        self.assertIn("采集完成", self.page.status.text())

    def test_cover_preference_uses_current_marker_and_freezes_each_command(self):
        stale_cfg = SimpleNamespace(
            collect_cover=True,
            collect_cover_contract_version=0,
            feishu_target={},
        )
        stale_service = FakeApplicationService()
        stale_page = CollectionPage(
            stale_cfg, collection_service=stale_service
        )
        stale_page.input.setText(DOUYIN_URL)
        stale_page._on_grab()
        self._wait_page_workers(stale_page)
        self.assertFalse(stale_service.commands[0].include_cover)
        stale_page.shutdown_workers()
        stale_page.deleteLater()

        current_cfg = SimpleNamespace(
            collect_cover=True,
            collect_cover_contract_version=COLLECT_COVER_CONTRACT_VERSION,
            feishu_target={},
        )
        current_service = FakeApplicationService()
        current_page = CollectionPage(
            current_cfg, collection_service=current_service
        )
        current_page.input.setText(DOUYIN_URL)
        current_page._on_grab()
        current_cfg.collect_cover = False
        self._wait_page_workers(current_page)
        self.assertFalse(current_service.commands[0].include_cover)
        current_page.shutdown_workers()
        current_page.deleteLater()

    def test_cover_download_runs_after_metadata_collection_is_complete(self):
        class BlockingCoverWorkflow:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def run(self, result, *, snapshot, is_cancelled):
                self.entered.set()
                while not self.release.wait(0.002):
                    if is_cancelled():
                        break
                return SimpleNamespace(
                    collect_result=result,
                    outcomes=tuple(
                        SimpleNamespace(
                            stable_key=item.stable_key,
                            status="url_only",
                        )
                        for item in result.items
                    ),
                )

        with tempfile.TemporaryDirectory() as raw:
            collection_root = Path(raw) / "app-data" / "collection"
            records = CollectionRepository(collection_root / "records.json")
            history = CollectionHistoryRepository(
                collection_root / "history.json"
            )
            item = replace(
                douyin_item("post-cover"),
                cover_url="https://images.example.test/post-cover.jpg",
                cover_status="url_only",
            )
            service = FakeApplicationService(CollectResult.complete([item]))
            workflow = BlockingCoverWorkflow()
            cfg = SimpleNamespace(
                collect_cover=True,
                collect_cover_contract_version=COLLECT_COVER_CONTRACT_VERSION,
                keep_media=False,
                link_media_mode="video",
                feishu_target={},
                last_output_dir=str(Path(raw) / "output"),
                save=lambda: True,
            )
            page = CollectionPage(
                cfg,
                collection_service=service,
                history_repository=history,
                records_repository=records,
                cover_workflow=workflow,
            )
            page.input.setText(DOUYIN_URL)
            page._on_grab()
            self._wait_page_workers(page)
            self.assertTrue(workflow.entered.wait(1.0))
            self.app.processEvents()

            self.assertFalse(service.commands[0].include_cover)
            self.assertIn("采集完成", page.status.text())
            self.assertIsNotNone(page._material_worker)
            self.assertEqual(page.table.item(0, 11).text(), "下载中")

            workflow.release.set()
            self._wait_material_worker(page)
            page.shutdown_workers()
            page.deleteLater()

    def test_xhs_stop_button_is_cooperative_and_keeps_old_results(self):
        blocking = BlockingApplicationService()
        self.page._service = blocking
        old = xhs_item("old_before_cancel")
        self.page._set_items([old])
        self.page.input.setText(XHS_URL)
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_grab()
        self.assertTrue(blocking.started.wait(1.0))
        self.assertEqual(self.page.grab_btn.text(), "停止采集")

        self.page._on_grab()
        self._wait_workers()

        self.assertTrue(blocking.cancel_seen.is_set())
        self.assertEqual([item.stable_key for item in self.page._items],
                         ["xiaohongshu:old_before_cancel"])
        self.assertIn("原有结果未清除", self.page.status.text())

    def test_douyin_partial_and_efficiency_only_full_keep_their_commands(self):
        self.service.result = CollectResult.complete([douyin_item("201")])
        self.page.input.setText(DOUYIN_URL)
        self.assertFalse(self.page.method_segment.isHidden())

        self.page._on_grab()
        self._wait_workers()
        self.page.m_full_btn.setChecked(True)
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_full_grab()
        self._wait_workers()

        self.assertEqual(
            [command.mode for command in self.service.commands],
            ["anonymous_latest", "profile_all"],
        )
        self.assertEqual(self.service.commands[1].browser_mode, "efficiency")
        self.assertFalse(hasattr(self.page, "mode_eff_btn"))
        self.assertFalse(hasattr(self.page, "mode_low_btn"))

        self.page._on_collect_done(
            CollectResult.partial(
                [douyin_item("captcha_safe")],
                reason_code="captcha",
                next_action="legacy low-risk copy must not surface",
            ),
            self.page._run_token,
        )
        self.assertIn("抖音出现人工验证", self.page.status.text())
        self.assertIn("已保留 1 条结果", self.page.status.text())
        self.assertNotIn("小红书", self.page.status.text())
        self.assertNotIn("低风险", self.page.status.text())

    def test_partial_cancel_and_late_signal_never_clear_safe_results(self):
        old = douyin_item("old")
        safe = xhs_item("safe")
        self.page._set_items([old])
        token = self.page._run_token

        self.page._on_collect_done(
            CollectResult.partial(
                [safe], reason_code="rate_limited", next_action="稍后重试"
            ),
            token,
        )
        self.assertEqual([item.stable_key for item in self.page._items],
                         ["xiaohongshu:safe"])

        self.page._on_collect_done(CollectResult.cancelled(), token)
        self.assertEqual([item.stable_key for item in self.page._items],
                         ["xiaohongshu:safe"])

        self.page._on_collect_done(CollectResult.complete([douyin_item("late")]), token - 1)
        self.assertEqual([item.stable_key for item in self.page._items],
                         ["xiaohongshu:safe"])

        self.page._on_collect_done(
            CollectResult.complete([], confirmed_empty=True), token
        )
        self.assertEqual(self.page._items, [])

    def test_cancelled_main_keeps_completed_cover_and_reports_separately(self):
        completed = xhs_item("covered_before_cancel")
        completed.cover_url = "https://images.example.test/cover.jpg"
        completed.cover_status = "downloaded"
        completed.cover_local_path = "xiaohongshu/covered_before_cancel.jpg"
        execution = CollectionExecutionResult(
            CollectResult.cancelled([completed]),
            CollectionCoverSummary(
                enabled=True,
                eligible_count=1,
                downloaded_count=1,
                code="settled",
            ),
        )
        self.page._on_collect_done(execution, self.page._run_token)
        self.assertEqual(
            self.page._items[0].cover_status, "downloaded"
        )
        self.assertIn("已停止", self.page.status.text())
        self.assertIn("封面文件已保存，但任务更新失败", self.page.status.text())

    def test_every_fixed_failure_state_maps_to_safe_user_text_without_clearing(self):
        old = xhs_item("old_safe")
        self.page._set_items([old])
        token = self.page._run_token
        cases = {
            "login_required": "请先打开",
            "login_expired": "登录已过期",
            "captcha": "人工验证",
            "rate_limited": "访问频繁",
            "platform_changed": "页面结构已变化",
            "network_error": "页面读取失败",
            "no_items_unverified": "无法确认主页真实为空",
            "permission_denied": "平台拒绝",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                self.page._on_collect_done(CollectResult.failed(reason), token)
                self.assertIn(expected, self.page.status.text())
                self.assertEqual(
                    [item.stable_key for item in self.page._items],
                    ["xiaohongshu:old_safe"],
                )
                lowered = self.page.status.text().lower()
                for forbidden in ("token", "cookie", "dom", "traceback", "xsec_token"):
                    self.assertNotIn(forbidden, lowered)

    def test_sort_filter_search_select_all_stay_cross_platform(self):
        first = douyin_item("301")
        first.author_name = "Alpha"
        first.like = 2
        second = xhs_item("note_302", known={"like": 9})
        second.author_name = "Beta"
        self.page._set_items([first, second])

        self.page.filter_edit.setText("beta")
        self.assertEqual([item.stable_key for item in self.page._view],
                         ["xiaohongshu:note_302"])
        self.page.filter_edit.clear()
        self.page.metric_combo.setCurrentIndex(1)  # 点赞
        self.page.metric_value.setText("5")
        self.assertEqual([item.stable_key for item in self.page._view],
                         ["xiaohongshu:note_302"])
        self.page.metric_value.clear()
        self.page.metric_combo.setCurrentIndex(0)
        self.page._sort_col = 5
        self.page._sort_desc = True
        self.page._render()
        self.assertEqual(self.page._view[0].stable_key, "xiaohongshu:note_302")
        self.page._toggle_select_all()
        self.assertEqual(len(self.page._checked_rows()), 2)

    def test_compact_toolbar_copy_and_empty_filter_keep_layout_stable(self):
        self.page.show()
        self.app.processEvents()
        self.assertEqual(
            self.page.collection_hint.text(),
            "默认快速全量采集；只有出现真实可见验证时，才会停止并保留已采结果。",
        )
        self.assertNotIn("低风险", self.page.collection_hint.text())
        ordered = [
            self.page.select_all_btn,
            self.page.material_download_btn,
            self.page.transcribe_btn,
            self.page.export_btn,
            self.page.bitable_btn,
            self.page.history_manage_button,
        ]
        self.assertEqual(self.page.export_btn.text().strip(), "导出表格")
        self.page.resize(1000, 760)
        self.app.processEvents()
        self.assertEqual(self.page.export_btn.text().strip(), "导出")
        self.page._items = [xhs_item("compact_count_1"), xhs_item("compact_count_2")]
        self.page._view = list(self.page._items)
        self.page.table.setRowCount(10)
        self.page._selected_keys = {
            self.page._items[0].stable_key,
            self.page._items[1].stable_key,
        }
        self.page._update_count()
        self.assertEqual(self.page.count_label.text(), "2/10 条")
        self.assertIn("已选 2 条", self.page.count_label.toolTip())
        for button in ordered:
            self.assertGreaterEqual(
                button.width(),
                button.sizeHint().width(),
                button.text(),
            )
        self.assertEqual(
            [button.geometry().center().y() for button in ordered],
            [ordered[0].geometry().center().y()] * len(ordered),
        )
        self.assertEqual(
            sorted(button.geometry().left() for button in ordered),
            [button.geometry().left() for button in ordered],
        )
        self.assertGreater(
            self.page.data_location_select.geometry().left(),
            self.page.history_manage_button.geometry().right(),
        )
        self.assertEqual(self.page.data_location_select.trigger.text(), "数据位置")
        self.assertEqual(
            self.page.data_location_select.size().toTuple(),
            (96, 38),
        )
        self.assertEqual(
            [row.open_button.text() for row in self.page.data_location_select.popup.rows],
            ["采集数据", "封面", "文稿", "音视频"],
        )
        shell_height = self.page.results_shell.height()
        activity_visible = self.page.activity.isVisible()
        self.page.metric_combo.setCurrentIndex(
            self.page.metric_combo.findText("点赞")
        )
        self.page.metric_value.setText("5")
        valid_filter = self.page._active_field_filter
        self.page.metric_value.setText("-1")
        self.assertFalse(hasattr(self.page, "filter_error"))
        self.assertEqual(self.page.metric_value.property("state"), "error")
        self.assertEqual(
            self.page.metric_value.toolTip(),
            "请输入有效的非负数字",
        )
        self.assertEqual(self.page._active_field_filter, valid_filter)
        self.page.metric_value.clear()
        self.app.processEvents()
        self.assertEqual(self.page.metric_value.property("state"), "default")
        self.assertEqual(self.page.metric_value.accessibleDescription(), "")
        self.assertIsNone(self.page._active_field_filter)
        self.assertEqual(self.page.results_shell.height(), shell_height)
        self.assertEqual(self.page.activity.isVisible(), activity_visible)

    def test_data_location_action_popup_is_stable_in_paper_and_two_sizes(self):
        for theme in ("warm",):
            for size in ((1200, 760), (1480, 900)):
                with self.subTest(theme=theme, size=size):
                    T.apply_theme(theme)
                    page = CollectionPage(
                        self.cfg,
                        collection_service=FakeApplicationService(),
                    )
                    try:
                        page.resize(*size)
                        page.show()
                        self.app.processEvents()
                        action = page.data_location_select
                        self.assertEqual(action.size().toTuple(), (96, 38))
                        self.assertEqual(action.popup.size().toTuple(), (172, 162))
                        self.assertEqual(
                            [row.open_button.text() for row in action.popup.rows],
                            ["采集数据", "封面", "文稿", "音视频"],
                        )
                        self.assertEqual(
                            [row.height() for row in action.popup.rows],
                            [38, 38, 38, 38],
                        )
                        self.assertTrue(
                            all(
                                row.open_button.sizeHint().width()
                                <= row.open_button.width()
                                for row in action.popup.rows
                            )
                        )
                        action.show_popup()
                        self.app.processEvents()
                        self.assertTrue(action.popup.isVisible())
                        trigger_bottom = action.trigger.mapToGlobal(
                            action.trigger.rect().bottomLeft()
                        ).y()
                        self.assertGreaterEqual(action.popup.y() - trigger_bottom, 6)
                        self.assertLessEqual(action.popup.y() - trigger_bottom, 8)
                        self.assertLessEqual(
                            action.popup.geometry().right(),
                            self.app.primaryScreen().availableGeometry().right(),
                        )
                        action.popup.close()
                    finally:
                        page.shutdown_workers()
                        page.deleteLater()
                        self.app.processEvents()
        T.apply_theme("warm")

    def test_status_and_progress_are_overlay_feedback_without_page_reflow(self):
        self.page.resize(1200, 760)
        self.page.show()
        self.app.processEvents()
        shell_geometry = self.page.results_shell.geometry()
        table_geometry = self.page.table.geometry()
        self.assertIs(self.page.activity.parentWidget(), self.page)
        self.assertEqual(self.page.layout().indexOf(self.page.activity), -1)

        self.page.status.setText("未检测到登录（超时或已取消），可重试。")
        self.app.processEvents()
        self.assertTrue(self.page.activity.isVisible())
        self.assertTrue(self.page.status.isVisible())
        self.assertEqual(self.page.results_shell.geometry(), shell_geometry)
        self.assertEqual(self.page.table.geometry(), table_geometry)
        overlay_global = self.page.activity.mapToGlobal(QPoint(0, 0))
        shell_global = self.page.results_shell.mapToGlobal(QPoint(0, 0))
        overlay_top_left = overlay_global - shell_global
        self.assertGreaterEqual(overlay_top_left.x(), 0)
        self.assertGreaterEqual(overlay_top_left.y(), 0)
        self.assertLessEqual(
            overlay_top_left.x() + self.page.activity.width(),
            self.page.results_shell.width(),
        )
        self.assertLessEqual(
            overlay_top_left.y() + self.page.activity.height(),
            self.page.results_shell.height(),
        )

        self.page._set_busy(True)
        self.app.processEvents()
        self.assertFalse(self.page.progress_bar.isVisible())
        self.assertTrue(self.page.status.isVisible())
        self.assertEqual(self.page.results_shell.geometry(), shell_geometry)
        self.assertEqual(self.page.table.geometry(), table_geometry)

        self.page._set_busy(False)
        self.page.status._expire()
        self.app.processEvents()
        self.assertFalse(self.page.activity.isVisible())
        self.assertEqual(self.page.status.text(), "")
        self.assertEqual(self.page.results_shell.geometry(), shell_geometry)
        self.assertEqual(self.page.table.geometry(), table_geometry)

    def test_last_three_status_columns_never_infer_media_success(self):
        video = douyin_item("media-evidence")
        saved = replace(
            douyin_item("saved-media"),
            media_local_path="/tmp/saved-media.mp4",
            media_status="downloaded",
        )
        image = xhs_item("image-evidence", content_kind="image")
        self.page._collection_task_states = lambda: {}
        self.page._managed_media_file = (
            lambda item: Path(item.media_local_path)
            if item.work_id == "saved-media"
            else None
        )
        self.page._set_items([video, saved, image])
        rows = {item.work_id: row for row, item in enumerate(self.page._view)}
        self.assertEqual(
            [
                self.page.table.horizontalHeaderItem(index).text()
                for index in (9, 10, 11)
            ],
            ["转录", "音视频", "封面"],
        )
        self.assertEqual(
            self.page.table.item(rows["media-evidence"], 10).text(),
            "未下载",
        )
        self.assertEqual(self.page.table.item(rows["saved-media"], 10).text(), "✓")
        self.assertIsNone(self.page.table.cellWidget(rows["saved-media"], 10))
        self.assertIn(
            "双击",
            self.page.table.item(rows["saved-media"], 10).toolTip(),
        )
        self.assertEqual(self.page.table.item(rows["image-evidence"], 10).text(), "图文")
        self.assertFalse(
            any(
                button.text() == "在访达显示"
                for button in self.page.table.findChildren(QPushButton)
            )
        )
        self.page._collection_task_states = lambda: {
            video.stable_key: CollectionItemRuntimeSnapshot(
                transcript="transcribing",
                media="downloading",
            )
        }
        self.page._render()
        rows = {item.work_id: row for row, item in enumerate(self.page._view)}
        self.assertEqual(self.page.table.item(rows["media-evidence"], 9).text(), "转录中")
        self.assertEqual(self.page.table.item(rows["media-evidence"], 10).text(), "下载中")
        self.page._collection_task_states = lambda: {
            video.stable_key: CollectionItemRuntimeSnapshot(
                transcript="failed",
                media="failed",
            )
        }
        self.page._transcribed.add(video.stable_key)
        self.page._render()
        rows = {item.work_id: row for row, item in enumerate(self.page._view)}
        self.assertEqual(self.page.table.item(rows["media-evidence"], 9).text(), "✓")
        self.assertEqual(self.page.table.item(rows["media-evidence"], 10).text(), "已失败")

    def test_material_columns_distinguish_idle_queue_running_and_failure(self):
        item = replace(
            douyin_item("material-runtime"),
            cover_url="https://images.example.test/material-runtime.jpg",
            cover_status="url_only",
        )
        self.page._cover_workflow = object()
        self.page._set_items([item])

        self.assertEqual(self.page.table.item(0, 9).text(), "未转录")
        self.assertEqual(self.page.table.item(0, 10).text(), "未下载")
        self.assertEqual(self.page.table.item(0, 11).text(), "未下载")

        self.page._queue_material_runtime(
            [item], MaterialPreferenceSnapshot("video", True, True)
        )
        self.assertEqual(self.page.table.item(0, 10).text(), "排队中")
        self.assertEqual(self.page.table.item(0, 11).text(), "排队中")

        self.page._on_material_progress(
            (item.stable_key, "media", "downloading")
        )
        self.assertEqual(self.page.table.item(0, 10).text(), "下载中")
        self.assertEqual(self.page.table.item(0, 11).text(), "排队中")

        media_result = SimpleNamespace(
            outcomes=(
                SimpleNamespace(
                    stable_key=item.stable_key,
                    status="failed",
                ),
            )
        )
        cover_result = SimpleNamespace(
            outcomes=(
                SimpleNamespace(
                    stable_key=item.stable_key,
                    status="downloaded",
                ),
            )
        )
        self.page._settle_material_runtime(media_result, cover_result)
        self.page._render()
        self.assertEqual(self.page.table.item(0, 10).text(), "已失败")
        self.assertEqual(self.page.table.item(0, 11).text(), "未下载")

    def test_unknown_known_zero_and_image_render_without_fake_zero(self):
        unknown = xhs_item("unknown")
        unknown.title = "未知字段"
        known_zero = xhs_item(
            "known_zero",
            known={
                "create_time": 0,
                "duration_ms": 0,
                "like": 0,
                "collect": 0,
                "comment": 0,
                "share": 0,
            },
        )
        known_zero.title = "真实零"
        image = xhs_item("image", content_kind="image")
        image.title = "图文"
        self.page._set_items([unknown, known_zero, image])

        rows = {
            self.page.table.item(row, 2).text(): row
            for row in range(self.page.table.rowCount())
        }
        unknown_row = rows["未知字段"]
        self.assertEqual(
            [self.page.table.item(unknown_row, col).text() for col in range(3, 9)],
            ["—", "—", "—", "—", "—", "—"],
        )
        self.assertEqual(
            self.page.table.item(unknown_row, 5).toolTip(), "未获取点赞数"
        )
        zero_row = rows["真实零"]
        self.assertEqual(
            [self.page.table.item(zero_row, col).text() for col in range(3, 9)],
            ["0", "0", "0", "0", "0", "0"],
        )
        self.assertEqual(self.page.table.item(rows["图文"], 4).text(), "图文")
        self.assertEqual(self.page.table.item(rows["图文"], 10).text(), "图文")
        self.assertIn("未获取值不参与筛选", self.page.metric_value.toolTip())

    def test_unknown_sort_last_stable_and_filter_excludes_unknown(self):
        unknown_first = xhs_item("unknown_first")
        known_nine = xhs_item("known_nine", known={"like": 9})
        unknown_second = xhs_item("unknown_second")
        known_zero = xhs_item("known_zero", known={"like": 0})
        self.page._set_items([unknown_first, known_nine, unknown_second, known_zero])
        self.page._sort_col = 5

        self.page._sort_desc = True
        self.page._render()
        self.assertEqual(
            [item.work_id for item in self.page._view],
            ["known_nine", "known_zero", "unknown_first", "unknown_second"],
        )
        self.page._sort_desc = False
        self.page._render()
        self.assertEqual(
            [item.work_id for item in self.page._view],
            ["known_zero", "known_nine", "unknown_first", "unknown_second"],
        )

        self.page.metric_combo.setCurrentIndex(1)
        self.page.metric_value.setText("0")
        self.assertEqual(
            [item.work_id for item in self.page._view], ["known_zero", "known_nine"]
        )
        self.page.metric_value.setText("1")
        self.assertEqual([item.work_id for item in self.page._view], ["known_nine"])

    def test_complete_and_partial_keep_flow_state_and_report_missing_fields(self):
        item = xhs_item("honest")
        token = self.page._run_token

        self.page._on_collect_done(CollectResult.complete([item]), token)
        self.assertIn("采集完成", self.page.status.text())
        self.assertIn("部分字段未获取", self.page.status.text())

        self.page._on_collect_done(
            CollectResult.partial(
                [item], reason_code="rate_limited", next_action="稍后重试"
            ),
            token,
        )
        self.assertIn("访问频繁", self.page.status.text())
        self.assertIn("已保留 1 条结果", self.page.status.text())
        self.assertIn("部分字段未获取", self.page.status.text())

    def test_cross_platform_transcription_uses_stable_identity_without_work_delete(self):
        captured = []
        self.page._on_transcribe = lambda sources, folder: captured.append((sources, folder))
        items = [douyin_item("101"), xhs_item("note_video"), xhs_item("note_image", content_kind="image")]
        self.page._set_items(items)
        for row in range(self.page.table.rowCount()):
            self.page.table.item(row, 0).setCheckState(Qt.Checked)

        self.page._on_transcribe_selected()
        sources = captured[0][0]
        self.assertIn("https://www.iesdouyin.com/share/video/101/", sources)
        self.assertIn("https://www.xiaohongshu.com/explore/note_video", sources)
        self.assertNotIn("https://www.xiaohongshu.com/explore/note_image", sources)

        self.assertFalse(hasattr(self.page, "delete_btn"))
        self.assertFalse(hasattr(self.page, "_on_delete_selected"))
        self.assertEqual(
            {item.stable_key for item in self.page._items},
            {"douyin:101", "xiaohongshu:note_video", "xiaohongshu:note_image"},
        )

    def test_local_export_falls_back_to_sanitized_work_url(self):
        item = xhs_item("note_export")
        text = export.to_csv_string([item])
        self.assertIn(item.work_url, text)
        self.assertIn("封面状态", text)
        self.assertIn("封面本地文件", text)
        self.assertIn("未下载封面", text)
        self.assertNotIn("?", item.work_url)
        self.assertNotIn("#", item.work_url)

        old = douyin_item("legacy")
        old_text = export.to_csv_string([old], {"legacy": "旧抖音转录"})
        self.assertIn(old.play_url, old_text)
        self.assertIn("旧抖音转录", old_text)

    def test_cover_user_actions_fail_closed_without_selection_or_workflow(self):
        self.page._set_items([
            replace(
                xhs_item("cover_pending"),
                cover_url="https://assets.example.test/cover.jpg",
                cover_status="url_only",
            )
        ])
        self.assertFalse(self.page.cover_retry_btn.isEnabled())
        self.assertFalse(self.page.cover_finder_btn.isEnabled())
        self.page.table.item(0, 0).setCheckState(Qt.Checked)
        self.assertFalse(self.page.cover_retry_btn.isEnabled())
        self.page._retry_selected_covers()
        self.assertIn("尚未准备好", self.page.status.text())

    def test_cover_column_is_permanent_and_each_row_has_truthful_state_action(self):
        self.assertEqual(
            [
                self.page.table.horizontalHeaderItem(index).text()
                for index in (9, 10, 11)
            ],
            ["转录", "音视频", "封面"],
        )
        self.assertTrue(self.page.cover_retry_btn.isHidden())
        self.assertTrue(self.page.cover_finder_btn.isHidden())

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(b"not-an-image-but-managed")
            saved_path = Path(handle.name)
        rows = [
            replace(xhs_item("none"), cover_url="", cover_status="not_requested"),
            replace(
                xhs_item("not_requested"),
                cover_url="https://assets.example.test/not-requested.jpg",
                cover_status="not_requested",
            ),
            replace(
                xhs_item("url_only"),
                cover_url="https://assets.example.test/url-only.jpg",
                cover_status="url_only",
            ),
            replace(
                xhs_item("pending"),
                cover_url="https://assets.example.test/pending.jpg",
                cover_status="pending",
            ),
            replace(
                xhs_item("failed"),
                cover_url="https://assets.example.test/failed.jpg",
                cover_status="failed",
            ),
            replace(
                xhs_item("saved"),
                cover_url="https://assets.example.test/saved.jpg",
                cover_status="downloaded",
                cover_local_path="saved.jpg",
            ),
            replace(
                xhs_item("missing"),
                cover_url="https://assets.example.test/missing.jpg",
                cover_status="downloaded",
                cover_local_path="missing.jpg",
            ),
            replace(
                xhs_item("uncertain"),
                cover_url="https://assets.example.test/uncertain.jpg",
                cover_status="failed",
                last_error_code="cover:cover_commit_uncertain",
            ),
            replace(
                xhs_item("recovered"),
                cover_url="https://assets.example.test/recovered.jpg",
                cover_status="downloaded",
                cover_local_path="saved.jpg",
                last_error_code="cover:recovered_previous",
            ),
        ]
        resolver = lambda relative: (
            saved_path if relative == "saved.jpg" else None
        )
        with patch(
            "src.ui.collection_page.export.resolve_managed_cover_file",
            side_effect=resolver,
        ):
            self.page._set_items(rows)

        visible = {}
        for row_index, model in enumerate(self.page._view):
            visible[model.work_id] = self.page.table.item(row_index, 11).text()
        self.assertEqual(visible["none"], "未下载")
        self.assertEqual(visible["not_requested"], "未下载")
        self.assertEqual(visible["url_only"], "未下载")
        self.assertEqual(visible["pending"], "下载中")
        self.assertEqual(visible["failed"], "已失败")
        self.assertEqual(visible["saved"], "✓")
        self.assertEqual(visible["missing"], "需补下")
        self.assertEqual(visible["uncertain"], "已失败")
        self.assertEqual(visible["recovered"], "✓")
        saved_path.unlink(missing_ok=True)

    def test_uncertain_cover_action_uses_recovery_copy_and_normal_retry_stays_unchanged(self):
        self.page._cover_workflow = object()
        uncertain = replace(
            xhs_item("cover_uncertain"),
            cover_url="https://assets.example.test/uncertain.jpg",
            cover_status="failed",
            last_error_code="cover:cover_commit_uncertain",
        )
        self.page._set_items([uncertain])
        self.page.table.item(0, 0).setCheckState(Qt.Checked)
        self.assertTrue(self.page.cover_retry_btn.isEnabled())
        self.assertEqual(self.page.cover_retry_btn.text(), "重试恢复")

        self.page._cover_retry_is_recovery = True
        self.page._cover_retry_worker = SimpleNamespace(isRunning=lambda: True)
        self.page._update_cover_actions()
        self.assertEqual(self.page.cover_retry_btn.text(), "正在恢复…")

        self.page._cover_retry_worker = None
        self.page._cover_retry_is_recovery = False
        normal = replace(
            xhs_item("cover_normal_retry"),
            cover_url="https://assets.example.test/retry.jpg",
            cover_status="failed",
            last_error_code="cover_timeout",
        )
        self.page._set_items([normal])
        self.page.table.item(0, 0).setCheckState(Qt.Checked)
        self.assertEqual(self.page.cover_retry_btn.text(), "重试封面")

        self.page._cover_retry_worker = SimpleNamespace(isRunning=lambda: True)
        self.page._update_cover_actions()
        self.assertEqual(self.page.cover_retry_btn.text(), "正在重试…")
        self.page._cover_retry_worker = None

    def test_xhs_and_douyin_feishu_export_are_available_and_cancel_is_zero_effect(self):
        self.page._set_items([xhs_item()])
        self.assertFalse(self.page.bitable_btn.isEnabled())
        self.page.table.item(0, 0).setCheckState(Qt.Checked)
        self.assertTrue(self.page.bitable_btn.isEnabled())
        with patch.object(self.page, '_transcript_text_map', return_value={}), patch('src.ui.feishu_dialog.send_records_to_feishu', return_value=None) as send, patch('src.core.feishu._api') as api:
            self.page._on_export_bitable()
            self._wait_feishu_preparation()
            self.assertEqual(len(send.call_args.args[2]),1)
            self.assertEqual(send.call_args.kwargs['context'],'homepage')
            api.assert_not_called()
        self.page._set_items([douyin_item()])
        self.assertFalse(self.page.bitable_btn.isEnabled())


    def test_bitable_without_explicit_selection_never_opens_creation_flow(self):
        self.page._set_items([xhs_item()])
        with patch('src.ui.feishu_dialog.send_records_to_feishu') as send, patch.object(self.page,'_transcript_text_map') as read:
            self.page._on_export_bitable()
            send.assert_not_called();read.assert_not_called()
        self.assertEqual(self.page.status.text(),'请先在表格中勾选要存入飞书的作品。')


    def test_bitable_explicit_selection_defaults_to_append_and_needs_no_new_table_target(self):
        first,middle,last=xhs_item('first'),xhs_item('middle'),xhs_item('last')
        self.page._set_items([first,middle,last]);self.page._selected_keys={first.stable_key,last.stable_key}
        self.page._sync_table_checks()
        with patch.object(self.page,'_transcript_text_map',return_value={}), patch('src.ui.feishu_dialog.send_records_to_feishu',return_value=None) as send:
            self.page._on_export_bitable();self._wait_feishu_preparation()
        self.assertEqual([r.work_id for r in send.call_args.args[2]],[first.work_id,last.work_id])


    def test_existing_bitable_mode_uses_saved_link_and_never_promises_schema_changes(self):
        item=xhs_item();self.page._set_items([item]);self.page._selected_keys={item.stable_key}
        self.cfg.feishu_table_targets=[{'name':'长期素材库','url':'https://my.feishu.cn/base/APP?table=TBL'}]
        with patch.object(self.page,'_transcript_text_map',return_value={}), patch('src.ui.feishu_dialog.send_records_to_feishu',return_value=None) as send, patch('src.ui.collection_page.dialogs.prompt') as prompt:
            self.page._on_export_bitable();self._wait_feishu_preparation()
        self.assertIs(send.call_args.args[1],self.cfg);prompt.assert_not_called()
        self.assertEqual(len(self.cfg.feishu_table_targets),1)


    def test_existing_bitable_success_remembers_target_and_reports_append(self):
        existing_url = "https://example.feishu.cn/base/APP1?table=TBL1"
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期素材库",
            existing_url,
        )
        receipt = feishu.BitableExportReceipt(
            url=existing_url,
            row_total=2,
            row_success=2,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
            destination_app_token="APP1",
            destination_table_id="TBL1",
            destination_table_name="长期素材库",
        )
        worker = SimpleNamespace(
            existing_url=existing_url,
            resolved_target=target,
            selection={
                "mode": "existing",
                "name": "长期素材库",
                "url": existing_url,
                "context": "selection",
                "register": True,
            },
            isRunning=lambda: False,
        )
        self.page._bitable_worker = worker
        prompts = []
        with patch(
            "src.ui.collection_page.dialogs.confirm_open",
            side_effect=lambda _parent, title, text: prompts.append((title, text)) or False,
        ):
            self.page._on_bitable_done(worker, True, receipt)

        self.assertEqual(len(self.cfg.feishu_table_targets), 1)
        self.assertEqual(self.cfg.feishu_table_targets[0]["url"], existing_url)
        self.assertEqual(self.cfg.feishu_table_targets[0]["name"], "长期素材库")
        self.assertEqual(
            self.cfg.feishu_storage_preferences["selection_target_id"],
            self.cfg.feishu_table_targets[0]["target_id"],
        )
        self.assertEqual(prompts[0][0], "已追加到飞书多维表格")
        self.assertIn("位置：指定的已有多维表格", prompts[0][1])
        self.assertIn("表格：长期素材库", prompts[0][1])
        self.assertIn("已追加到飞书多维表格：2/2 条", self.page.status.text())
        self.page._bitable_worker = None

    def test_existing_bitable_all_duplicates_reports_zero_new_write(self):
        existing_url = "https://example.feishu.cn/base/APP1?table=TBL1"
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期素材库",
            existing_url,
        )
        receipt = feishu.BitableExportReceipt(
            url=existing_url,
            row_total=0,
            row_success=0,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
            row_duplicate_skipped=2,
        )
        worker = SimpleNamespace(
            existing_url=existing_url,
            resolved_target=target,
            selection={"register": False},
            isRunning=lambda: False,
        )
        self.page._bitable_worker = worker

        with patch(
            "src.ui.collection_page.dialogs.confirm_open",
            return_value=False,
        ) as result_dialog:
            self.page._on_bitable_done(worker, True, receipt)

        self.assertEqual(
            result_dialog.call_args.args[1],
            "作品已在飞书多维表格中",
        )
        self.assertIn("没有重复写入", self.page.status.text())
        self.assertIn("已跳过 2 条", self.page.status.text())
        self.assertIn("跳过 2 条", result_dialog.call_args.args[2])
        self.page._bitable_worker = None

    def test_existing_bitable_all_duplicates_skips_attachment_preflight_and_write(self):
        item = xhs_item("already_saved")
        record = WorkArchiveRecord(
            title=item.title,
            platform=item.platform,
            work_id=item.work_id,
            work_url=item.work_url,
        )
        target = feishu.ExistingBitableTarget(
            "APP1",
            "TBL1",
            "长期素材库",
            "https://example.feishu.cn/base/APP1?table=TBL1",
        )
        worker = collection_page._BitableWorker(
            "",
            "",
            "不会新建",
            "作品库",
            [],
            export.BitableExportPreflight(
                records=({"fields": {"标题": item.title}},),
                cover_candidates=(),
                cover_skipped=(),
            ),
            existing_url=target.url,
            archive_records=(record,),
            selection={"schema_version": 1},
            source_items=(item,),
        )
        outcomes = []
        worker.done.connect(lambda ok, payload: outcomes.append((ok, payload)))

        with (
            patch.object(
                feishu,
                "resolve_existing_bitable",
                return_value=target,
            ),
            patch.object(
                feishu,
                "find_existing_bitable_text_values",
                return_value={record.work_url},
            ) as find_existing,
            patch.object(export, "prepare_bitable_export") as prepare,
            patch.object(feishu, "export_collection_to_new_bitable") as write,
        ):
            worker.run()

        self.assertTrue(outcomes[0][0])
        self.assertEqual(outcomes[0][1].row_duplicate_skipped, 1)
        self.assertEqual(
            find_existing.call_args.args[2],
            "原作品链接",
        )
        prepare.assert_not_called()
        write.assert_not_called()

    def test_existing_bitable_validation_failure_keeps_zero_success_state(self):
        worker = SimpleNamespace(
            existing_url="https://example.feishu.cn/base/APP1?table=TBL1",
            resolved_target=None,
            isRunning=lambda: False,
        )
        self.page._bitable_worker = worker
        alerts = []
        message = "目标表与 Lulu 当前字段不兼容。Lulu 没有写入。"
        with patch(
            "src.ui.collection_page.dialogs.alert",
            side_effect=lambda _parent, title, text, **_kwargs: alerts.append((title, text)),
        ):
            self.page._on_bitable_done(worker, False, message)

        self.assertEqual(alerts[0], ("存入飞书多维表格失败", message))
        self.assertEqual(self.page.status.text(), "飞书导出未完成。")
        self.assertNotIn("已存入", self.page.status.text())
        self.assertNotIn("已追加", self.page.status.text())
        self.assertNotIn("bitable_mode", self.cfg.feishu_target)
        self.page._bitable_worker = None

    def test_bitable_worker_rejects_duplicate_and_keeps_reference_until_finished(self):
        from unittest.mock import Mock
        active=SimpleNamespace(isRunning=lambda:True,dialog=SimpleNamespace(raise_=Mock()))
        self.page._feishu_session=active
        with patch('src.ui.feishu_dialog.send_records_to_feishu') as send:
            self.page._on_export_bitable();send.assert_not_called()
        active.dialog.raise_.assert_called_once();self.page._feishu_session=None


    def test_shutdown_guards_bitable_worker_until_finished(self):
        from src.ui.feishu_save_dialog import FeishuSaveSession
        from src.core import feishu_save as saving
        from tests.test_feishu_unified_ui import settle
        with patch.object(saving,'account_info',return_value={'account_id':'fake','name':'测试'}):
            session=FeishuSaveSession(self.page,self.cfg,(WorkArchiveRecord(title='作品'),),'作品','homepage')
            session.start();settle(session.dialog)
        self.page._feishu_session=session
        entered=threading.Event();release=threading.Event()
        def call():entered.set();release.wait(1);return None
        session.dialog._run(call,lambda _:None)
        self.assertTrue(entered.wait(1));worker=session.dialog._worker
        self.page.shutdown_workers(timeout_ms=1)
        self.assertTrue(worker.isRunning());self.assertIn(worker,collection_page._PENDING_BITABLE_WORKERS)
        release.set();settle(session.dialog);self.app.processEvents()
        self.assertNotIn(worker,collection_page._PENDING_BITABLE_WORKERS)


    def test_bitable_uncertain_outcome_never_suggests_retry_and_keeps_link(self):
        def receipt(url: str):
            return SimpleNamespace(
                url=url,
                row_total=1,
                row_success=0,
                row_failed=0,
                row_unconfirmed=1,
                row_cancelled=0,
                cover_candidate=0,
                cover_success=0,
                cover_skipped=0,
                cover_failed=0,
                cover_uncertain=0,
                cancelled=False,
                complete=False,
                outcome_uncertain=True,
                uncertain_stage="table_create" if url else "wiki_node_create",
                safe_details=lambda: "",
            )

        worker = SimpleNamespace(isRunning=lambda: False)
        prompts = []
        self.page._bitable_worker = worker
        with patch(
            "src.ui.collection_page.dialogs.confirm_open",
            side_effect=lambda _parent, title, text: prompts.append(
                (title, text)
            ) or False,
        ):
            self.page._on_bitable_done(
                worker,
                True,
                receipt("https://feishu.cn/wiki/NODE1"),
                destination_name="产品知识库",
                result_title="主页采集 · 测试作者 · 2026-08-02",
            )
        known_text = self.page.status.text()
        self.assertIn("可能已经创建", known_text)
        self.assertIn("不要立即重试", known_text)
        self.assertNotIn("检查网络后重试", known_text)
        self.assertEqual(prompts[0][0], "飞书结果待确认")
        self.assertIn("位置：知识库「产品知识库」", prompts[0][1])
        self.assertIn("表格：主页采集 · 测试作者", prompts[0][1])

        self.page._bitable_worker = worker
        alerts = []
        with patch(
            "src.ui.collection_page.dialogs.alert",
            side_effect=lambda _parent, title, text, **_kwargs: alerts.append(
                (title, text)
            ),
        ):
            self.page._on_bitable_done(worker, True, receipt(""))
        unknown_text = self.page.status.text()
        self.assertIn("可能已经创建", unknown_text)
        self.assertIn("按本次标题和时间确认", unknown_text)
        self.assertIn("目标知识库", unknown_text)
        self.assertNotIn("检查网络后重试", unknown_text)
        self.assertEqual(alerts[0][0], "飞书结果待确认")
        self.page._bitable_worker = None

    def test_bitable_complete_result_modal_opens_only_after_user_choice(self):
        receipt = feishu.BitableExportReceipt(
            url="https://feishu.cn/wiki/NODE1",
            row_total=3,
            row_success=3,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=3,
            cover_success=3,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            media_candidate=3,
            media_success=3,
            media_skipped=0,
            media_failed=0,
            media_uncertain=0,
            cancelled=False,
        )
        worker = SimpleNamespace(isRunning=lambda: False)
        self.page._bitable_worker = worker
        prompts = []
        with (
            patch(
                "src.ui.collection_page.dialogs.confirm_open",
                side_effect=lambda _parent, title, text: prompts.append(
                    (title, text)
                ) or False,
            ),
            patch(
                "src.ui.collection_page.QDesktopServices.openUrl"
            ) as open_url,
        ):
            self.page._on_bitable_done(
                worker,
                True,
                receipt,
                destination_name="内容中心",
                result_title="主页采集 · 三条作品",
            )
        open_url.assert_not_called()
        self.assertEqual(prompts[0][0], "已存入飞书多维表格")
        self.assertIn("位置：知识库「内容中心」", prompts[0][1])
        self.assertIn("数据行：3/3", prompts[0][1])
        self.assertIn("封面文件上传：3/3", prompts[0][1])
        self.assertIn("音视频附件：3/3", prompts[0][1])

        self.page._bitable_worker = worker
        with (
            patch(
                "src.ui.collection_page.dialogs.confirm_open",
                return_value=True,
            ),
            patch(
                "src.ui.collection_page.QDesktopServices.openUrl",
                return_value=True,
            ) as open_url,
        ):
            self.page._on_bitable_done(worker, True, receipt)
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), receipt.url)
        self.page._bitable_worker = None

    def test_drive_result_names_cloud_drive_instead_of_knowledge_base(self):
        receipt = feishu.BitableExportReceipt(
            url="https://example.feishu.cn/base/APP1",
            row_total=1,
            row_success=1,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=0,
            cover_candidate=0,
            cover_success=0,
            cover_skipped=0,
            cover_failed=0,
            cover_uncertain=0,
            cancelled=False,
        )
        worker = SimpleNamespace(
            isRunning=lambda: False,
            selection={"destination_kind": "drive", "register": False},
            existing_url="",
            resolved_target=None,
        )
        self.page._bitable_worker = worker
        prompts = []
        with patch(
            "src.ui.collection_page.dialogs.confirm_open",
            side_effect=lambda _parent, title, text: prompts.append(
                (title, text)
            ) or False,
        ):
            self.page._on_bitable_done(
                worker,
                True,
                receipt,
                destination_name="云盘（我的文稿库）",
                result_title="本次新建表格",
            )

        self.assertIn("位置：云盘（我的文稿库）", prompts[0][1])
        self.assertNotIn("位置：知识库", prompts[0][1])
        self.page._bitable_worker = None

    def test_final_cover_items_write_master_then_history_with_separate_status(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            records = CollectionRepository(root / "records.json")
            history = CollectionHistoryRepository(root / "history.json")
            persistence_order = []
            real_upsert = records.upsert_collected
            real_add_record = history.add_record

            def traced_upsert(*args, **kwargs):
                persistence_order.append("records")
                return real_upsert(*args, **kwargs)

            def traced_add_record(*args, **kwargs):
                persistence_order.append("history")
                return real_add_record(*args, **kwargs)

            service = FakeApplicationService()
            covered = xhs_item("persisted_cover")
            covered.cover_url = "https://images.example.test/cover.jpg"
            covered.cover_status = "downloaded"
            covered.cover_local_path = "xiaohongshu/persisted_cover.jpg"
            service.result = CollectResult.complete([covered])
            service.cover_summary = CollectionCoverSummary(
                enabled=True,
                eligible_count=1,
                downloaded_count=1,
                code="settled",
            )
            page = CollectionPage(
                self.cfg,
                collection_service=service,
                history_repository=history,
                records_repository=records,
            )
            page.input.setText(XHS_URL)
            with (
                patch(
                    "src.ui.collection_page.dialogs.confirm",
                    return_value=True,
                ),
                patch.object(
                    records, "upsert_collected", side_effect=traced_upsert
                ),
                patch.object(
                    history, "add_record", side_effect=traced_add_record
                ),
            ):
                page._on_grab()
                self._wait_page_workers(page)

            self.assertEqual(persistence_order, ["records", "history"])
            self.assertEqual(
                records.load().items[0].cover_local_path,
                "xiaohongshu/persisted_cover.jpg",
            )
            saved_history = history.load().active_records
            self.assertEqual(len(saved_history), 1)
            self.assertEqual(
                saved_history[0].items[0].cover_local_path,
                "xiaohongshu/persisted_cover.jpg",
            )
            self.assertIn("采集完成", page.status.text())
            self.assertIn("封面已保存 1/1，失败 0", page.status.text())
            page.shutdown_workers()
            page.deleteLater()

    def test_repository_failures_are_independent_and_keep_page_result(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for failed_side in ("records", "history"):
                with self.subTest(failed_side=failed_side):
                    real_records = CollectionRepository(
                        root / f"{failed_side}-records.json"
                    )
                    real_history = CollectionHistoryRepository(
                        root / f"{failed_side}-history.json"
                    )
                    records = (
                        FailingRecordsRepository(real_records, "disk_full")
                        if failed_side == "records"
                        else real_records
                    )
                    history = (
                        FailingHistoryRepository(real_history, "permission_denied")
                        if failed_side == "history"
                        else real_history
                    )
                    failed_item = xhs_item(f"{failed_side}-safe")
                    failed_item.cover_url = (
                        "https://images.example.test/failure.jpg"
                    )
                    failed_item.cover_status = "downloaded"
                    failed_item.cover_local_path = (
                        f"xiaohongshu/{failed_side}-safe.jpg"
                    )
                    service = FakeApplicationService(
                        CollectResult.complete([failed_item])
                    )
                    service.cover_summary = CollectionCoverSummary(
                        enabled=True,
                        eligible_count=1,
                        downloaded_count=1,
                        code="settled",
                    )
                    page = CollectionPage(
                        self.cfg,
                        collection_service=service,
                        history_repository=history,
                        records_repository=records,
                    )
                    page.input.setText(XHS_URL)
                    with patch(
                        "src.ui.collection_page.dialogs.confirm",
                        return_value=True,
                    ):
                        page._on_grab()
                    self._wait_page_workers(page)

                    self.assertEqual(
                        page._items[0].work_id, f"{failed_side}-safe"
                    )
                    if failed_side == "records":
                        self.assertIn("作品主档", page.status.text())
                        self.assertEqual(len(real_history.load().active_records), 1)
                    else:
                        self.assertIn("任务目录不可写", page.status.text())
                        self.assertEqual(len(real_records.load().items), 1)
                    self.assertIn(
                        "封面文件已保存，但任务更新失败",
                        page.status.text(),
                    )
                    self.assertNotIn(
                        "封面已保存 1/1",
                        page.status.text(),
                    )
                    page.shutdown_workers()
                    page.deleteLater()

    def test_dedicated_login_entry_keeps_browser_open_and_enables_close(self):
        self.page.input.setText(XHS_URL)
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_xhs_login()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.page._xhs_login_worker is not None:
            self.app.processEvents()
            time.sleep(0.002)

        self.assertIsNone(self.page._xhs_login_worker)
        self.assertEqual(self.service.opened, 1)
        self.assertEqual(self.service.closed, 0)
        self.assertTrue(self.service.browser_status.running)
        self.assertTrue(self.page.xhs_close_btn.isEnabled())
        self.assertIn("专用浏览器已打开", self.page.xhs_browser_status.text())
        self.assertTrue(self.page.input.isEnabled())

    def test_late_ready_updates_ui_without_second_open_or_early_close(self):
        previous = self.page
        service = DelayedLoginApplicationService()
        page = CollectionPage(self.cfg, collection_service=service)
        self.page = page
        page.input.setText(XHS_URL)

        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            page._on_xhs_login()
        self.assertTrue(service.login_entered.wait(0.5))
        self.app.processEvents()
        self.assertEqual(service.opened, 1)
        self.assertIsNotNone(page._xhs_login_worker)
        self.assertTrue(page._xhs_login_worker.isRunning())
        self.assertIn("同一个", page.status.text())
        self.assertFalse(page.xhs_close_btn.isEnabled())

        service.login_ready.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and page._xhs_login_worker is not None:
            self.app.processEvents()
            time.sleep(0.002)

        self.assertIsNone(page._xhs_login_worker)
        self.assertEqual(service.opened, 1)
        self.assertEqual(service.closed, 0)
        self.assertTrue(page.xhs_close_btn.isEnabled())
        self.assertIn("专用浏览器已打开", page.xhs_browser_status.text())
        page.shutdown_workers()
        page.deleteLater()
        self.page = previous

    def test_untrusted_lost_and_cleanup_unknown_keep_close_disabled(self):
        self.page.input.setText(XHS_URL)
        cases = (
            XhsBrowserStatus(
                "cdp_discovery_invalid",
                False,
                False,
                "专用浏览器连接尚未可信就绪，请重试。",
            ),
            XhsBrowserStatus(
                "browser_lost",
                False,
                False,
                "专用浏览器已关闭；重试会重新打开。",
            ),
            XhsBrowserStatus(
                "task_cleanup_unproven",
                True,
                True,
                "本次采集的停止状态尚未确认。",
            ),
        )
        for status in cases:
            with self.subTest(code=status.code):
                self.service.browser_status = status
                self.page._sync_xhs_browser_status()
                self.assertFalse(self.page.xhs_close_btn.isEnabled())
                self.assertEqual(
                    self.page.xhs_browser_status.text(),
                    status.message,
                )

    def test_explicit_close_is_idle_only_and_failure_remains_honest(self):
        self.page.input.setText(XHS_URL)
        self.service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        self.page._sync_xhs_browser_status()
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_xhs_close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.page._xhs_close_worker is not None:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertIsNone(self.page._xhs_close_worker)
        self.assertEqual(self.service.closed, 1)
        self.assertFalse(self.service.browser_status.running)
        self.assertIn("已关闭", self.page.status.text())

        self.service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        self.service.close_result = XhsBrowserStatus(
            "close_failed_browser_alive",
            True,
            False,
            "专用浏览器仍在运行。请在浏览器窗口中正常退出，或稍后重试。",
        )
        self.page._sync_xhs_browser_status()
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            self.page._on_xhs_close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.page._xhs_close_worker is not None:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(self.service.browser_status.running)
        self.assertIn("仍在运行", self.page.status.text())
        self.assertTrue(self.page.xhs_close_btn.isEnabled())

    def test_collection_cancel_keeps_browser_status_and_never_calls_close(self):
        previous = self.page
        service = BlockingApplicationService()
        service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        page = CollectionPage(self.cfg, collection_service=service)
        self.page = page
        page.input.setText(XHS_URL)
        with patch("src.ui.collection_page.dialogs.confirm", return_value=True):
            page._on_grab()
        self.assertTrue(service.started.wait(1.0))
        page._on_grab()
        self._wait_page_workers(page)

        self.assertTrue(service.cancel_seen.is_set())
        self.assertEqual(service.closed, 0)
        self.assertTrue(service.browser_status.running)
        self.assertIn("浏览器保持打开", page.status.text())
        page.shutdown_workers()
        page.deleteLater()
        self.page = previous

    def test_exit_choices_keep_close_cancel_and_close_failure_are_explicit(self):
        service = FakeApplicationService()
        service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        owner = SimpleNamespace(_collection_service=service)

        for choice, expected in ((None, "cancel"), (0, "keep"), (1, "close")):
            with self.subTest(choice=choice), patch(
                "src.ui.main_window.dialogs.choose",
                return_value=choice,
            ):
                self.assertEqual(
                    MainWindow._choose_xhs_exit_action(owner),
                    expected,
                )

        self.assertTrue(MainWindow._settle_xhs_exit(owner, "keep"))
        self.assertEqual((service.released, service.closed), (1, 0))

        service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        self.assertTrue(MainWindow._settle_xhs_exit(owner, "close"))
        self.assertEqual(service.closed, 1)

        service.browser_status = XhsBrowserStatus(
            "ready_idle",
            True,
            False,
            "专用浏览器已打开；登录状态由平台页面确认。",
        )
        service.close_result = XhsBrowserStatus(
            "close_failed_browser_alive",
            True,
            False,
            "专用浏览器仍在运行。",
        )
        with patch("src.ui.main_window.dialogs.choose", return_value=0):
            self.assertFalse(MainWindow._settle_xhs_exit(owner, "close"))
        self.assertTrue(service.browser_status.running)
        with patch("src.ui.main_window.dialogs.choose", return_value=1):
            self.assertTrue(MainWindow._settle_xhs_exit(owner, "close"))
        self.assertGreaterEqual(service.released, 2)

    def test_paper_keep_workspace_and_browser_controls_at_dual_sizes(self):
        previous = self.page
        for name in ("warm",):
            for size in ((1200, 760), (1480, 900)):
                with self.subTest(theme=name, size=size):
                    T.apply_theme(name)
                    service = FakeApplicationService()
                    service.browser_status = XhsBrowserStatus(
                        "ready_idle",
                        True,
                        False,
                        "专用浏览器已打开；登录状态由平台页面确认。",
                    )
                    geometry_cfg = SimpleNamespace(
                        collect_cover=False,
                        feishu_target={},
                        collection_mode="partial",
                    )
                    page = CollectionPage(
                        geometry_cfg,
                        collection_service=service,
                    )
                    page.resize(*size)
                    page.input.setText(DOUYIN_URL)
                    page.show()
                    self.app.processEvents()
                    self.assertTrue(page.grab_btn.isVisible())
                    self.assertFalse(page.full_btn.isVisible())
                    page.m_full_btn.click()
                    self.app.processEvents()
                    self.assertFalse(page.grab_btn.isVisible())
                    self.assertTrue(page.full_btn.isVisible())
                    self.assertEqual(page.full_btn.text(), "先登录专用浏览器")
                    self.assertGreater(page.full_panel.width(), 0)
                    page.m_quick_btn.click()
                    self.app.processEvents()
                    self.assertTrue(page.grab_btn.isVisible())
                    self.assertFalse(page.full_btn.isVisible())
                    page.input.setText(XHS_URL)
                    self.app.processEvents()
                    self.assertEqual(page.property("workspacePage"), "collection")
                    self.assertEqual(page.property("consumerThemeScope"), "coreWorkspace")
                    strengths = {
                        frame.property("paperTextureStrength")
                        for frame in page.findChildren(PaperTextureFrame)
                    }
                    consumer = T.consumer_theme(name)
                    self.assertIn(consumer.paper_canvas_opacity, strengths)
                    self.assertIn(consumer.paper_queue_opacity, strengths)
                    self.assertGreater(page.table.viewport().height(), 0)
                    self.assertTrue(page.xhs_login_btn.isVisible())
                    self.assertTrue(page.xhs_close_btn.isVisible())
                    self.assertTrue(page.xhs_browser_status.isVisible())
                    self.assertGreater(page.xhs_login_btn.width(), 0)
                    self.assertGreater(page.xhs_close_btn.width(), 0)
                    self.assertEqual(page.count_label.text(), "共 0 条")
                    self.assertEqual(
                        page.table.horizontalHeaderItem(11).text(),
                        "封面",
                    )
                    self.assertGreaterEqual(page.table.columnWidth(11), 68)
                    self.assertEqual(
                        page.count_label.geometry().center().y(),
                        page.select_all_btn.geometry().center().y(),
                    )
                    self.assertEqual(
                        page.focus_btn.geometry().center().y(),
                        page.select_all_btn.geometry().center().y(),
                    )
                    shell_right = page.results_shell.contentsRect().right()
                    focus_right = page.focus_btn.mapTo(
                        page.results_shell,
                        page.focus_btn.rect().bottomRight(),
                    ).x()
                    self.assertLessEqual(focus_right, shell_right)
                    self.assertGreater(
                        page.history_select.geometry().top(),
                        page.focus_btn.geometry().bottom(),
                    )
                    page._set_items([xhs_item(f"{name}_{size[0]}")])
                    self.app.processEvents()
                    self.assertEqual(page.count_label.text(), "共 1 条")
                    page._toggle_table_focus()
                    self.app.processEvents()
                    self.assertEqual(page.focus_btn.text().strip(), "退出放大")
                    self.assertEqual(
                        page.focus_btn.geometry().center().y(),
                        page.select_all_btn.geometry().center().y(),
                    )
                    page.shutdown_workers()
                    page.deleteLater()
        self.page = previous

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
