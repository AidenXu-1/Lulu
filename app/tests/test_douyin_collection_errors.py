"""Anonymous collection errors retain their cause without exposing request data.

All requests are injected failures. The UI callback uses a small in-memory page
double, so these checks create no application, browser, configuration or files.
"""

from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from src.core.collect.adapters.douyin import DouyinAdapter
from src.core.collect.contracts import CollectRequest, CollectResult, LinkIdentity
from src.core.collect.douyin_fetch import DouyinCollectError
from src.ui.collection_page import CollectionPage


def request(mode="anonymous_latest"):
    return CollectRequest(
        LinkIdentity("douyin", "homepage", "https://www.douyin.com/user/MS4w.test",
                     "MS4w.test", "exact"),
        mode=mode,
    )


def failed_collection(error, mode="anonymous_latest"):
    def fail(*_args):
        raise error
    adapter = DouyinAdapter(fetch_latest_fn=fail, collect_all_fn=fail)
    return adapter.collect(request(mode))


def displayed_message(result, mode="partial", platform="douyin"):
    page = SimpleNamespace(
        _run_token=1,
        _active_collection_mode=mode,
        _persist_records_result=lambda _result: ("", False),
        _persist_history_result=lambda _result, _token: ("", False),
        _material_snapshots={},
        _cover_workflow=None,
        _records_repo=None,
        _cover_status_text=lambda *_args, **_kwargs: "",
        _result_suffix=CollectionPage._result_suffix,
        _current_platform=lambda: platform,
        _has_unknown_fields=lambda _items: False,
        status=Mock(),
    )
    page._finish_collect = lambda: setattr(page, "_active_collection_mode", "")
    CollectionPage._on_collect_done(page, result, 1)
    return page.status.setText.call_args.args[0]


class DouyinCollectionErrorTests(unittest.TestCase):



    def test_rejected_anonymous_access_is_not_presented_as_network_or_privacy(self):
        result = failed_collection(DouyinCollectError("403", code="DOUYIN_ACCESS_RESTRICTED"))
        message = displayed_message(result)
        self.assertIn("抖音拒绝了本次匿名访问", message)
        self.assertIn("全量采集", message)
        self.assertNotIn("公开范围", message)
        self.assertNotIn("检查网络", message)
        self.assertNotIn("页面读取失败", message)



    def test_real_network_failure_asks_to_check_connection(self):
        result = failed_collection(DouyinCollectError("timeout", code="DOUYIN_NETWORK_ERROR"))
        self.assertIn("检查网络", displayed_message(result))

    def test_service_failure_does_not_blame_users_connection(self):
        result = failed_collection(DouyinCollectError("HTTP 503 Cookie=synthetic", code="DOUYIN_SERVICE_ERROR"))
        self.assertEqual(result.reason_code, "network_error")
        self.assertEqual(result.next_action, "抖音服务暂时无法返回作品，请稍后重试。")
        self.assertEqual(displayed_message(result), result.next_action)
        self.assertNotIn("检查网络", displayed_message(result))




if __name__ == "__main__":
    unittest.main()
