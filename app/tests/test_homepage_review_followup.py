"""External review regressions; temporary data, socket pairs, no external service."""
import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.collect import douyin_browser as browser, douyin_fetch as fetch
from src.core.collect.adapters.douyin import DouyinAdapter
from src.core.collect.contracts import CollectRequest
from src.core.collect.router import detect_link
from tests.test_homepage_reliability import AUTHOR, URL, Page, packet, work


def collect(page, mode="profile_all"):
    with patch.object(browser, "_open_page", return_value=page), patch.object(browser.time, "sleep"):
        return DouyinAdapter().collect(CollectRequest(detect_link(URL), mode))


class PaginationReviewTests(unittest.TestCase):
    def test_conflicting_first_page_never_means_complete(self):
        result = collect(Page([packet([work("1")], more=1), packet([], more=0), None]))
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))
        self.assertEqual([item.work_id for item in result.items], ["1"])

    def test_duplicate_identical_page_still_allows_complete(self):
        result = collect(Page([packet([work("1")], more=1), packet([work("1")], more=1),
                               packet([work("2")], cursor="123"), None]))
        self.assertEqual(result.completion, "complete")
        self.assertEqual([item.work_id for item in result.items], ["1", "2"])

    def test_conflicting_middle_page_preserves_valid_works(self):
        result = collect(Page([packet([work("1")], more=1),
                               packet([work("2")], more=1, cursor="123", next_cursor=456),
                               packet([work("3")], more=0, cursor="123"), None]))
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))
        self.assertEqual([item.work_id for item in result.items], ["1", "2", "3"])

    def test_conflicting_work_set_in_same_terminal_page_is_not_complete(self):
        result = collect(Page([packet([work("1")]), packet([work("2")]), None]))
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))
        self.assertEqual(len(result.items), 2)

    def test_foreign_tail_keeps_target_works_without_claiming_platform_revision(self):
        result = collect(Page([packet([work("1")], more=1),
                               packet([work("2"), work("3", "MS4w_other")], cursor="123"), None]))
        self.assertEqual((result.completion, result.reason_code), ("partial", "incomplete"))
        self.assertEqual([item.work_id for item in result.items], ["1", "2"])
        self.assertNotIn("改版", result.next_action)

    def test_first_page_can_arrive_after_three_empty_waits(self):
        result = collect(Page([None, None, None, packet([work("1")]), None]))
        self.assertEqual(result.completion, "complete")

    def test_first_page_deadline_is_bounded_and_honest(self):
        clock = [0.0]
        page = Page()
        def wait(**kwargs):
            clock[0] += kwargs["timeout"]
        page.listen.wait = wait
        with patch.object(browser.time, "monotonic", side_effect=lambda: clock[0]):
            result = collect(page)
        self.assertEqual((result.completion, result.reason_code), ("failed", "no_items_unverified"))
        self.assertGreaterEqual(clock[0], 40)
        self.assertLessEqual(clock[0], 50)
        self.assertNotIn("作者无公开作品", result.next_action)

    def test_malformed_cursor_of_exact_author_is_format_error(self):
        for cursor in ("abc", "-1", "9" * 5000):
            with self.subTest(cursor=cursor[:10]):
                result = collect(Page([packet([work("1")], cursor=cursor)]))
                self.assertEqual(result.reason_code, "platform_changed")
                self.assertFalse(result.items)

    def test_foreign_author_bad_cursor_is_still_ignored(self):
        result = collect(Page([packet(cursor="abc", author="MS4w_other"), packet([work()]), None]))
        self.assertEqual(result.completion, "complete")

    def test_missing_list_is_not_silently_accepted_as_terminal(self):
        for value in ({"status_code": 0, "has_more": 0},
                      {"status_code": 0, "has_more": 0, "aweme_list": None}):
            with self.subTest(value=value):
                result = collect(Page([packet([work()], more=1), packet(cursor="123", body=value)]))
                self.assertEqual((result.completion, result.reason_code), ("partial", "platform_changed"))
                self.assertEqual(len(result.items), 1)


class LinkAndDetailReviewTests(unittest.TestCase):
    def test_direct_mobile_share_forms_bind_exact_identity_without_network(self):
        with patch.object(fetch.urllib.request, "build_opener") as opened:
            for host in ("www.douyin.com", "www.iesdouyin.com"):
                for kind, identity, expected in (("user", AUTHOR, "homepage"), ("video", "1001", "work")):
                    raw = f"看看作品 https://{host}/share/{kind}/{identity}/?from=mobile 。"
                    with self.subTest(raw=raw):
                        linked = detect_link(raw)
                        self.assertEqual((linked.platform, linked.kind), ("douyin", expected))
                        result = fetch._resolve_douyin_link(raw)
                        self.assertEqual(result.extracted_id, identity)
            opened.assert_not_called()

    def test_lookalike_mobile_host_is_rejected(self):
        for raw in ("https://iesdouyin.com.evil.test/share/video/1001",
                    "https://www.iesdouyin.com@evil.test/share/video/1001",
                    "https://www.iesdouyin.com:1234/share/video/1001"):
            with self.subTest(raw=raw), self.assertRaises(fetch.DouyinCollectError):
                fetch._resolve_douyin_link(raw)

    def test_detail_failure_mapping_is_specific_and_safe(self):
        cases = (({"status_code": 0}, "no_items_unverified"),
                 ({"status_code": 1}, "network_error"),
                 ({"status_code": 0, "aweme_detail": work("999")}, "platform_changed"),
                 ({"status_code": 0, "aweme_detail": work(video=["bad"])}, "platform_changed"))
        for payload, reason in cases:
            def latest(*_args):
                return [fetch.parse_aweme_detail(payload, "1001")]
            with self.subTest(payload=payload):
                result = DouyinAdapter(fetch_latest_fn=latest).collect(CollectRequest(detect_link(URL), "anonymous_latest"))
                self.assertEqual(result.reason_code, reason)
                self.assertNotIn("程序异常", result.next_action)
                self.assertNotIn("发生异常", result.next_action)


