import unittest

from src.core.synthesis.errors import scrub_detail, user_error_message


class SynthesisErrorMessageTests(unittest.TestCase):
    def test_segment_failure_hides_input_text_body_path_and_secret(self):
        msg = user_error_message(
            RuntimeError(
                "第 2/2 段生成失败:stderr input text: 我的私密正文 "
                "/Users/aiden/private/model.bin token sk-secret-123"
            )
        )
        self.assertIn("第 2/2 段生成失败", msg)
        self.assertIn("input text: [已隐藏正文]", msg)
        self.assertNotIn("我的私密正文", msg)
        self.assertNotIn("/Users/aiden", msg)
        self.assertNotIn("sk-secret", msg)
        self.assertNotIn("本地环境尚未准备", msg)

    def test_segment_failure_hides_prompt_body(self):
        msg = user_error_message(
            RuntimeError("第 3/4 段生成失败:prompt=这是客户隐私口播正文 config=/Users/aiden/private/config.toml")
        )
        self.assertIn("第 3/4 段生成失败", msg)
        self.assertIn("prompt: [已隐藏正文]", msg)
        self.assertNotIn("客户隐私", msg)
        self.assertNotIn("/Users/aiden", msg)

    def test_generic_config_word_does_not_invent_environment_failure(self):
        msg = user_error_message(RuntimeError("RuntimeError: config missing at /Users/aiden/secret/config.toml"))
        self.assertEqual(msg, "配音失败,请检查输入后重试。")

    def test_model_warning_does_not_invent_environment_failure(self):
        msg = user_error_message(
            RuntimeError("GPT2InferenceModel model architecture warning; past_key_values is deprecated"),
            "IndexTTS2 本次生成未完成，请稍后重试。",
        )
        self.assertEqual(msg, "IndexTTS2 本次生成未完成，请稍后重试。")

    def test_explicit_indextts2_readiness_error_keeps_environment_message(self):
        msg = user_error_message(RuntimeError("IndexTTS2 本地环境尚未准备完成:config missing"))
        self.assertEqual(msg, "IndexTTS2 本地环境尚未准备完成,请先检查 runtime、model 与配置。")

    def test_forced_worker_exit_reports_resource_pressure(self):
        msg = user_error_message(RuntimeError("IndexTTS2 worker forced_exit returncode=-9"))
        self.assertEqual(
            msg,
            "系统资源不足或模型进程被强制结束，IndexTTS2 本次生成已安全停止。文案和音色已保留，请释放内存后重试。",
        )

    def test_scrub_detail_hides_prompt_even_outside_segment_failure(self):
        msg = scrub_detail("prompt=客户稿正文 token sk-secret-123 /Users/aiden/private/file.txt")
        self.assertIn("prompt: [已隐藏正文]", msg)
        self.assertNotIn("客户稿正文", msg)
        self.assertNotIn("/Users/aiden", msg)
        self.assertNotIn("sk-secret", msg)

    def test_scrub_detail_preserves_tail_root_cause_after_long_transformers_warning(self):
        raw = (
            "GPT2InferenceModel has generative capabilities, as `prepare_inputs_for_generation` is explicitly overwritten. "
            * 20
            + "\npast_key_values is deprecated and will be removed in a future version.\n"
            + "RuntimeError: shape mismatch in semantic tokens at /Users/aiden/private/model.bin"
        )
        msg = scrub_detail(raw, limit=220)
        self.assertIn("RuntimeError: shape mismatch in semantic tokens", msg)
        self.assertNotIn("GPT2InferenceModel has generative capabilities", msg)
        self.assertNotIn("/Users/aiden", msg)

    def test_segment_failure_preserves_tail_root_cause(self):
        raw = (
            "第 1/4 段生成失败:IndexTTS2 生成失败。:"
            + "GPT2InferenceModel has generative capabilities " * 30
            + "RuntimeError: emotion_text cannot be empty"
        )
        msg = user_error_message(raw)
        self.assertIn("第 1/4 段生成失败", msg)
        self.assertIn("RuntimeError: emotion_text cannot be empty", msg)


if __name__ == "__main__":
    unittest.main()
