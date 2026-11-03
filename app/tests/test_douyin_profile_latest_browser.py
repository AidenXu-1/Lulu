"""Explicit authenticated first-page collection, with an isolated fake browser."""
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from src.core.collect import douyin_browser as browser
from src.core.collect.douyin_fetch import DouyinCollectError


AUTHOR = "MS4w_latest-author"
URL = "https://www.douyin.com/user/" + AUTHOR
POST = "https://www.douyin.com/aweme/v1/web/aweme/post/"


def packet(count=18, *, author=AUTHOR, cursor="0", status=200, body=None, host="www.douyin.com"):
    if body is None:
        body = {"status_code": 0, "has_more": 1, "aweme_list": [
            {"aweme_id": str(1000 + i), "desc": "公开作品",
             "author": {"sec_uid": author, "nickname": "目标作者"}}
            for i in range(count)]}
    return SimpleNamespace(url=POST.replace("www.douyin.com", host) + "?sec_user_id=" + author + "&max_cursor=" + cursor,
                           response=SimpleNamespace(status=status, body=body))


class Listener:
    def __init__(self, packets):
        self.packets = list(packets)
        self.waits = 0

    def start(self, _target):
        pass

    def wait(self, **_kwargs):
        self.waits += 1
        return self.packets.pop(0) if self.packets else None


class Page:
    def __init__(self, packets=(), sessions=(True,)):
        self.listen = Listener(packets)
        self.sessions = list(sessions)
        self.states = SimpleNamespace(is_alive=True)
        self.paths = None
        self.url = "about:blank"
        self.visits = []
        self.closed = False

    def cookies(self, all_domains=False, all_info=False):
        active = self.sessions.pop(0) if len(self.sessions) > 1 else self.sessions[0]
        return [{"name": "sessionid"}] if active else []

    def get(self, url):
        self.visits.append(url)
        self.url = url

    def eles(self, *_args, **_kwargs):
        return []

    def quit(self):
        self.closed = True


class ProfileLatestBrowserTests(unittest.TestCase):



    def collect(self, page, **kwargs):
        with patch.object(browser, "_open_page", return_value=page), \
                patch.object(browser.time, "sleep", return_value=None), \
                patch.object(browser, "_bring_to_front"), \
                patch.object(browser, "_scroll_bottom", side_effect=AssertionError("must not scroll")):
            return browser.collect_latest(URL, **kwargs)

    def test_first_page_is_bounded_and_does_not_wait_for_all_pages(self):
        page = Page([packet(30), packet(30, cursor="123")])
        works, reason = self.collect(page, limit=20)
        self.assertEqual(len(works), 20)
        self.assertEqual(reason, "")
        self.assertTrue(all(work.author_id == AUTHOR for work in works))
        self.assertTrue(all(work.source_method == "profile_latest" for work in works))
        self.assertEqual(page.listen.waits, 1)
        self.assertEqual(page.visits[-1], URL)
        self.assertTrue(page.closed)


    def test_unrelated_author_or_cursor_packet_is_never_accepted(self):
        page = Page([packet(author="MS4w_other"), packet(cursor="123"), packet(2)])
        works, _ = self.collect(page)
        self.assertEqual(len(works), 2)
        self.assertEqual(page.listen.waits, 3)

    def test_official_regional_api_host_is_accepted(self):
        page = Page([packet(21, host="www-hj.douyin.com")])
        works, reason = self.collect(page, timeout_s=0.01)
        self.assertEqual(len(works), 20)
        self.assertEqual(reason, "")

    def test_lookalike_domain_is_ignored(self):
        page = Page([packet(host="douyin.com.evil.test"), packet(2)])
        works, _ = self.collect(page)
        self.assertEqual(len(works), 2)
        self.assertEqual(page.listen.waits, 2)




    def test_cancel_while_browser_opens_prevents_initial_navigation(self):
        page = Page([packet(2)])
        cancelled = [False]
        def opened(_path, *, is_cancelled=None):
            cancelled[0] = True
            return page
        with patch.object(browser, "_open_page", side_effect=opened):
            works, reason = browser.collect_latest(URL, is_cancelled=lambda: cancelled[0])
        self.assertEqual(works, [])
        self.assertIn("取消", reason)
        self.assertEqual(page.visits, [])
        self.assertTrue(page.closed)


    def test_cancel_after_login_prevents_author_navigation(self):
        page = Page([packet(2)])
        cancelled = [False]
        works, reason = self.collect(page,
            progress=lambda *_: cancelled.__setitem__(0, True),
            is_cancelled=lambda: cancelled[0])
        self.assertEqual(works, [])
        self.assertIn("取消", reason)
        self.assertEqual(page.visits, ["https://www.douyin.com/"])
        self.assertEqual(page.listen.waits, 0)
        self.assertTrue(page.closed)

    def test_manual_verification_keeps_window_open_and_then_resumes_first_page(self):
        page = Page([packet(2)])
        messages = []
        with patch.object(browser, "_captcha_present", side_effect=[True, True, False, False]):
            works, reason = self.collect(page, progress=lambda message, _count: messages.append(message))
        self.assertEqual(len(works), 2)
        self.assertEqual(reason, "")
        self.assertTrue(any("自行完成验证" in message for message in messages))
        self.assertEqual(page.visits.count(URL), 2)
        self.assertTrue(page.closed)

    def test_verification_timeout_does_not_accept_queued_works(self):
        page = Page([packet(2)])
        with patch.object(browser, "_captcha_present", return_value=True), self.assertRaises(DouyinCollectError) as caught:
            self.collect(page, verification_timeout_s=0)
        self.assertEqual(caught.exception.code, "DOUYIN_VERIFICATION_REQUIRED")
        self.assertEqual(page.listen.waits, 0)
        self.assertTrue(page.closed)

    def test_verification_deadline_includes_page_check_time(self):
        clock = [0.0]
        def present(*_args):
            clock[0] += 3
            return True
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(browser.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(browser.time, "sleep", side_effect=sleep), \
                patch.object(browser, "_captcha_present", side_effect=present):
            cleared = browser._wait_captcha_cleared(Page(), lambda:False, lambda *_:None, 0, timeout_s=4)
        self.assertFalse(cleared)
        self.assertEqual(clock[0], 4.0)

    def test_verification_cancel_does_not_accept_queued_works(self):
        cancelled = [False]
        page = Page([packet(2)])
        def progress(message, _count):
            if "验证" in message:
                cancelled[0] = True
        with patch.object(browser, "_captcha_present", return_value=True):
            works, reason = self.collect(page, progress=progress, is_cancelled=lambda: cancelled[0])
        self.assertEqual(works, [])
        self.assertIn("取消", reason)
        self.assertTrue(page.closed)




if __name__ == "__main__":
    unittest.main()
