"""Explicit signed-in latest collection, with injected collectors and no user state."""

from unittest.mock import Mock, patch
import unittest

from src.core.collect.adapters.douyin import DouyinAdapter
from src.core.collect.application import CollectionApplicationService, CollectionCommand
from src.core.collect.contracts import CollectRequest, LinkIdentity, adapter_capability
from src.core.collect.douyin_fetch import DouyinCollectError
from src.core.collect.models import CollectedItem


URL = "https://www.douyin.com/user/MS4w.fixture-author"


def request(mode="profile_latest", *, limit=None, profile_id="default"):
    return CollectRequest(
        LinkIdentity("douyin", "homepage", URL, "MS4w.fixture-author", "exact"),
        mode=mode, limit=limit, profile_id=profile_id,
    )


def item(work_id="1"):
    return CollectedItem(work_id, source_method="profile_latest")


class DouyinProfileLatestAdapterTests(unittest.TestCase):

    def test_anonymous_failure_never_opens_a_signed_in_collector(self):
        fetch = Mock(side_effect=DouyinCollectError("denied", code="DOUYIN_ACCESS_RESTRICTED"))
        latest = Mock()
        full = Mock()
        result = DouyinAdapter(
            fetch_latest_fn=fetch, collect_all_fn=full, collect_latest_fn=latest,
        ).collect(request("anonymous_latest"))
        self.assertEqual((result.completion, result.reason_code), ("failed", "permission_denied"))
        fetch.assert_called_once()
        latest.assert_not_called()
        full.assert_not_called()

    def test_explicit_mode_forwards_callbacks_path_and_small_scope(self):
        work = item()
        latest = Mock(return_value=([work], ""))
        anonymous = Mock()
        full = Mock()
        progress = Mock()
        cancelled = Mock(return_value=False)
        result = DouyinAdapter(
            fetch_latest_fn=anonymous, collect_all_fn=full, collect_latest_fn=latest,
        ).collect(request(limit=3), progress=progress, is_cancelled=cancelled,
                  browser_path="/isolated/browser", browser_mode="unused-for-latest")
        latest.assert_called_once_with(
            URL, limit=3, progress=progress, is_cancelled=cancelled,
            browser_path="/isolated/browser",
        )
        anonymous.assert_not_called()
        full.assert_not_called()
        self.assertEqual(result.completion, "complete")
        self.assertIs(result.items[0], work)
        self.assertEqual(result.items[0].source_method, "profile_latest")



    def test_cancel_before_call_and_nondefault_profile_never_open_browser(self):
        for req, cancelled, expected in (
            (request(), lambda: True, ("cancelled", "cancelled")),
            (request(profile_id="other"), lambda: False, ("failed", "invalid_input")),
        ):
            latest = Mock()
            result = DouyinAdapter(collect_latest_fn=latest).collect(req, is_cancelled=cancelled)
            self.assertEqual((result.completion, result.reason_code), expected)
            latest.assert_not_called()












if __name__ == "__main__":
    unittest.main()
