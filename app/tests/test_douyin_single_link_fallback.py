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


















if __name__ == "__main__":
    unittest.main()
