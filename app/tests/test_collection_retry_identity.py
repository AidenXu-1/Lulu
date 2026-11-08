"""Public author identity survives failures; unknown short links remain usable."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from src.core.collect import douyin_fetch as fetch
from src.core.collect.adapters.douyin import DouyinAdapter
from src.core.collect.application import CollectionApplicationService, CollectionCommand
from src.core.collect.contracts import CollectRequest
from src.core.collect.contracts import CollectResult
from src.core.collect.models import CollectedItem
from src.core.collect.router import detect_link
from src.ui.collection_page import CollectionPage

SHORT = 'https://v.douyin.com/fixture-short/'
AUTHOR = 'MS4w_fixture-author'
URL = 'https://www.douyin.com/user/' + AUTHOR


class RetryAuthorIdentityTests(unittest.TestCase):
    def test_failure_after_resolving_author_preserves_that_author(self):
        with patch.object(fetch, 'resolve_sec_uid', return_value=AUTHOR), \
                patch.object(fetch, '_register_ttwid', side_effect=fetch.DouyinCollectError('denied', code='DOUYIN_ACCESS_RESTRICTED')):
            with self.assertRaises(fetch.DouyinCollectError) as caught:
                fetch.fetch_latest(SHORT)
        self.assertEqual(caught.exception.resolved_author_id, AUTHOR)

    def test_adapter_carries_valid_author_out_of_anonymous_failure(self):
        error = fetch.DouyinCollectError('denied', code='DOUYIN_ACCESS_RESTRICTED')
        error.resolved_author_id = AUTHOR
        adapter = DouyinAdapter(fetch_latest_fn=Mock(side_effect=error))
        result = adapter.collect(CollectRequest(detect_link(SHORT), 'anonymous_latest', limit=3))
        self.assertEqual(result.resolved_author_id, AUTHOR)
        self.assertEqual(result.reason_code, 'permission_denied')

    def service(self):
        latest = Mock(return_value=([CollectedItem('123', author_id=AUTHOR)], ''))
        service = CollectionApplicationService(adapter_resolver=lambda _: DouyinAdapter(collect_latest_fn=latest))
        return service, latest

    def test_known_author_is_frozen_without_reexpanding_short_link_and_keeps_raw_input(self):
        service, latest = self.service()
        command = CollectionCommand(SHORT, 'profile_latest', limit=3, resolved_author_id=AUTHOR)
        result = service.collect(command)
        self.assertEqual(result.completion, 'complete')
        self.assertEqual(command.raw_input, SHORT)
        self.assertEqual(latest.call_args.args[0], URL)
        self.assertEqual(latest.call_args.kwargs['limit'], 3)

    def test_conflicting_long_url_and_frozen_author_are_rejected(self):
        service, latest = self.service()
        result = service.collect(CollectionCommand('https://www.douyin.com/user/MS4w_other',
            'profile_latest', limit=3, resolved_author_id=AUTHOR))
        self.assertEqual(result.reason_code, 'invalid_input')
        self.assertIn('原作者', result.next_action)
        latest.assert_not_called()

    def test_unknown_author_before_403_still_uses_explicit_browser_resolution(self):
        service, latest = self.service()
        result = service.collect(CollectionCommand(SHORT, 'profile_latest', limit=3))
        self.assertEqual(result.completion, 'complete')
        self.assertEqual(latest.call_args.args[0], detect_link(SHORT).canonical_url)

    def test_malformed_author_metadata_cannot_become_a_retry_target(self):
        service, latest = self.service()
        result = service.collect(CollectionCommand(SHORT, 'profile_latest', resolved_author_id='https://invalid.example/'))
        self.assertEqual(result.reason_code, 'invalid_input')
        latest.assert_not_called()

    def test_existing_frozen_author_is_not_replaced_by_later_metadata(self):
        from types import SimpleNamespace
        owner = SimpleNamespace(_sync_partial_login_retry=Mock())
        command = CollectionCommand(SHORT, 'profile_latest', limit=3, resolved_author_id=AUTHOR)
        result = replace(CollectResult.failed('permission_denied'), resolved_author_id='MS4w_other')
        CollectionPage._offer_partial_login_retry(owner, 7, command, result)
        self.assertEqual(owner._partial_login_retry[1].resolved_author_id, AUTHOR)

    def test_frozen_target_rejects_other_author_from_real_browser_parser(self):
        from src.core.collect import douyin_browser as browser
        from tests.test_douyin_profile_latest_browser import Page, packet
        body = {'status_code': 0, 'aweme_list': [{'aweme_id': '123', 'author': {'sec_uid': 'MS4w_other'}}]}
        page = Page([packet(author=AUTHOR, body=body)])
        with patch.object(browser, '_open_page', return_value=page), \
                patch.object(browser, '_scroll_bottom', side_effect=AssertionError('must not scroll')), \
                patch.object(fetch.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('must not expand short link')):
            result = CollectionApplicationService().collect(CollectionCommand(
                SHORT, 'profile_latest', limit=3, resolved_author_id=AUTHOR))
        self.assertEqual(result.reason_code, 'platform_changed')
        self.assertFalse(result.items)
        self.assertTrue(page.closed)
