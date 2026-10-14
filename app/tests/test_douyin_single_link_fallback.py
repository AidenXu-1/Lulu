"""抖音分享页空壳后的匿名公开详情回退。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core.audio import extractor
from src.core.collect.models import CollectedItem


WORK_ID = "7663085744302476579"
SHARE_URL = "https://v.douyin.com/example/"
CANONICAL_HTML = (
    '<link data-react-helmet="true" rel="canonical" '
    f'href="https://www.douyin.com/video/{WORK_ID}"/>'
    '<script>window._ROUTER_DATA = {"loaderData":{}}</script>'
)


def _video_item(**overrides) -> CollectedItem:
    values = {
        "aweme_id": WORK_ID,
        "work_id": WORK_ID,
        "title": "一人公司，越干越穷！",
        "author_name": "李守洲",
        "author_id": "MS4w.fixture",
        "create_time": 1_786_297_871,
        "duration_ms": 306_295,
        "like": 10,
        "collect": 20,
        "comment": 30,
        "share": 40,
        "play_url": "https://v26-web.douyinvod.com/video/fallback.mp4",
        "cover_url": "https://p3-sign.douyinpic.com/cover.jpeg",
        "cover_status": "url_only",
    }
    values.update(overrides)
    return CollectedItem(**values)


class DouyinSingleLinkFallbackTests(unittest.TestCase):
    def test_work_id_only_accepts_official_path_or_official_canonical(self):
        self.assertEqual(
            extractor._douyin_page_work_id(
                f"https://www.douyin.com/video/{WORK_ID}", ""
            ),
            WORK_ID,
        )
        self.assertEqual(
            extractor._douyin_page_work_id(SHARE_URL, CANONICAL_HTML),
            WORK_ID,
        )
        rejected = (
            f"https://www.douyin.com/?next=https://www.douyin.com/video/{WORK_ID}",
            f"https://www.douyin.com/video/{WORK_ID}/extra",
            f"https://douyin.com.attacker.test/video/{WORK_ID}",
            f"https://www.douyin.com/video/not-digits",
        )
        for source in rejected:
            with self.subTest(source=source):
                self.assertEqual(extractor._douyin_page_work_id(source, ""), "")

        attacker_canonical = (
            '<link rel="canonical" '
            f'href="https://douyin.com.attacker.test/video/{WORK_ID}">'
        )
        self.assertEqual(
            extractor._douyin_page_work_id(SHARE_URL, attacker_canonical), ""
        )

    def test_current_empty_shell_uses_one_public_detail_fallback(self):
        meta: dict = {}
        events: list[tuple[str, float]] = []
        with (
            patch.object(
                extractor,
                "_fetch_douyin_html",
                return_value=(CANONICAL_HTML, SHARE_URL),
            ) as fetch_page,
            patch(
                "src.core.collect.douyin_fetch.fetch_item",
                return_value=_video_item(),
            ) as fetch_detail,
        ):
            result = extractor._douyin_play_url(
                SHARE_URL,
                lambda message, fraction: events.append((message, fraction)),
                meta,
            )

        play, title, stats, author, work_id, cover_url = result
        self.assertEqual(fetch_page.call_count, 1)
        fetch_detail.assert_called_once_with(WORK_ID)
        self.assertEqual(
            play,
            "https://v26-web.douyinvod.com/video/fallback.mp4",
        )
        self.assertEqual(title, "一人公司，越干越穷！")
        self.assertEqual(
            stats,
            {"like": 10, "favorite": 20, "share": 40, "comment": 30},
        )
        self.assertEqual(author["name"], "李守洲")
        self.assertEqual(work_id, WORK_ID)
        self.assertEqual(cover_url, "https://p3-sign.douyinpic.com/cover.jpeg")
        self.assertEqual(
            meta["original_url"],
            f"https://www.douyin.com/video/{WORK_ID}",
        )
        self.assertEqual(meta["duration"], 306.295)
        self.assertTrue(events)

    def test_existing_embedded_media_does_not_call_detail_fallback(self):
        html = (
            '<script>window._ROUTER_DATA = '
            '{"aweme_detail":{"aweme_id":"123",'
            '"video":{"play_addr":{"url_list":'
            '["https://v26-web.douyinvod.com/video/original.mp4"]}}}}'
            '</script>'
        )
        with (
            patch.object(extractor, "_fetch_douyin_html", return_value=(html, SHARE_URL)),
            patch(
                "src.core.collect.douyin_fetch.fetch_item",
                side_effect=AssertionError("旧路径成功时不应触发详情回退"),
            ) as fetch_detail,
        ):
            result = extractor._douyin_play_url(SHARE_URL, lambda *_: None)

        fetch_detail.assert_not_called()
        self.assertEqual(
            result[0],
            "https://v26-web.douyinvod.com/video/original.mp4",
        )

    def test_detail_identity_and_media_domain_fail_closed(self):
        cases = (
            _video_item(work_id="999", aweme_id="999"),
            _video_item(play_url="http://v26-web.douyinvod.com/video.mp4"),
            _video_item(play_url="https://douyinvod.com.attacker.test/video.mp4"),
        )
        for item in cases:
            with self.subTest(item=item):
                with patch(
                    "src.core.collect.douyin_fetch.fetch_item",
                    return_value=item,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "身份不一致|非允许媒体地址",
                    ):
                        extractor._douyin_public_detail_fallback(WORK_ID)

    def test_confirmed_image_keeps_specific_no_audio_message(self):
        image_item = _video_item(
            aweme_type=68,
            content_kind="image",
            play_url="",
            duration_ms=0,
        )
        with (
            patch.object(
                extractor,
                "_fetch_douyin_html",
                return_value=(CANONICAL_HTML, SHARE_URL),
            ),
            patch(
                "src.core.collect.douyin_fetch.fetch_item",
                return_value=image_item,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "图文 / 图片帖"):
                extractor._douyin_play_url(SHARE_URL, lambda *_: None)

    def test_detail_failure_reports_valid_link_without_blaming_paste(self):
        with (
            patch.object(
                extractor,
                "_fetch_douyin_html",
                return_value=(CANONICAL_HTML, SHARE_URL),
            ),
            patch(
                "src.core.collect.douyin_fetch.fetch_item",
                side_effect=RuntimeError("fixture unavailable"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "链接有效.*公开作品详情暂时没有返回",
            ):
                extractor._douyin_play_url(SHARE_URL, lambda *_: None)

    def test_official_cdn_download_enforces_redirect_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_stream(url, destination, _progress, **kwargs):
                self.assertTrue(kwargs["url_validator"](url))
                self.assertEqual(
                    kwargs["referer"],
                    f"https://www.douyin.com/video/{WORK_ID}",
                )
                destination.write_bytes(
                    b"\x00" * 64 + b"ftypmp42" + b"\x00" * 64
                )
                return destination

            with (
                patch.object(
                    extractor,
                    "_douyin_play_url",
                    return_value=(
                        "https://v26-web.douyinvod.com/video/fallback.mp4",
                        "作品",
                        {},
                        {},
                        WORK_ID,
                        "",
                    ),
                ),
                patch.object(
                    extractor,
                    "_stream_download",
                    side_effect=fake_stream,
                ) as stream,
            ):
                result = extractor._download_douyin(
                    SHARE_URL,
                    root,
                    lambda *_: None,
                )

            self.assertTrue(result.exists())
            self.assertEqual(stream.call_count, 1)


if __name__ == "__main__":
    unittest.main()
