"""Cover fallback and old homepage batch stability; no network or user data."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core.collect import cover as cover_module
from src.core.collect.cover import CoverLocalizationService
from src.core.collect.cover_workflow import CoverWorkflowService
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.ui.collection_page import _MaterialDownloadWorker
from tests.test_douyin_hd_cover import aweme, from_aweme, image, SMALL, FULL
from tests.test_collection_p3c_cover import FakePlan, FakeResolver, FakeTransport


class _ImageTransport:
    def __init__(self, small, full):
        self.small, self.full, self.calls = small, full, []

    def request(self, target, **kwargs):
        self.calls.append(target.url)
        plan = FakePlan(body=self.full, mime='image/webp') if target.url == FULL else FakePlan(body=self.small)
        return FakeTransport(plan).request(target, **kwargs)


class CoverStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'covers'
        self.small, self.full = image(360, 481), image(959, 1280, 'WEBP')
        self.first = from_aweme(aweme())
        self.second = replace(self.first, aweme_id='2', work_id='2', work_url='', title='保留第二条标题')

    def tearDown(self):
        self.temp.cleanup()

    def service(self, transport=None, **options):
        return CoverLocalizationService(self.root, resolver=FakeResolver(),
            transport=transport or _ImageTransport(self.small, self.full), **options)

    def worker(self, service, items, save_media=False):
        return _MaterialDownloadWorker(None, CoverWorkflowService(service), items,
            MaterialPreferenceSnapshot('video', save_media, True), origin='homepage_history_material')

    def test_exhausted_optional_hd_budget_keeps_validated_small_image(self):
        clock = [0.0]
        def stall(*_):
            clock[0] = 31.0
        service = self.service(transport=FakeTransport(FakePlan(body=self.small),
            FakePlan(request_error=TimeoutError(), on_request=stall)), clock=lambda: clock[0])
        receipt = service.localize(platform='douyin', work_id=self.first.work_id, url=SMALL, variants=(FULL,))
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.small)

    def test_slow_post_download_check_keeps_validated_larger_image(self):
        clock, calls = [0.0], [0]
        original = cover_module._static_image_size
        def delayed(payload):
            calls[0] += 1
            result = original(payload)
            if calls[0] == 3:
                clock[0] = 31.0
            return result
        service = self.service(clock=lambda: clock[0])
        with patch.object(cover_module, '_static_image_size', side_effect=delayed):
            receipt = service.localize(platform='douyin', work_id=self.first.work_id, url=SMALL, variants=(FULL,))
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.full)

    def test_old_batch_refreshes_each_exact_work_and_saves_large_images(self):
        service = self.service()
        items = tuple(replace(item, cover_variants=()) for item in (self.first, self.second))
        details = {item.work_id: item for item in (self.first, self.second)}
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback',
                   side_effect=lambda work, *_a, **_k: details[work]) as lookup:
            result = self.worker(service, items)._download_covers(items)
        self.assertEqual([call.args[0] for call in lookup.call_args_list], [item.work_id for item in items])
        self.assertEqual(result.collect_result.completion, 'complete')
        self.assertEqual(result.collect_result.items[1].title, self.second.title)
        for outcome in result.outcomes:
            self.assertEqual(outcome.status, 'downloaded')
            self.assertEqual((self.root / outcome.relative_path).read_bytes(), self.full)

    def test_one_missing_batch_source_does_not_block_another_work(self):
        service = self.service()
        first = replace(self.first, cover_url='', cover_variants=())
        second = replace(self.second, cover_variants=())
        def fresh(work, *_a, **_k):
            if work == first.work_id:
                raise TimeoutError()
            return self.second
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', side_effect=fresh):
            result = self.worker(service, (first, second))._download_covers((first, second))
        self.assertEqual(result.collect_result.completion, 'complete')
        self.assertEqual([outcome.status for outcome in result.outcomes], ['failed', 'downloaded'])
        self.assertEqual((self.root / result.outcomes[1].relative_path).read_bytes(), self.full)

    def test_old_record_combined_media_and_cover_refreshes_cover_source(self):
        service = self.service()
        old = replace(self.first, cover_variants=())
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', return_value=self.first) as lookup:
            result = self.worker(service, [old], save_media=True)._download_covers((old,))
        lookup.assert_called_once()
        self.assertEqual((self.root / result.outcomes[0].relative_path).read_bytes(), self.full)

    def test_recent_batch_with_candidates_avoids_extra_detail_requests(self):
        service = self.service()
        items = (self.first, self.second)
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback') as lookup:
            result = self.worker(service, items)._download_covers(items)
        lookup.assert_not_called()
        self.assertTrue(all(outcome.status == 'downloaded' for outcome in result.outcomes))

    def test_failed_source_refresh_keeps_usable_saved_urls_for_old_batch(self):
        service = self.service()
        items = tuple(replace(item, cover_variants=()) for item in (self.first, self.second))
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', side_effect=TimeoutError()):
            result = self.worker(service, items)._download_covers(items)
        for outcome in result.outcomes:
            self.assertEqual(outcome.status, 'downloaded')
            self.assertEqual((self.root / outcome.relative_path).read_bytes(), self.small)

    def test_cancelled_batch_starts_no_detail_or_image_request(self):
        service = self.service()
        items = (replace(self.first, cover_variants=()), replace(self.second, cover_variants=()))
        worker = self.worker(service, items)
        worker.request_cancel()
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback') as lookup:
            result = worker._download_covers(items)
        lookup.assert_not_called()
        self.assertEqual(service.transport.calls, [])
        self.assertTrue(all(outcome.reason_code == 'cover_cancelled' for outcome in result.outcomes))

    def test_other_platform_cover_flow_does_not_refresh_douyin(self):
        from src.core.collect.models import CollectedItem
        service = self.service()
        item = CollectedItem('', platform='xiaohongshu', work_id='note', cover_url=SMALL)
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback') as lookup:
            result = self.worker(service, [item])._download_covers((item,))
        lookup.assert_not_called()
        self.assertEqual(result.outcomes[0].status, 'downloaded')


if __name__ == '__main__':
    unittest.main()
