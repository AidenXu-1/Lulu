"""抖音全量采集完成语义与失败保留的离线故障注入测试。"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.adapters.douyin import DouyinAdapter
from core.collect.contracts import CollectRequest, LinkIdentity
from core.collect.douyin_fetch import DouyinCollectError
from core.collect import douyin_browser


def _aweme(aid: str = "1001") -> dict:
    return {
        "aweme_id": aid,
        "desc": f"作品 {aid}",
        "author": {"nickname": "测试作者"},
        "video": {"duration": 1000},
    }


class _Packet:
    def __init__(self, body: dict, status: int = 200):
        self.response = SimpleNamespace(body=body, status=status)


class _Listener:
    def __init__(self, waits):
        self._waits = list(waits)
        self.started = ""

    def start(self, target):
        self.started = target

    def wait(self, timeout):
        del timeout
        return self._waits.pop(0) if self._waits else None


class _Element:
    def __init__(self, displayed: bool):
        self.states = SimpleNamespace(is_displayed=displayed)


class _Page:
    def __init__(self, waits, *, cookies=None, heights=None, captcha=None, url=None):
        self.listen = _Listener(waits)
        self._cookies = cookies if cookies is not None else {"sessionid": "secret"}
        self._heights = list(heights or [100])
        self._captcha = captcha
        self.url = url or "https://www.douyin.com/user/sec_uid"
        self.quit_called = False
        self.js_calls = 0
        self.got = ""

    def get(self, url):
        self.got = url

    def cookies(self, as_dict=False):
        if callable(self._cookies):
            value = self._cookies()
        else:
            value = self._cookies
        if as_dict:
            return dict(value)
        return [{"name": key, "value": val} for key, val in value.items()]

    def run_js(self, script):
        del script
        self.js_calls += 1
        if len(self._heights) > 1:
            return self._heights.pop(0)
        return self._heights[0]

    def eles(self, selector, timeout=None):
        del selector, timeout
        return [] if self._captcha is None else [self._captcha]

    def quit(self):
        self.quit_called = True


class TestDouyinFullCollectionReliability(unittest.TestCase):
    def collect(self, page, **kwargs):
        with (
            patch.object(douyin_browser, "resolve_sec_uid", return_value="sec_uid"),
            patch.object(douyin_browser, "_open_page", return_value=page),
            patch.object(douyin_browser.time, "sleep", return_value=None),
        ):
            return douyin_browser.collect_all(
                "https://www.douyin.com/user/input",
                **kwargs,
            )

    def test_successful_has_more_zero_is_the_only_normal_completion(self):
        page = _Page([
            _Packet({"status_code": 0, "has_more": 0, "aweme_list": [_aweme()]}),
            None,
        ])

        items, reason = self.collect(page)

        self.assertEqual([item.work_id for item in items], ["1001"])
        self.assertEqual(reason, "")
        self.assertEqual(page.js_calls, 2)
        self.assertTrue(page.quit_called)

    def test_hidden_verify_element_does_not_block_terminal_completion(self):
        page = _Page([
            _Packet({"status_code": 0, "has_more": False, "aweme_list": [_aweme()]}),
            None,
        ], captcha=_Element(False))

        items, reason = self.collect(page)

        self.assertEqual(len(items), 1)
        self.assertEqual(reason, "")

    def test_visible_verification_wins_over_terminal_and_preserves_items(self):
        page = _Page([
            _Packet({"status_code": 0, "has_more": 0, "aweme_list": [_aweme()]}),
            None,
        ], captcha=_Element(True))

        items, reason = self.collect(page)

        self.assertEqual(len(items), 1)
        self.assertIn("可见验证", reason)
        self.assertTrue(page.quit_called)

    def test_stale_login_marker_fails_fast_clears_only_marker_and_closes(self):
        page = _Page([], cookies={})
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            marker = profile / douyin_browser._LOGIN_MARK
            marker.write_text("ok", encoding="utf-8")
            preserved = profile / "Cookies"
            preserved.write_text("opaque", encoding="utf-8")
            with patch.object(douyin_browser, "_profile_dir", return_value=profile):
                with self.assertRaisesRegex(DouyinCollectError, "登录态丢失"):
                    self.collect(page)

            self.assertFalse(marker.exists())
            self.assertTrue(preserved.exists())
        self.assertEqual(page.js_calls, 0)
        self.assertTrue(page.quit_called)

    def test_idle_without_terminal_returns_partial_reason_instead_of_success(self):
        page = _Page([
            _Packet({"status_code": 0, "has_more": 1, "aweme_list": [_aweme()]}),
            None,
            None,
            None,
            None,
        ], heights=[100] * 8)

        items, reason = self.collect(page, idle_rounds_to_stop=3)

        self.assertEqual(len(items), 1)
        self.assertIn("未能确认已全部采集", reason)
        self.assertTrue(page.quit_called)

    def test_round_limit_returns_partial_reason_instead_of_success(self):
        page = _Page([
            _Packet({"status_code": 0, "has_more": 1, "aweme_list": [_aweme()]}),
            None,
        ])

        items, reason = self.collect(page, max_rounds=1)

        self.assertEqual(len(items), 1)
        self.assertIn("安全上限", reason)

    def test_cancel_during_continuous_packet_drain_returns_and_keeps_items(self):
        packet = _Packet({"status_code": 0, "has_more": 1, "aweme_list": [_aweme()]})
        page = _Page([packet] * 20)

        class _CancelAfter:
            calls = 0

            def __call__(self):
                self.calls += 1
                return self.calls >= 5

        items, reason = self.collect(page, is_cancelled=_CancelAfter())

        self.assertEqual(len(items), 1)
        self.assertIn("已取消", reason)
        self.assertTrue(page.quit_called)

    def test_incomplete_legacy_result_maps_to_partial_network_result(self):
        item_page = _Page([
            _Packet({"status_code": 0, "has_more": 1, "aweme_list": [_aweme()]}),
            None,
        ])
        items, reason = self.collect(item_page, max_rounds=1)
        adapter = DouyinAdapter(collect_all_fn=lambda *_args: (items, reason))
        request = CollectRequest(
            identity=LinkIdentity(
                platform="douyin",
                kind="homepage",
                canonical_url="https://www.douyin.com/user/sec_uid",
                extracted_id="sec_uid",
                confidence="exact",
            ),
            mode="profile_all",
        )

        result = adapter.collect(request, browser_mode="efficiency")

        self.assertEqual((result.completion, result.reason_code), ("partial", "network_error"))
        self.assertEqual(len(result.items), 1)
        self.assertIn("已保留", result.next_action)


if __name__ == "__main__":
    unittest.main()
