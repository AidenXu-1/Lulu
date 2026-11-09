import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.core.collect.transcription_input import download_collected_input
from tests.test_douyin_single_link_fallback import _video_item
from src.core.collect.models import CollectedItem, from_aweme


class CollectedTranscriptionInputTests(unittest.TestCase):
    def test_article_music_is_not_accepted_as_author_narration(self):
        item=_video_item(aweme_type=163,duration_ms=0,content_kind='video',
            play_url='https://sf6-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/backgroundMusic')
        self.assertTrue(item.is_image)
        repo=Mock();repo.load.return_value=SimpleNamespace(items=[item])
        with patch('src.core.collect.transcription_input.download_douyin_video_cdn') as download:
            self.assertIsNone(download_collected_input(item.stable_key,Path('/unused'),lambda *_:None,{},repository=repo))
        download.assert_not_called()

    def test_real_article_shape_is_classified_before_making_transcription_candidates(self):
        item=from_aweme({'aweme_id':'7702488065255705856','aweme_type':163,
            'desc':'文章标题','article_info':{'article_title':'文章标题','article_content':'文章正文'},
            'video':{'duration':0,'play_addr':{'url_list':['https://sf6-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/backgroundMusic']}}})
        self.assertTrue(item.is_image)
        self.assertEqual(item.content_kind,'image')
        self.assertEqual(item.title,'文章标题')
        self.assertEqual(item.duration_text,'图文')

    def test_video_and_other_platform_type_numbers_are_preserved(self):
        self.assertFalse(_video_item().is_image)
        other=CollectedItem(aweme_id='',platform='xiaohongshu',work_id='xhs1',aweme_type=163,content_kind='video')
        self.assertFalse(other.is_image)


