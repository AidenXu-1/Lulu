"""Native button events and real callback contexts, using isolated services."""
import errno
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import ANY, Mock, patch
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from src.core.collect.application import CollectionCommand
from src.core.collect.contracts import CollectResult
from src.core.collect.douyin_fetch import DouyinCollectError
from src.core.collect.models import CollectedItem
from src.ui import collection_page as ui
from tests import test_douyin_partial_login_ui as fixture_module


class PendingWorker:
    def __init__(self, *_args):
        self.progress, self.done, self.failed, self.finished = (Mock() for _ in range(4))
        self.running = False
        self.cancels = 0
    def isRunning(self): return self.running
    def start(self): self.running = True
    def request_cancel(self): self.cancels += 1


class HomepageUiReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DouyinPartialLoginUiTests(methodName="runTest")
        self.fixture.setUpClass(); self.fixture.setUp()
        self.page = self.fixture.page
    def tearDown(self):
        self.fixture.tearDown()

    def test_fast_clicks_ignore_second_then_explicit_stop_still_works(self):
        with patch.object(ui, "_CollectionWorker", PendingWorker):
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            worker = self.page._worker
            QTest.qWait(50)
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            self.assertEqual(worker.cancels, 0)
            self.page._collection_started_monotonic -= 2
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            self.assertEqual(worker.cancels, 1)
            self.assertTrue(self.page._stopping_collection)

    def test_fast_completed_task_double_click_writes_only_one_history_then_allows_retry(self):
        from src.core.collect.history import CollectionHistoryRepository
        from src.core.collect.repository import CollectionRepository
        calls = []
        def collect(*_args, **_kwargs):
            calls.append(True)
            return CollectResult.complete([CollectedItem("1001", author_name="隔离作者")])
        self.page._service.collect = collect
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.page._history_repo = CollectionHistoryRepository(root / "history.json", legacy_path=root / "legacy.json")
            self.page._records_repo = CollectionRepository(root / "records.json")
            self.page._history_snapshot = self.page._history_repo.load()
            def settle():
                deadline = time.perf_counter() + 2
                while self.page._worker is not None and time.perf_counter() < deadline:
                    QTest.qWait(5)
                self.assertIsNone(self.page._worker)
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            settle()
            # Keep the double-click timing deterministic even on slow CI disks.
            self.page._collection_started_monotonic = time.monotonic()
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            settle()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(self.page._history_repo.load().active_records), 1)
            self.page._collection_started_monotonic -= 2
            QTest.mouseClick(self.page.grab_btn, Qt.LeftButton)
            settle()
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(self.page._history_repo.load().active_records), 2)

    def test_full_mode_double_click_does_not_stop_or_create_second_worker(self):
        self.page._douyin_login_state = lambda: ("已保存浏览器登录", False)
        self.fixture.confirm.return_value = True
        self.page.m_full_btn.click()
        self.fixture.app.processEvents()
        with patch.object(ui, "_CollectionWorker", PendingWorker):
            QTest.mouseClick(self.page.full_btn, Qt.LeftButton)
            worker = self.page._full_worker
            self.assertIsNotNone(worker)
            QTest.mouseDClick(self.page.full_btn, Qt.LeftButton)
            QTest.mouseRelease(self.page.full_btn, Qt.LeftButton)
            self.assertEqual(worker.cancels, 0)
            self.assertIs(self.page._full_worker, worker)
            self.page._collection_started_monotonic -= 2
            QTest.mouseClick(self.page.full_btn, Qt.LeftButton)
            self.assertEqual(worker.cancels, 1)

    def test_start_reentry_cannot_replace_current_worker_or_token(self):
        command = CollectionCommand(fixture_module.URL, "anonymous_latest", limit=3)
        with patch.object(ui, "_CollectionWorker", PendingWorker):
            self.page._start_collect(command, full=False)
            worker, token = self.page._worker, self.page._run_token
            self.page._start_collect(command, full=False)
            self.assertIs(self.page._worker, worker)
            self.assertEqual(self.page._run_token, token)

    def test_date_is_default_but_explicit_source_order_is_preserved(self):
        items = [CollectedItem("1", title="置顶旧作", create_time=100),
                 CollectedItem("2", title="较新作品", create_time=200)]
        _, worker, _ = self.fixture.deliver(result=CollectResult.complete(items))
        self.fixture.finish(worker)
        self.assertEqual([x.work_id for x in self.page._view], ["2", "1"])
        self.assertEqual(self.page.sort_combo.currentData(), "3")
        self.page.sort_combo.set_value("0", emit=True)
        self.assertEqual([x.work_id for x in self.page._view], ["1", "2"])
        _, worker, _ = self.fixture.deliver(result=CollectResult.complete(items))
        self.fixture.finish(worker)
        self.assertEqual([x.work_id for x in self.page._view], ["1", "2"])

    def test_hidden_failure_and_later_hide_never_promise_absent_button(self):
        self.page.hide()
        _, worker, _ = self.fixture.deliver()
        self.fixture.finish(worker)
        self.page.show(); self.fixture.app.processEvents()
        self.assertTrue(self.page.partial_login_btn.isHidden())
        self.assertNotIn("可点击", self.page.status.text())
        _, worker, _ = self.fixture.deliver()
        self.fixture.finish(worker)
        self.assertFalse(self.page.partial_login_btn.isHidden())
        self.page.hide()
        self.assertNotIn("可点击", self.page.status.text())

    def test_partial_errors_keep_specific_guidance_and_retained_count(self):
        for reason, expected in (("captcha", "验证"), ("incomplete", "未能确认已采全"),
                                 ("profile_busy", "占用"), ("browser_closed", "浏览器已关闭")):
            with self.subTest(reason=reason):
                result = CollectResult.partial([CollectedItem("1")], reason_code=reason,
                                               next_action="private-token must not display")
                _, worker, _ = self.fixture.deliver(mode="profile_all", result=result)
                self.fixture.finish(worker)
                self.assertIn(expected, self.page.status.text())
                self.assertIn("已保留 1 条", self.page.status.text())
                self.assertNotIn("private-token", self.page.status.text())


    def test_out_of_range_platform_date_does_not_break_result_table(self):
        item = CollectedItem("1", title="日期异常，作品保留", create_time=10**30)
        _, worker, _ = self.fixture.deliver(result=CollectResult.complete([item]))
        self.fixture.finish(worker)
        self.assertEqual(self.page.table.rowCount(), 1)
        self.assertEqual(self.page._view[0].work_id, "1")
        self.assertEqual(item.publish_date, "")

    def test_huge_platform_date_is_safely_excluded_by_date_filter(self):
        from src.core.collect.douyin_fetch import parse_author_page
        from tests.test_homepage_reliability import AUTHOR, work
        item = parse_author_page({"status_code": 0, "aweme_list": [work(create_time=10**1000)]}, AUTHOR)[0]
        self.page._items = [item]
        self.page._date_mode = "7"
        self.page._render()
        self.assertEqual(self.page.table.rowCount(), 0)
        self.assertEqual(self.page._items, [item])

    def test_huge_platform_duration_can_be_displayed_and_filtered(self):
        from src.core.collect.douyin_fetch import parse_author_page
        from tests.test_homepage_reliability import AUTHOR, work
        milliseconds = 10**1000
        item = parse_author_page({"status_code": 0, "aweme_list": [work(video={"duration": milliseconds})]}, AUTHOR)[0]
        self.page._items = [item]
        self.page._render()
        self.assertEqual(self.page.table.rowCount(), 1)
        self.assertEqual(item.duration_text, f"{(milliseconds // 1000) // 60}:{(milliseconds // 1000) % 60:02d}")
        self.assertTrue(self.page._matches_field_filter(item, ("duration_sec", "numeric", "gt", 1.001)))
        self.assertFalse(self.page._matches_field_filter(item, ("duration_sec", "numeric", "lt", 1.001)))
        ordinary = CollectedItem("2", duration_ms=1001)
        self.assertTrue(self.page._matches_field_filter(ordinary, ("duration_sec", "numeric", "eq", 1.001)))
        for value, display in ((500, "0:00"), (1500, "0:02"), (2500, "0:02"), (3500, "0:04")):
            self.assertEqual(CollectedItem("2", duration_ms=value).duration_text, display)


    def test_expired_and_unconfirmed_login_progress_keep_safe_instructions(self):
        from src.core.collect.router import detect_link
        token = self.page._run_token
        self.page._history_context[token] = (
            CollectionCommand(fixture_module.URL, "profile_latest"), detect_link(fixture_module.URL))
        for message, expected in (
            ("登录可能已失效，请在专用浏览器中手动登录；secret-cookie", "重新登录"),
            ("暂未读取到作品，请在专用浏览器检查登录是否有效；secret-cookie", "检查登录"),
        ):
            with self.subTest(message=message):
                self.page._on_collect_progress(token, 0, message)
                self.assertIn(expected, self.page.status.text())
                self.assertNotIn("secret-cookie", self.page.status.text())


