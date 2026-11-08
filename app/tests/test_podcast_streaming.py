"""Large public feeds should only read enough to obtain the requested episodes."""
from io import BytesIO
import unittest
from unittest.mock import patch

from src.core.podcast import fetch_feed


class Response(BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.bytes_read = 0

    def read(self, size=-1):
        value = super().read(size)
        self.bytes_read += len(value)
        return value


class PodcastStreamingTests(unittest.TestCase):
    def fetch(self, raw, **kwargs):
        response = Response(raw)
        with patch('src.core.podcast.urllib.request.urlopen', return_value=response):
            result = fetch_feed('https://example.invalid/rss', **kwargs)
        self.assertTrue(response.closed)
        return result, response.bytes_read

    def test_large_feed_returns_requested_prefix_without_downloading_tail(self):
        items = ''.join(f'<item><title>第 {i} 集</title><enclosure url="https://example.invalid/{i}.mp3" type="audio/mpeg"/></item>' for i in range(4))
        raw = ('<rss><channel><title>长节目</title>' + items).encode() + b' ' * (9 * 1024 * 1024) + b'</channel></rss>'
        feed, consumed = self.fetch(raw, limit=3)
        self.assertIsNotNone(feed)
        self.assertEqual(feed.title, '长节目')
        self.assertEqual([e.title for e in feed.episodes], ['第 0 集', '第 1 集', '第 2 集'])
        self.assertLessEqual(consumed, 128 * 1024)

    def test_empty_or_invalid_feed_is_not_saved_as_success(self):
        for raw in (b'<html>not RSS</html>', b'<rss><channel></channel></rss>', b'<rss><channel><item>'):
            with self.subTest(raw=raw):
                self.assertIsNone(self.fetch(raw)[0])

    def test_input_budget_remains_bounded_without_usable_episodes(self):
        raw = b'<rss><channel><description>' + b'x' * (9 * 1024 * 1024)
        feed, consumed = self.fetch(raw, limit=3)
        self.assertIsNone(feed)
        self.assertLessEqual(consumed, 8 * 1024 * 1024 + 1)

    def test_skips_non_audio_and_handles_unusable_optional_duration(self):
        raw = b'''<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0/"><channel><title>test</title>
          <item><enclosure url="https://example.invalid/video" type="video/mp4"/></item>
          <item><enclosure url="   " type="audio/mpeg"/></item>
          <item><title>valid</title><enclosure url="https://example.invalid/audio" type="audio/mpeg"/><itunes:duration>inf</itunes:duration></item>
          </channel></rss>'''
        feed, _ = self.fetch(raw, limit=2)
        self.assertEqual(len(feed.episodes), 1)
        self.assertEqual(feed.episodes[0].title, 'valid')
        self.assertEqual(feed.episodes[0].duration, '')
