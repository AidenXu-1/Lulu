import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src.core.collect.transcription_input import download_collected_input
from tests.test_douyin_single_link_fallback import _video_item


class CollectedTranscriptionInputTests(unittest.TestCase):
    def test_same_work_uses_collected_url_and_keeps_metadata(self):
        item = _video_item()
        repo = Mock()
        repo.load.return_value = SimpleNamespace(items=[item])
        with tempfile.TemporaryDirectory() as tmp, patch(
            'src.core.collect.transcription_input.download_douyin_video_cdn',
            return_value=Path(tmp)/'douyin-video.mp4'
        ) as download:
            meta = {}
            result = download_collected_input(item.stable_key, Path(tmp), lambda *_: None, meta, repository=repo)
            self.assertEqual(result, Path(tmp)/'douyin-video.mp4')
            self.assertEqual(download.call_args.kwargs['work_id'],item.work_id)
            self.assertEqual(download.call_args.args[0],item.play_url)
            self.assertEqual(meta['title'], item.title)
            self.assertEqual(meta['original_url'], item.work_url)

    def test_missing_ambiguous_or_untrusted_record_falls_back_without_download(self):
        item = _video_item()
        for items in ([], [item,item], [_video_item(play_url='https://example.invalid/a')]):
            repo=Mock()
            repo.load.return_value = SimpleNamespace(items=items)
            with patch('src.core.collect.transcription_input.download_douyin_video_cdn') as download:
                self.assertIsNone(download_collected_input(item.stable_key,Path('/unused'),lambda *_:None,{},repository=repo))
                download.assert_not_called()
