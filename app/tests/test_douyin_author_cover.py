from __future__ import annotations
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.core.collect.models import from_aweme
from src.core.audio.extractor import _douyin_cover_identity


class AuthorCoverTests(unittest.TestCase):
    def test_existing_transcript_cover_retry_refreshes_source_without_transcribing(self):
        from src.core.models import TranscriptCover
        from src.core.collect.cover import CoverReceipt
        from src.ui.main_window import _TranscriptCoverRetryWorker
        original = TranscriptCover(platform='douyin',work_id='100',url='https://p3.douyinpic.com/first.jpg',status='url_only').merge_meta({'title':'保留标题'})
        workflow = Mock()
        workflow.localize.return_value = CoverReceipt.failed('cover_timeout')
        worker = _TranscriptCoverRetryWorker('entry', original, workflow)
        results=[]
        worker.done.connect(lambda *args: results.append(args))
        with patch('src.core.collect.douyin_fetch.fetch_item_with_page_fallback',return_value=SimpleNamespace(
            work_id='100',cover_url='https://p3.douyinpic.com/author.jpg')), patch('src.core.pipeline.transcribe_source') as transcribe:
            worker.run()
        self.assertEqual(workflow.localize.call_args.kwargs['url'],'https://p3.douyinpic.com/author.jpg')
        self.assertEqual(results[0][1]['title'],'保留标题')
        transcribe.assert_not_called()

    def test_single_and_homepage_prefer_author_cover_over_first_frame(self):
        item = {'aweme_id':'7676371995923205403', 'video':{
            'cover':{'url_list':['https://p3.douyinpic.com/first-frame.jpg']},
            'origin_cover':{'url_list':['https://p3.douyinpic.com/first-frame-full.jpg']},
            'cover_original_scale':{'url_list':['https://p3.douyinpic.com/author-cover.jpg']},
        }}
        expected = 'https://p3.douyinpic.com/author-cover.jpg'
        self.assertEqual(from_aweme(item).cover_url, expected)
        self.assertEqual(_douyin_cover_identity([{'items':[item]}]), (item['aweme_id'], expected))

    def test_invalid_custom_cover_falls_back_to_valid_platform_image(self):
        item = {'aweme_id':'1', 'video':{
            'cover_original_scale':{'url_list':['file:///tmp/no.jpg']},
            'cover':{'url_list':['https://p3.douyinpic.com/platform.jpg']},
        }}
        self.assertEqual(from_aweme(item).cover_url, 'https://p3.douyinpic.com/platform.jpg')
        self.assertEqual(_douyin_cover_identity([item])[1], from_aweme(item).cover_url)
