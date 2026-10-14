import unittest

from src.core.synthesis.errors import user_error_message
from src.core.synthesis.indextts2_text_guard import (
    RECOMMENDED_CHAR_LIMIT,
    RISKY_CHAR_LIMIT,
    assess_text_length,
    count_text_chars,
)


class IndexTTS2TextGuardTests(unittest.TestCase):
    def test_count_ignores_whitespace(self):
        self.assertEqual(count_text_chars("  你好\n 世界 "), 4)

    def test_empty_blocks_with_input_reason(self):
        result = assess_text_length("  \n")
        self.assertEqual(result.level, "empty")
        self.assertTrue(result.is_blocked)
        self.assertIn("请输入", result.blocking_reason)
        self.assertIn("单次字符保护", result.hint)
        self.assertIn("不是模型官方字符限制", result.hint)

    def test_normal_range(self):
        result = assess_text_length("字" * RECOMMENDED_CHAR_LIMIT)
        self.assertEqual(result.level, "normal")
        self.assertFalse(result.is_blocked)
        self.assertIn("可直接生成", result.hint)
        self.assertIn("单次字符保护建议", result.hint)

    def test_risky_range_warns_but_allows(self):
        result = assess_text_length("字" * (RECOMMENDED_CHAR_LIMIT + 1))
        self.assertEqual(result.level, "risky")
        self.assertTrue(result.is_risky)
        self.assertFalse(result.is_blocked)
        self.assertIn("未验证的长文本一次生成失败", result.hint)
        self.assertIn("不是模型官方字符限制", result.hint)

    def test_over_limit_blocks(self):
        result = assess_text_length("字" * (RISKY_CHAR_LIMIT + 1))
        self.assertEqual(result.level, "blocked")
        self.assertTrue(result.is_blocked)
        self.assertIn(str(RISKY_CHAR_LIMIT), result.blocking_reason)
        self.assertIn("单次字符保护上限", result.hint)
        self.assertIn("官方式自动分段", result.hint)

    def test_error_mapping_separates_length_and_runtime(self):
        self.assertEqual(user_error_message("IndexTTS2 生成超时"), "文本偏长或生成超时,请缩短文本后重试。")
        self.assertEqual(
            user_error_message("IndexTTS2 本地环境尚未准备完成:config missing"),
            "IndexTTS2 本地环境尚未准备完成,请先检查 runtime、model 与配置。",
        )


if __name__ == "__main__":
    unittest.main()
