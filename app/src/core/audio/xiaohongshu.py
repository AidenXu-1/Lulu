"""Adapt public mobile note data to the existing yt-dlp video parser.

Desktop anonymous requests can redirect to a login shell. Mobile share pages
still provide public video data, under a different state key. No account cookies
or browser profile are read. Format parsing and downloading remain upstream.
"""
from copy import deepcopy

from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
from yt_dlp.utils import ExtractorError


def _exact_public_count(value):
    if isinstance(value, str):
        value = value.strip()
        if not (value.isascii() and value.isdecimal() and len(value) <= 20):
            return None
        value = int(value)
    return value if type(value) is int and value >= 0 else None


class LuluXiaoHongShuIE(XiaoHongShuIE):
    _MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
        "Mobile/15E148 Safari/604.1"
    )

    @classmethod
    def ie_key(cls):
        # Replace this provider only; all other yt-dlp extractors stay intact.
        return XiaoHongShuIE.ie_key()

    def _download_webpage(self, *args, **kwargs):
        kwargs['headers'] = {**kwargs.get('headers', {}), 'User-Agent': self._MOBILE_UA}
        return super()._download_webpage(*args, **kwargs)

    def _search_json(self, start_pattern, string, name, video_id, **kwargs):
        state = super()._search_json(start_pattern, string, name, video_id, **kwargs)
        if name != 'initial state':
            return state
        from yt_dlp.utils.traversal import traverse_obj

        note = traverse_obj(state, ('note', 'noteDetailMap', video_id, 'note'))
        if not isinstance(note, dict) or not note:
            note = traverse_obj(state, ('noteData', 'data', 'noteData'))
        if not isinstance(note, dict) or not note:
            raise ExtractorError(
                '小红书未取得作品资料，页面可能要求登录或分享链接已失效。请重新复制作品分享链接。',
                expected=True,
            )
        if note.get('noteId') != video_id:
            raise ExtractorError('小红书返回的作品身份不一致，已停止下载。', expected=True)
        if note.get('type') == 'normal' and not note.get('video'):
            raise ExtractorError('这条小红书作品是图文笔记，没有可提取的音视频。', expected=True)
        note = deepcopy(note)
        for image in note.get('imageList') or []:
            if isinstance(image, dict) and image.get('url') and not image.get('urlDefault'):
                image['urlDefault'] = image['url']
        self._lulu_note = note
        return {'note': {'noteDetailMap': {video_id: {'note': note}}}}

    def _real_extract(self, url):
        self._lulu_note = {}
        info = super()._real_extract(url)
        if not info.get('formats'):
            raise ExtractorError(
                '小红书未取得视频地址，请重新复制作品分享链接后重试。', expected=True,
            )
        user = self._lulu_note.get('user') or {}
        info['uploader'] = user.get('nickName') or user.get('nickname')
        info['duration'] = next(
            (f['duration'] for f in info['formats'] if f.get('duration') is not None), None,
        )
        # Read only the identity-checked note already used for media. Missing
        # counters must not cause another page/profile request or break video.
        stats = self._lulu_note.get('interactInfo')
        if isinstance(stats, dict):
            for source, target in (
                ('likedCount', 'like_count'), ('collectedCount', 'save_count'),
                ('commentCount', 'comment_count'), ('shareCount', 'repost_count'),
            ):
                count = _exact_public_count(stats.get(source))
                if count is not None:
                    info[target] = count
        return info