class DownloadCauseTests(unittest.TestCase):
    def test_invalid_remote_content_refreshes_same_work_once(self):
        from src.core.audio import extractor
        item = CollectedItem("1001", play_url="https://v26-weba.douyinvod.com/video/old.mp4",
                             work_url="https://www.douyin.com/video/1001")
        refreshed = replace(item, play_url="https://v26-weba.douyinvod.com/video/fresh.mp4")
        valid = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00"
        for wrong in (b"<html>expired</html>", b'{"expired":true}', b"", b"invalid media header"):
            calls = []
            def stream(url, destination, *_args, **_kwargs):
                calls.append(url)
                Path(destination).write_bytes(wrong if len(calls) == 1 else valid)
                return destination
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as raw, \
                    patch.object(extractor, "_stream_download", side_effect=stream), \
                    patch("src.core.collect.douyin_fetch.fetch_item", return_value=refreshed) as refresh:
                target = ui._fetch_managed_media(item, "video", Path(raw), lambda: False)
                self.assertEqual(target.read_bytes(), valid)
                self.assertEqual(calls, [item.play_url, refreshed.play_url])
                refresh.assert_called_once_with(item.work_id, is_cancelled=ANY)

    def test_twice_invalid_remote_content_stops_after_single_refresh(self):
        from src.core.audio import extractor
        item = CollectedItem("1001", play_url="https://v26-weba.douyinvod.com/video/old.mp4",
                             work_url="https://www.douyin.com/video/1001")
        calls = []
        def stream(url, destination, *_args, **_kwargs):
            calls.append(url)
            Path(destination).write_bytes(b"<html>unavailable</html>")
            return destination
        with tempfile.TemporaryDirectory() as raw, patch.object(extractor, "_stream_download", side_effect=stream), \
                patch("src.core.collect.douyin_fetch.fetch_item", return_value=item) as refresh:
            with self.assertRaises(ui.ManagedMediaFetchError) as raised:
                ui._fetch_managed_media(item, "video", Path(raw), lambda: False)
            self.assertEqual(raised.exception.reason_code, "media_source_refresh_failed")
            self.assertEqual(len(calls), 2)
            refresh.assert_called_once_with(item.work_id, is_cancelled=ANY)

    def test_disk_permission_and_unknown_errors_do_not_refresh_source(self):
        item = CollectedItem("1001", play_url="https://v26-weba.douyinvod.com/video/1001.mp4",
                             work_url="https://www.douyin.com/video/1001")
        cases = ((OSError(errno.ENOSPC, "synthetic"), "media_disk_full"),
                 (PermissionError(errno.EACCES, "synthetic"), "media_permission_denied"),
                 (RuntimeError("synthetic private request"), "media_download_failed"))
        for error, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw, \
                    patch("src.core.audio.extractor.download_douyin_video_cdn", side_effect=error), \
                    patch("src.core.collect.douyin_fetch.fetch_item") as refresh:
                with self.assertRaises(ui.ManagedMediaFetchError) as raised:
                    ui._fetch_managed_media(item, "video", Path(raw), lambda: False)
                self.assertEqual(raised.exception.reason_code, expected)
                self.assertNotIn("private request", str(raised.exception))
                refresh.assert_not_called()

    def test_transient_network_failure_can_refresh_same_work_once(self):
        from urllib.error import URLError
        from src.core.audio import extractor
        item = CollectedItem("1001", play_url="https://v26-weba.douyinvod.com/video/old.mp4",
                             work_url="https://www.douyin.com/video/1001")
        refreshed = replace(item, play_url="https://v26-weba.douyinvod.com/video/fresh.mp4")
        calls = []
        def stream(url, destination, *_args, **_kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise URLError("synthetic temporary failure")
            Path(destination).write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            return destination
        with tempfile.TemporaryDirectory() as raw, patch.object(extractor, "_stream_download", side_effect=stream), \
                patch("src.core.collect.douyin_fetch.fetch_item", return_value=refreshed) as refresh:
            result = ui._fetch_managed_media(item, "video", Path(raw), lambda: False)
            self.assertTrue(result.is_file())
            self.assertEqual(calls, [item.play_url, refreshed.play_url])
            refresh.assert_called_once_with(item.work_id, is_cancelled=ANY)

    def test_network_still_failing_after_refresh_keeps_network_cause(self):
        from urllib.error import URLError
        from src.core.audio import extractor
        item = CollectedItem("1001", play_url="https://v26-weba.douyinvod.com/video/old.mp4",
                             work_url="https://www.douyin.com/video/1001")
        refreshed = replace(item, play_url="https://v26-weba.douyinvod.com/video/fresh.mp4")
        with tempfile.TemporaryDirectory() as raw, \
                patch.object(extractor, "_stream_download", side_effect=URLError("synthetic")) as download, \
                patch("src.core.collect.douyin_fetch.fetch_item", return_value=refreshed) as refresh:
            with self.assertRaises(ui.ManagedMediaFetchError) as raised:
                ui._fetch_managed_media(item, "video", Path(raw), lambda: False)
            self.assertEqual(raised.exception.reason_code, "media_network_error")
            self.assertEqual(download.call_count, 2)
            refresh.assert_called_once_with(item.work_id, is_cancelled=ANY)

    def test_detail_refresh_receives_same_cancel_callback(self):
        item = CollectedItem("1001", work_url="https://www.douyin.com/video/1001")
        cancelled = lambda: False
        with tempfile.TemporaryDirectory() as raw, \
                patch("src.core.collect.douyin_fetch.fetch_item", side_effect=InterruptedError("cancelled")) as refresh:
            with self.assertRaises(InterruptedError):
                ui._fetch_managed_media(item, "video", Path(raw), cancelled)
        refresh.assert_called_once_with(item.work_id, is_cancelled=cancelled)
