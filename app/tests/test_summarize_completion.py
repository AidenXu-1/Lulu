import unittest
from unittest.mock import patch

import requests

from src.core import summarize


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class SummarizeCompletionTests(unittest.TestCase):
    def test_delivery_chat_disables_deepseek_thinking_without_changing_default_chat(self):
        config = {
            "api_key": "sk-test",
            "base_url": "https://api.example.test",
            "model": "deepseek-v4-flash",
        }
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return _Resp({"choices": [{
                "message": {"content": "完整正文"},
                "finish_reason": "stop",
            }]})

        with patch.object(summarize.secrets, "get_config", return_value=config), patch.object(
            requests, "post", side_effect=fake_post
        ):
            reply = summarize.chat(
                [{"role": "user", "content": "校正文案"}],
                max_tokens=2400,
                thinking=False,
            )

        self.assertEqual(reply, "完整正文")
        self.assertEqual(calls[0]["thinking"], {"type": "disabled"})

    def test_truncated_chat_completion_is_rejected_instead_of_returning_partial_text(self):
        config = {
            "api_key": "sk-test",
            "base_url": "https://api.example.test",
            "model": "deepseek-v4-flash",
        }
        response = _Resp({
            "choices": [{
                "message": {"content": "只有前半段"},
                "finish_reason": "length",
            }],
        })
        with patch.object(summarize.secrets, "get_config", return_value=config), patch.object(
            requests, "post", return_value=response
        ):
            with self.assertRaisesRegex(summarize.OutputTruncatedError, "输出未完成.*length"):
                summarize.chat([{"role": "user", "content": "hi"}], max_tokens=20)

    def test_connection_probe_has_enough_output_budget_for_current_models(self):
        calls = []

        def fake_chat(messages, max_tokens=0, timeout=0, thinking=None):
            calls.append((messages, max_tokens, timeout, thinking))
            return "正常"

        with patch.object(summarize, "_chat", side_effect=fake_chat):
            result = summarize.test_connection()

        self.assertIn("连接正常", result)
        self.assertEqual(calls[0][0], [{"role": "user", "content": "只回复两个字:正常"}])
        self.assertGreaterEqual(calls[0][1], 64)
        self.assertEqual(calls[0][2], 30)
        self.assertIs(calls[0][3], False)


if __name__ == "__main__":
    unittest.main()
