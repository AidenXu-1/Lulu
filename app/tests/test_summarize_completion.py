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
