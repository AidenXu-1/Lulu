"""单篇作品元数据写入 Obsidian 的离线回归。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.models import Transcript  # noqa: E402
from src.core.obsidian import build_note  # noqa: E402


class ObsidianWorkMetadataTests(unittest.TestCase):
    def test_alias_metadata_is_visible_and_keeps_existing_source_field(self):
        transcript = Transcript(
            text="逐字稿正文",
            source="https://v.douyin.com/share",
            meta={
                "original_url": "https://www.douyin.com/video/735002",
                "play_url": "https://v26.douyinvod.com/video.mp4",
                "create_time": 1785686400,
                "duration_ms": 95400,
            },
        )

        note = build_note(transcript, "完整作品")

        self.assertIn("日期: 2026-08-03", note)
        self.assertIn("来源: https://www.douyin.com/video/735002", note)
        self.assertIn("原作品链接: https://www.douyin.com/video/735002", note)
        self.assertIn("音视频直链: https://v26.douyinvod.com/video.mp4", note)
        self.assertIn("时长: 01:35", note)
        self.assertIn("> 原作品：https://www.douyin.com/video/735002", note)


if __name__ == "__main__":
    unittest.main()
