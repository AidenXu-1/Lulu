"""Real yt-dlp parser with synthetic pages; no network, profiles or credentials."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import yt_dlp
from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
from src.core.audio.xiaohongshu import LuluXiaoHongShuIE
from src.core.audio import extractor

ID = '6a9a942a000000001203d971'
URL = f'https://www.xiaohongshu.com/explore/{ID}?xsec_token=fixture&xsec_source=pc_feed'
NOTE = {'noteId': ID, 'type': 'video', 'title': '视频标题', 'desc': '说明',
        'user': {'userId': 'author', 'nickName': '作者'},
        'imageList': [{'url': 'https://sns-webpic.xhscdn.com/cover.jpg'}],
        'video': {'media': {'stream': {'h264': [{'masterUrl': 'https://sns-video.xhscdn.com/video.mp4',
                  'duration': 455000, 'videoCodec': 'h264', 'audioCodec': 'aac', 'width': 720, 'height': 1280}]}}}}

class XiaohongshuPublicVideoTests(unittest.TestCase):
    def extract(self, state):
        html = '<script>window.__INITIAL_STATE__=' + json.dumps(state) + '</script>'
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            ie = LuluXiaoHongShuIE(ydl)
            with patch.object(XiaoHongShuIE, '_download_webpage', return_value=html) as page:
                result = ie._real_extract(URL)
            self.assertIn('iPhone', page.call_args.kwargs['headers']['User-Agent'])
            self.assertEqual(page.call_args.args[0], URL)
            return result

    def test_mobile_video_reuses_upstream_formats_and_metadata(self):
        result = self.extract({'noteData': {'data': {'noteData': copy.deepcopy(NOTE)}}})
        self.assertEqual(result['id'], ID)
        self.assertEqual(result['title'], '视频标题')
        self.assertEqual(result['uploader'], '作者')
        self.assertEqual(result['formats'][0]['duration'], 455)
        self.assertEqual(result['duration'], 455)
        self.assertEqual(result['formats'][0]['url'], 'https://sns-video.xhscdn.com/video.mp4')
        self.assertEqual(result['thumbnails'][0]['url'], 'https://sns-webpic.xhscdn.com/cover.jpg')

    def test_existing_desktop_structure_still_works(self):
        self.assertTrue(self.extract({'note': {'noteDetailMap': {ID: {'note': copy.deepcopy(NOTE)}}}})['formats'])

    def test_counts_follow_target_note_into_existing_display_without_more_requests(self):
        from src.core import obsidian
        note = copy.deepcopy(NOTE)
        note['interactInfo'] = {'likedCount': '335', 'collectedCount': '380',
                                'commentCount': '24', 'shareCount': '63'}
        state = {'noteData': {'data': {'noteData': note}},
                 'recommendations': [{'interactInfo': {'likedCount': '99999'}}]}
        info = self.extract(state)
        info.update(extractor_key='XiaoHongShu', webpage_url=URL)
        with patch('urllib.request.urlopen', side_effect=AssertionError('extra request')) as fetch:
            meta = {}
            extractor._fill_meta(meta, info, URL)
        fetch.assert_not_called()
        self.assertEqual(meta['stats'], {'like': 335, 'favorite': 380, 'comment': 24, 'share': 63})
        self.assertEqual(meta['author'], {'name': '作者'})
        self.assertEqual({key: value for key, _, value in obsidian.stat_items(meta)},
                         {'like': '335', 'favorite': '380', 'comment': '24', 'share': '63'})
        self.assertEqual(meta['cover_work_id'], ID)

    def test_absent_or_malformed_optional_counts_never_break_video(self):
        for stats in (None, [], 'bad', {'likedCount': True, 'collectedCount': '-1',
                                      'commentCount': '1万+', 'shareCount': 2.5}):
            with self.subTest(stats=stats):
                note = copy.deepcopy(NOTE)
                note['interactInfo'] = stats
                info = self.extract({'noteData': {'data': {'noteData': note}}})
                self.assertTrue(info['formats'])
                self.assertFalse(any(key in info for key in ('like_count', 'save_count', 'comment_count', 'repost_count')))

    def test_zero_counts_are_kept_and_unrelated_counts_are_ignored(self):
        note = copy.deepcopy(NOTE)
        note['interactInfo'] = {'likedCount': 0, 'collectedCount': '0',
                                'commentCount': 24, 'shareCount': ' 63 '}
        other = copy.deepcopy(NOTE)
        other['noteId'] = 'other'
        other['interactInfo'] = {'likedCount': '99999'}
        info = self.extract({'note': {'noteDetailMap': {ID: {'note': note}, 'other': {'note': other}}}})
        self.assertEqual((info['like_count'], info['save_count'], info['comment_count'], info['repost_count']),
                         (0, 0, 24, 63))

    def test_counts_survive_saved_document_and_native_card(self):
        from PySide6.QtWidgets import QApplication, QLabel
        from src.core import library, obsidian
        from src.core.models import Transcript
        from src.core.task import Task, TaskState
        from src.ui.widgets import StatRow
        app = QApplication.instance() or QApplication([])
        note = copy.deepcopy(NOTE)
        note['interactInfo'] = {'likedCount': '335', 'collectedCount': '380',
                                'commentCount': '24', 'shareCount': '63'}
        info = self.extract({'noteData': {'data': {'noteData': note}}})
        meta = {}
        extractor._fill_meta(meta, info, URL)
        transcript = Transcript(text='这是逐字稿。', title='视频标题', source=URL, meta=meta)
        document = obsidian.note_body(transcript, '视频标题')
        for fragment in ('点赞 335', '收藏 380', '评论 24', '分享 63'):
            self.assertIn(fragment, document)
        with tempfile.TemporaryDirectory() as tmp, patch.object(library, 'LIBRARY_DIR', Path(tmp)):
            entry = library.save(transcript, '视频标题')
            library.save_markdown(entry, document, preserve_timestamps=True)
            restored = library.load(entry)
        self.assertEqual(restored.meta['stats'], meta['stats'])
        stats = StatRow()
        try:
            self.assertTrue(stats.set_meta(restored.meta))
            app.processEvents()
            labels = [label.text() for label in stats.findChildren(QLabel)]
            for value in ('335', '380', '24', '63'):
                self.assertIn(value, labels)
        finally:
            stats.close()

    def test_login_shell_is_not_labelled_as_image_post(self):
        with self.assertRaisesRegex(Exception, '未取得作品资料') as caught:
            self.extract({'login': {}, 'note': {'noteDetailMap': {}}})
        self.assertNotIn('图文', str(caught.exception))

    def test_wrong_work_never_uses_related_video(self):
        note = copy.deepcopy(NOTE); note['noteId'] = 'other'
        with self.assertRaisesRegex(Exception, '身份不一致'):
            self.extract({'noteData': {'data': {'noteData': note}}})

    def test_explicit_image_post_has_specific_message(self):
        note = copy.deepcopy(NOTE); note['type'] = 'normal'; note.pop('video')
        with self.assertRaisesRegex(Exception, '图文笔记'):
            self.extract({'noteData': {'data': {'noteData': note}}})

    def test_video_without_formats_is_not_an_image_post(self):
        note = copy.deepcopy(NOTE); note.pop('video')
        with self.assertRaisesRegex(Exception, '未取得视频地址'):
            self.extract({'noteData': {'data': {'noteData': note}}})

    def test_adapter_replaces_only_xhs_extractor(self):
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            before = ydl.get_info_extractor('Youtube')
            ydl.add_info_extractor(LuluXiaoHongShuIE())
            self.assertIsInstance(ydl.get_info_extractor('XiaoHongShu'), LuluXiaoHongShuIE)
            self.assertIs(ydl.get_info_extractor('Youtube'), before)

    def check_selected_format(self, mode, expected):
        real_ydl = yt_dlp.YoutubeDL
        chosen = []
        with tempfile.TemporaryDirectory() as tmp:
            def create(options):
                ydl = real_ydl(options)
                def extract(url, download=True):
                    self.assertIsInstance(ydl.get_info_extractor('XiaoHongShu'), LuluXiaoHongShuIE)
                    info = ydl.process_ie_result({
                        'id': ID, 'title': 'fixture', 'formats': [
                            {'format_id': 'playback', 'url': 'https://sns-video.xhscdn.com/playback.mp4',
                             'vcodec': 'h264', 'acodec': 'aac', 'filesize': 20_000_000},
                            {'format_id': 'direct', 'url': 'https://sns-video.xhscdn.com/original.mp4',
                             'vcodec': 'h264', 'acodec': 'aac', 'quality': 1, 'filesize': 900_000_000},
                        ]}, download=False)
                    chosen.append(info['format_id'])
                    path = Path(tmp) / 'media.mp4'; path.write_bytes(b'fixture')
                    info['filepath'] = str(path)
                    return info
                ydl.extract_info = extract
                return ydl
            with patch.object(yt_dlp, 'YoutubeDL', side_effect=create):
                extractor._download_via_ytdlp(URL, Path(tmp), lambda *_: None, link_media_mode=mode)
        self.assertEqual(chosen, [expected])

    def test_audio_uses_playback_instead_of_large_original(self):
        self.check_selected_format('audio', 'playback')

    def test_video_preserves_original_quality_selection(self):
        self.check_selected_format('video', 'direct')
