"""高清候选必须属于同一作者封面；下载后按真实静态图片尺寸选择。"""
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage

from src.core.collect.cover import CoverLocalizationService
from src.core.collect.models import from_aweme
from tests.test_collection_p3c_cover import FakePlan, FakeResolver, FakeTransport


URI = 'tos-cn-i-dy/8c46318696e345c1bf6f78f33865dcef'
SMALL = f'https://p3.douyinpic.com/{URI}~tplv-dy-360p.jpeg?sign=keep%2Bexact'
FULL = f'https://p3.douyinpic.com/obj/{URI}?sign=also%2Bkeep'


def aweme(uri=URI, full=FULL):
    return {'aweme_id': '7675669869509070115', 'video': {
        'cover_original_scale': {'uri': URI, 'url_list': [SMALL]},
        'dynamic_cover': {'uri': uri, 'url_list': [full], 'width': 9999, 'height': 9999},
        'origin_cover': {'uri': 'other/frame', 'url_list': ['https://p3.douyinpic.com/other/frame']},
    }}


def image(width, height, fmt='JPEG'):
    value = QImage(width, height, QImage.Format_RGB32)
    value.fill(0xFF61819A)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    assert value.save(buffer, fmt)
    return bytes(data)


class DouyinHDCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'covers'
        self.small = image(360, 481)
        self.full = image(959, 1280, 'WEBP')

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *plans):
        return CoverLocalizationService(self.root, transport=FakeTransport(*plans),
                                        resolver=FakeResolver())

    def download(self, service, item=None, **extra):
        item = item or from_aweme(aweme())
        return service.localize(platform='douyin', work_id=item.work_id,
                                url=item.cover_url, variants=item.cover_variants, **extra)

    def test_same_uri_preserves_author_cover_and_signed_urls(self):
        item = from_aweme(aweme())
        self.assertEqual(item.cover_url, SMALL)
        self.assertEqual(item.cover_variants, (FULL,))
        crowded = aweme()
        for key in ('cover', 'origin_cover'):
            crowded['video'][key] = {'uri': URI, 'url_list': [
                f'https://p3.douyinpic.com/{URI}~tplv-{key}-{i}.jpeg' for i in range(4)]}
        variants = from_aweme(crowded).cover_variants
        self.assertEqual(variants[0], FULL)
        self.assertLessEqual(len(variants), 4)

    def test_other_identity_missing_uri_and_malformed_fields_add_no_variants(self):
        for detail in (aweme('other/frame'), aweme('', FULL), aweme(URI, FULL.replace(URI, 'other/frame')),
                       {'video': {'cover_original_scale': None, 'dynamic_cover': 'bad'}}, {}):
            with self.subTest(detail=detail):
                self.assertEqual(from_aweme(detail).cover_variants, ())

    def test_download_chooses_actual_larger_static_webp_without_changing_urls(self):
        service = self.service(FakePlan(body=self.small), FakePlan(body=self.full, mime='image/webp'))
        receipt = self.download(service)
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual(receipt.mime_type, 'image/webp')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.full)
        self.assertEqual([call[0].url for call in service.transport.calls], [SMALL, FULL])

    def test_smaller_changed_composition_unsupported_animation_and_failure_keep_original(self):
        animated = b'RIFF' + (16).to_bytes(4, 'little') + b'WEBPANIM' + (4).to_bytes(4, 'little') + b'\0' * 4
        plans = [FakePlan(body=image(180, 240)), FakePlan(body=image(1280, 720)),
                 FakePlan(body=animated, mime='image/webp'), FakePlan(body=b'GIF89a', mime='image/gif'),
                 FakePlan(status=403), FakePlan(request_error=TimeoutError())]
        for plan in plans:
            with self.subTest(plan=plan):
                service = self.service(FakePlan(body=self.small), plan)
                receipt = self.download(service)
                self.assertEqual(receipt.status, 'downloaded')
                self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.small)

    def test_no_high_resolution_candidate_preserves_existing_download(self):
        item = from_aweme(aweme('different/image'))
        service = self.service(FakePlan(body=self.small))
        receipt = self.download(service, item)
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual(len(service.transport.calls), 1)

    def test_failed_thumbnail_can_use_verified_full_image(self):
        service = self.service(FakePlan(status=403), FakePlan(body=self.full, mime='image/webp'))
        receipt = self.download(service)
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.full)

    def test_failed_first_cdn_does_not_stop_on_another_low_resolution_cdn(self):
        alternate = SMALL.replace('p3.', 'p6.')
        item = replace(from_aweme(aweme()), cover_variants=(alternate, FULL))
        service = self.service(FakePlan(status=403), FakePlan(body=self.small),
                               FakePlan(body=self.full, mime='image/webp'))
        receipt = self.download(service, item)
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.full)

    def test_active_redownload_replaces_only_managed_cache_and_failure_keeps_file(self):
        service = self.service(FakePlan(body=self.small), FakePlan(body=self.small),
                               FakePlan(body=self.full, mime='image/webp'), FakePlan(status=403), FakePlan(status=403))
        initial = service.localize(platform='douyin', work_id='7675669869509070115', url=SMALL)
        old = self.root / initial.relative_path
        self.assertEqual(old.read_bytes(), self.small)
        upgraded = self.download(service)
        self.assertEqual((self.root / upgraded.relative_path).read_bytes(), self.full)
        self.assertFalse(old.exists())
        failed = self.download(service)
        self.assertEqual(failed.status, 'failed')
        self.assertEqual((self.root / upgraded.relative_path).read_bytes(), self.full)

    def test_single_link_and_homepage_pass_the_same_variants_to_shared_localizer(self):
        from src.core import pipeline
        from src.core.audio.extractor import _douyin_work_facts
        from src.core.models import TranscriptCover
        from src.core.collect.contracts import CollectResult
        from src.core.collect.cover_workflow import CoverWorkflowService
        from src.core.collect.preferences import CollectCoverPreferenceSnapshot
        from unittest.mock import Mock
        item = from_aweme(aweme())
        self.assertEqual(_douyin_work_facts([aweme()])['cover_variants'], item.cover_variants)
        localizer = Mock(startup_recovery_receipts=())
        CoverWorkflowService(localizer).run(CollectResult.complete((item,)),
                                          snapshot=CollectCoverPreferenceSnapshot(True))
        home = localizer.localize.call_args.kwargs['variants']
        meta = TranscriptCover(platform='douyin', work_id=item.work_id, url=item.cover_url,
                               variants=item.cover_variants).merge_meta({})
        pipeline._settle_single_link_cover(meta, snapshot=CollectCoverPreferenceSnapshot(True),
                                          workflow=localizer, is_cancelled=lambda: False,
                                          progress=lambda *_: None)
        self.assertEqual(localizer.localize.call_args.kwargs['variants'], home)

    def test_json_roundtrip_preserves_candidates_and_old_records_still_load(self):
        from src.core.collect.repository import CollectionRepository
        from src.core.models import TranscriptCover
        item = from_aweme(aweme())
        repo = CollectionRepository(self.root.parent / 'collection' / 'records.json')
        repo.upsert([item])
        self.assertEqual(repo.load().items[0].cover_variants, (FULL,))
        meta = TranscriptCover(platform='douyin', work_id=item.work_id, url=SMALL,
                               variants=(FULL,)).merge_meta({})
        self.assertEqual(TranscriptCover.from_meta(json.loads(json.dumps(meta))).variants, (FULL,))
        old = item.to_dict()
        del old['cover_variants']
        self.assertEqual(type(item)(**old).cover_variants, ())

    def test_cli_and_gui_asset_adapters_download_the_same_real_bytes(self):
        from src.core import extraction, link_assets
        from src.core.models import TranscriptCover
        item = from_aweme(aweme())
        meta = TranscriptCover(platform='douyin', work_id=item.work_id, url=SMALL,
                               variants=(FULL,)).merge_meta({'title': '保留作者封面'})
        service = self.service(FakePlan(body=self.small), FakePlan(body=self.full, mime='image/webp'))
        def create_service(root, **options):
            return service if Path(root) == self.root else CoverLocalizationService(root, **options)
        with patch.object(extraction, 'metadata', return_value=meta), patch(
            'src.core.collect.cover.CoverLocalizationService', side_effect=create_service):
            files, errors = link_assets.acquire('https://www.douyin.com/video/' + item.work_id,
                ('cover', 'data'), self.root.parent, lambda *_: None, lambda: False, '2026-11-09')
        self.assertEqual(errors, {})
        self.assertEqual(files['cover'].read_bytes(), self.full)
        self.assertEqual(set(files), {'cover', 'data'})

    def test_single_link_metadata_keeps_same_work_candidates(self):
        from src.core import extraction
        from src.core.audio import extractor
        from src.core.models import TranscriptCover
        detail = aweme()
        detail['video']['play_addr'] = {'url_list': ['https://v26-web.douyinvod.com/video/test.mp4']}
        self.root.mkdir()
        meta = {}
        with patch.object(extractor, '_fetch_douyin_html', return_value=('', '')), patch.object(
            extractor, '_extract_embedded_json', return_value=[detail]):
            extraction._metadata_download('https://www.douyin.com/video/' + detail['aweme_id'],
                                           self.root, lambda *_: None, meta)
        self.assertEqual(TranscriptCover.from_meta(meta).variants, (FULL,))
        self.assertEqual(TranscriptCover.from_meta(meta).work_id, detail['aweme_id'])

    def test_animation_without_thumbnail_never_gets_committed(self):
        animated = b'RIFF' + (16).to_bytes(4, 'little') + b'WEBPANIM' + (4).to_bytes(4, 'little') + b'\0' * 4
        service = self.service(FakePlan(status=403), FakePlan(body=animated, mime='image/webp'))
        receipt = self.download(service)
        self.assertEqual(receipt.status, 'failed')
        self.assertFalse(any(self.root.rglob('*.webp')))

    def test_cancel_during_candidate_does_not_replace_existing_cover(self):
        cancelled = [False]
        service = self.service(FakePlan(body=self.small), FakePlan(body=self.small),
                               FakePlan(body=self.full, mime='image/webp',
                                        on_read=lambda: cancelled.__setitem__(0, True)))
        old = service.localize(platform='douyin', work_id=from_aweme(aweme()).work_id, url=SMALL)
        receipt = self.download(service, is_cancelled=lambda: cancelled[0])
        self.assertEqual(receipt.reason_code, 'cover_cancelled')
        self.assertEqual((self.root / old.relative_path).read_bytes(), self.small)

    def test_active_gui_retry_refreshes_anonymous_source_and_keeps_saved_cover_on_failure(self):
        from src.ui.collection_page import _CoverRetryWorker
        from src.core.collect.cover_workflow import CoverWorkflowService
        item = from_aweme(aweme())
        service = self.service(FakePlan(body=self.small), FakePlan(status=403), FakePlan(status=403))
        saved = service.localize(platform='douyin', work_id=item.work_id, url=SMALL)
        old = replace(item, cover_variants=(), cover_status='downloaded', cover_local_path=saved.relative_path)
        worker = _CoverRetryWorker(CoverWorkflowService(service), [old])
        results = []
        worker.done.connect(results.append)
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', return_value=item) as refresh:
            worker.run()
        refresh.assert_called_once()
        self.assertEqual(results[0].collect_result.items[0], old)
        self.assertEqual((self.root / saved.relative_path).read_bytes(), self.small)
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.ui.collection_page import CollectionPage
        page = SimpleNamespace(_items=[old], _records_repo=None, _render=Mock(), status=Mock())
        CollectionPage._on_cover_retry_done(page, results[0])
        summary = page.status.setText.call_args.args[0]
        self.assertIn('已保存 0 条', summary)
        self.assertIn('未完成 1 条', summary)
        self.assertIn('保留原封面 1 条', summary)

    def test_main_material_menu_exposes_explicit_hd_redownload(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.ui.collection_page import CollectionPage
        item = replace(from_aweme(aweme()), cover_status='downloaded', cover_local_path='douyin/test.jpg')
        page = SimpleNamespace(_items=[item], _managed_cover_file=lambda _: self.root / 'test.jpg',
                               _cover_workflow=object(), cursor=lambda: SimpleNamespace(pos=lambda: None),
                               _retry_cover_item=Mock())
        with patch('src.ui.collection_page.QMenu') as menu:
            reveal, refresh = object(), object()
            menu.return_value.addAction.side_effect = [reveal, refresh]
            menu.return_value.exec.return_value = refresh
            CollectionPage._reveal_material_cell(page, item.stable_key, 'cover')
        page._retry_cover_item.assert_called_once_with(item)
        self.assertTrue(CollectionPage._cover_retry_eligible(item))

    def test_missing_old_cached_file_refreshes_stored_thumbnail_source_on_click(self):
        from src.ui.collection_page import _MaterialDownloadWorker
        from src.core.collect.cover_workflow import CoverWorkflowService
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        item = from_aweme(aweme())
        old = replace(item, cover_variants=())
        service = self.service(FakePlan(body=self.small), FakePlan(body=self.full, mime='image/webp'))
        worker = _MaterialDownloadWorker(None, CoverWorkflowService(service), [old],
            MaterialPreferenceSnapshot('video', False, True), origin='homepage_history_material')
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback', return_value=item) as refresh:
            result = worker._download_covers((old,))
        refresh.assert_called_once()
        receipt = result.outcomes[0]
        self.assertEqual(receipt.status, 'downloaded')
        self.assertEqual((self.root / receipt.relative_path).read_bytes(), self.full)
