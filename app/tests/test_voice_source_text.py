import unittest

from src.core.document_text import voice_text_from_source


class VoiceSourceTextTests(unittest.TestCase):
    def test_transcript_source_keeps_only_unique_transcript_section(self):
        document = """---
标题: 测试作品
---

# 测试作品

![封面](file:///Users/test/cover.webp)

## 简介

这段简介不能送去配音。

## 逐字稿

真正需要配音的第一句。

真正需要配音的第二句。

## 文案拆解

这段拆解也不能送去配音。
"""

        self.assertEqual(
            voice_text_from_source(document, "transcript"),
            "真正需要配音的第一句。\n\n真正需要配音的第二句。",
        )

    def test_plain_transcript_text_is_preserved(self):
        self.assertEqual(
            voice_text_from_source("  纯逐字稿第一句。\n第二句。  ", "transcript"),
            "纯逐字稿第一句。\n第二句。",
        )

    def test_rewrite_source_is_not_stripped_even_when_it_uses_headings(self):
        rewrite = "# 改写口播\n\n## 开场\n\n先说第一句。\n\n## 正文\n\n再说第二句。"
        self.assertEqual(voice_text_from_source(rewrite, "rewrite"), rewrite)

    def test_structured_transcript_without_unique_transcript_section_fails_closed(self):
        missing = "# 标题\n\n## 简介\n\n没有逐字稿章节。"
        duplicated = "## 逐字稿\n\n第一段\n\n## 逐字稿\n\n第二段"
        self.assertEqual(voice_text_from_source(missing, "transcript"), "")
        self.assertEqual(voice_text_from_source(duplicated, "transcript"), "")


if __name__ == "__main__":
    unittest.main()