class BrowserCloseReviewTests(unittest.TestCase):
    def test_latest_close_failure_preserves_works_and_reports_unconfirmed_close(self):
        page = Page([packet([work()])])
        def quit():
            raise RuntimeError("synthetic private diagnostics")
        page.quit = quit
        result = collect(page, "profile_latest")
        self.assertEqual((result.completion, result.reason_code), ("partial", "browser_unavailable"))
        self.assertEqual(len(result.items), 1)
        self.assertNotIn("private", result.next_action)

    def test_close_failure_cannot_replace_original_cause(self):
        page = Page([packet(status=403)])
        page.quit = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
        result = collect(page, "profile_latest")
        self.assertEqual(result.reason_code, "permission_denied")

    def test_legacy_login_exits_when_owned_browser_is_closed(self):
        page = Page()
        page.states.is_alive = False
        page.cookies = lambda: []
        with patch.object(browser, "_open_page", return_value=page), \
                patch.object(browser.time, "sleep") as slept:
            result = browser.login(timeout_s=300)
        self.assertFalse(result)
        self.assertTrue(page.closed)
        slept.assert_not_called()


class AnonymousReadReviewTests(unittest.TestCase):
    def run_blocked_response(self, *, detail=False, deadline=None, cancel=True, drip=False):
        client, server = socket.socketpair()
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n{")
        response = http.client.HTTPResponse(client)
        response.begin()
        entered, cancelled = threading.Event(), threading.Event()
        stop_sender = threading.Event()
        def send_slowly():
            try:
                while not stop_sender.wait(.01):
                    server.sendall(b" ")
            except OSError:
                pass
        sender = threading.Thread(target=send_slowly) if drip else None
        if sender is not None:
            sender.start()
        original_read = response.read
        def read(*args):
            entered.set()
            return original_read(*args)
        response.read = read
        response.__enter__ = lambda: response
        outcome = []
        def run():
            try:
                if detail:
                    fetch.fetch_item("1001", is_cancelled=cancelled.is_set)
                else:
                    fetch.fetch_latest(URL, is_cancelled=cancelled.is_set)
            except BaseException as exc:
                outcome.append(exc)
        signer = SimpleNamespace(generate_abogus=lambda _: ("", "fixture", "fixture", ""))
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(fetch, "_register_ttwid", return_value="ttwid=fixture"))
            stack.enter_context(patch.object(fetch, "ABogus", return_value=signer))
            stack.enter_context(patch.object(fetch.urllib.request, "urlopen", return_value=response))
            if deadline is not None:
                stack.enter_context(patch.object(fetch, "_ANONYMOUS_REQUEST_TIMEOUT_S", deadline, create=True))
            worker = threading.Thread(target=run)
            try:
                worker.start()
                did_enter = entered.wait(1)
                if cancel:
                    cancelled.set()
                worker.join(.8)
                stopped = not worker.is_alive()
            finally:
                stop_sender.set()
                server.close()
                if sender is not None:
                    sender.join(1)
                worker.join(2)
                response.close()
                client.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(any(t.name == "lulu-douyin-response" for t in threading.enumerate()))
        self.assertTrue(did_enter)
        self.assertTrue(stopped, "read stayed blocked until the peer was closed")
        self.assertEqual(len(outcome), 1)
        return outcome[0]

    def test_cancel_interrupts_real_socket_response_read(self):
        self.assertIsInstance(self.run_blocked_response(), InterruptedError)

    def test_detail_read_also_accepts_and_obeys_cancel(self):
        self.assertIsInstance(self.run_blocked_response(detail=True), InterruptedError)

    def test_whole_response_deadline_interrupts_blocked_read(self):
        error = self.run_blocked_response(deadline=.15, cancel=False)
        self.assertEqual(getattr(error, "code", ""), "DOUYIN_NETWORK_ERROR")

    def test_continuous_slow_response_cannot_extend_request_deadline(self):
        error = self.run_blocked_response(deadline=.15, cancel=False, drip=True)
        self.assertEqual(getattr(error, "code", ""), "DOUYIN_NETWORK_ERROR")

    def test_successful_real_socket_response_still_parses_without_watcher_leak(self):
        client, server = socket.socketpair()
        body = json.dumps({"status_code": 0, "aweme_list": [work()]}).encode()
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        response = http.client.HTTPResponse(client)
        response.begin()
        try:
            with patch.object(fetch, "_register_ttwid", return_value="ttwid=fixture"), \
                    patch.object(fetch.urllib.request, "urlopen", return_value=response):
                items = fetch.fetch_latest(URL, is_cancelled=lambda: False)
            self.assertEqual([item.author_id for item in items], [AUTHOR])
            self.assertFalse(any(t.name == "lulu-douyin-response" for t in threading.enumerate()))
        finally:
            response.close()
            client.close()
            server.close()

    def test_cancelled_work_link_never_initializes_anonymous_session(self):
        with patch.object(fetch, "_register_ttwid") as session, self.assertRaises(InterruptedError):
            fetch.fetch_item("1001", is_cancelled=lambda: True)
        session.assert_not_called()
