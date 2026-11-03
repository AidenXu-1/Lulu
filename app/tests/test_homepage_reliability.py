"""Homepage collection failures use no network, browser, account or model."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.core.collect import douyin_browser as browser, douyin_fetch as fetch
from src.core.collect.adapters.douyin import DouyinAdapter
from src.core.collect.contracts import CollectRequest
from src.core.collect.models import CollectedItem
from src.core.collect.router import detect_link
from src.core.audio.extractor import _ensure_downloaded_media_looks_valid

AUTHOR = "MS4wLjABAAAA_reliability"
URL = "https://www.douyin.com/user/" + AUTHOR
POST = "https://www.douyin.com/aweme/v1/web/aweme/post/"


def work(wid="1001", author=AUTHOR, **changes):
    value = {"aweme_id": wid, "desc": "作品", "author": {
        "sec_uid": author, "nickname": "作者"}, "video": {"duration": 1000}}
    value.update(changes)
    return value


def packet(works=(), *, status=200, sc=0, more=0, cursor="0", next_cursor=123,
           author=AUTHOR, host="www.douyin.com", body=None):
    return SimpleNamespace(
        url=POST.replace("www.douyin.com", host) + f"?sec_user_id={author}&max_cursor={cursor}",
        response=SimpleNamespace(status=status, body=body if body is not None else {
            "status_code": sc, "aweme_list": list(works), "has_more": more,
            "max_cursor": next_cursor}))


class Listener:
    def __init__(self, events):
        self.events = list(events)
    def start(self, *_args, **_kwargs):
        pass
    def wait(self, count=1, timeout=None, fit_count=True, raise_err=None):
        event = self.events.pop(0) if self.events else None
        if isinstance(event, BaseException):
            raise event
        return event


class Page:
    def __init__(self, events=()):
        self.listen = Listener(events)
        self.states = SimpleNamespace(is_alive=True)
        self.url = URL
        self.paths = None
        self.closed = False
    def get(self, url):
        self.url = url
    def cookies(self, all_domains=False, all_info=False):
        return [{"name": "sessionid"}]
    def run_js(self, *_args, **_kwargs):
        return 100
    def eles(self, *_args, **_kwargs):
        return []
    def quit(self):
        self.closed = True


class FullCollectionTests(unittest.TestCase):
    def collect(self, events, *, page=None, cancelled=None):
        page = page or Page(events)
        with patch.object(browser, "_open_page", return_value=page), \
                patch.object(browser.time, "sleep"):
            result = DouyinAdapter().collect(CollectRequest(detect_link(URL), "profile_all"),
                                             is_cancelled=cancelled)
        self.assertTrue(page.closed)
        return result

    def test_valid_multi_page_keeps_source_order_and_deduplicates(self):
        result = self.collect([packet([work("1"), work("2")], more=1), None,
                               packet([work("2"), work("3")], cursor="123"), None])
        self.assertEqual(result.completion, "complete")
        self.assertEqual([x.work_id for x in result.items], ["1", "2", "3"])

    def test_empty_final_page_after_valid_first_is_complete(self):
        result = self.collect([packet([work()], more=1), None, packet(cursor="123"), None])
        self.assertEqual((result.completion, len(result.items)), ("complete", 1))

    def test_nonzero_business_state_never_contributes_works_or_completion(self):
        result = self.collect([packet([work("777")], sc=8), None])
        self.assertEqual(result.completion, "failed")
        self.assertFalse(result.items)

    def test_http_error_body_never_contributes_works(self):
        result = self.collect([packet([work("777")], status=403), None])
        self.assertEqual((result.completion, result.reason_code), ("failed", "permission_denied"))
        self.assertFalse(result.items)

    def test_text_http_errors_keep_cause(self):
        for status, reason in ((401, "login_required"), (403, "permission_denied"),
                               (429, "rate_limited"), (503, "network_error")):
            with self.subTest(status=status):
                result = self.collect([packet(status=status, body="blocked"), None])
                self.assertEqual((result.completion, result.reason_code), ("failed", reason))

    def test_other_author_and_lookalike_host_cannot_finish_target(self):
        for wrong in (packet([work("9", "MS4w_other")], author="MS4w_other"),
                      packet([work("9")], host="douyin.com.evil.test")):
            with self.subTest(url=wrong.url):
                result = self.collect([wrong, packet([work()]), None])
                self.assertEqual(result.completion, "complete")
                self.assertEqual([x.work_id for x in result.items], ["1001"])


    def test_bad_work_shape_keeps_prior_valid_work(self):
        result = self.collect([packet([work(), work("9", video=["bad"])]), None])
        self.assertEqual((result.completion, result.reason_code), ("partial", "platform_changed"))
        self.assertEqual([x.work_id for x in result.items], ["1001"])

    def test_disconnect_keeps_prior_valid_work(self):
        result = self.collect([packet([work()], more=1), RuntimeError("CDP disconnected")])
        self.assertEqual(result.completion, "partial")
        self.assertEqual([x.work_id for x in result.items], ["1001"])


    def test_tail_page_without_first_page_never_means_complete(self):
        result = self.collect([packet([work()], cursor="999"), None])
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))

    def test_missing_middle_page_never_means_complete(self):
        result = self.collect([packet([work()], more=1), None,
                               packet([work("2")], cursor="999"), None])
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))



class InputIdentityTests(unittest.TestCase):
    def test_only_homepage_path_supplies_direct_author_not_share_text(self):
        self.assertEqual(fetch.resolve_sec_uid("MS4w_wrong " + URL), AUTHOR)
        with patch.object(fetch.urllib.request, "build_opener") as opened:
            for text in ("MS4w_wrong", "https://www.douyin.com/", "https://evil.test/user/" + AUTHOR):
                with self.subTest(text=text), self.assertRaises(fetch.DouyinCollectError):
                    fetch.resolve_sec_uid(text)
            opened.assert_not_called()

    def test_work_url_binds_author_to_requested_work_detail(self):
        item = CollectedItem("123", author_id=AUTHOR)
        with patch.object(fetch, "fetch_item", return_value=item) as detail, \
                patch.object(fetch.urllib.request, "build_opener") as opened:
            self.assertEqual(fetch.resolve_sec_uid("https://www.douyin.com/video/123"), AUTHOR)
        detail.assert_called_once_with("123")
        opened.assert_not_called()




class SignedWorkIdentityTests(unittest.TestCase):
    def detail(self, *, requested="1001", returned="1001", author=AUTHOR):
        return SimpleNamespace(
            url=f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={requested}",
            response=SimpleNamespace(status=200, body={"status_code": 0,
                "aweme_detail": work(returned, author=author)}))

    def collect(self, mode, *, events=None, url="https://www.douyin.com/video/1001", cancel=None):
        page = Page(events if events is not None else [self.detail(), packet([work()]), None])
        page.visits = []
        original_get = page.get
        def get(target):
            page.visits.append(target)
            original_get(target)
        page.get = get
        with patch.object(fetch, "fetch_item", side_effect=fetch.DouyinCollectError(
                "synthetic denial", code="DOUYIN_ACCESS_RESTRICTED")) as anonymous, \
                patch.object(browser, "_open_page", return_value=page) as opened, \
                patch.object(browser.time, "sleep"):
            result = DouyinAdapter().collect(CollectRequest(detect_link(url), mode), is_cancelled=cancel)
        if mode != "anonymous_latest":
            self.assertTrue(page.closed)
            anonymous.assert_not_called()
        return result, page, opened

    def test_signed_latest_and_full_do_not_depend_on_anonymous_work_detail(self):
        for mode in ("profile_latest", "profile_all"):
            with self.subTest(mode=mode):
                result, page, opened = self.collect(mode)
                self.assertEqual(result.completion, "complete")
                self.assertEqual([x.author_id for x in result.items], [AUTHOR])
                self.assertEqual(page.visits[-2:], ["https://www.douyin.com/video/1001", URL])
                opened.assert_called_once()

    def test_anonymous_denial_still_never_opens_browser(self):
        result, _page, opened = self.collect("anonymous_latest")
        self.assertEqual((result.completion, result.reason_code), ("failed", "permission_denied"))
        opened.assert_not_called()

    def test_signed_short_work_link_preserves_exact_final_work(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = "https://www.iesdouyin.com/share/video/1001/"
        opener = Mock(); opener.open.return_value = response
        with patch.object(fetch.urllib.request, "build_opener", return_value=opener):
            result, page, _opened = self.collect("profile_latest", url="https://v.douyin.com/fixture/")
        self.assertEqual(result.completion, "complete")
        self.assertEqual(page.visits[-1], URL)

    def test_wrong_work_payload_cannot_redirect_to_its_author(self):
        result, page, _opened = self.collect("profile_latest", events=[self.detail(returned="999")])
        self.assertEqual((result.completion, result.reason_code), ("failed", "platform_changed"))
        self.assertNotIn(URL, page.visits)

    def test_unrelated_work_response_is_ignored(self):
        result, _page, _opened = self.collect("profile_latest", events=[self.detail(requested="999", returned="999"),
                                                                 self.detail(), packet([work()])])
        self.assertEqual(result.completion, "complete")

    def test_cancel_before_work_navigation_closes_owned_browser(self):
        cancelled = [False]
        page = Page([self.detail(), packet([work()])])
        page.visits = []
        page.get = page.visits.append
        def progress(message, _count):
            if "确认指定作品" in message:
                cancelled[0] = True
        with patch.object(browser, "_open_page", return_value=page):
            result = DouyinAdapter().collect(CollectRequest(detect_link("https://www.douyin.com/video/1001"),
                                                           "profile_latest"),
                                             is_cancelled=lambda: cancelled[0], progress=progress)
        self.assertEqual(result.completion, "cancelled")
        self.assertNotIn("https://www.douyin.com/video/1001", page.visits)
        self.assertTrue(page.closed)


class MediaMemoryTests(unittest.TestCase):
    def test_header_check_memory_stays_bounded_for_large_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sparse.mp4"
            with path.open("wb") as out:
                out.write(b"\x00\x00\x00\x18ftypisom")
                out.truncate(16 * 1024 * 1024)
            tracemalloc.start()
            try:
                _ensure_downloaded_media_looks_valid(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)
