import unittest
from unittest.mock import patch

import requests

from src.core import agnes


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self):
        return self._data


class AgnesClientTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "api_key": "sk-test",
            "base_url": "https://apihub.agnes-ai.com/v1",
            "text_model": "agnes-2.0-flash",
            "image_model": "agnes-image-2.1-flash",
            "video_model": "agnes-video-v2.0",
        }
        self.old_get_config = agnes.secrets.get_config
        agnes.secrets.get_config = lambda key: dict(self.cfg)  # type: ignore[assignment]

    def tearDown(self):
        agnes.secrets.get_config = self.old_get_config  # type: ignore[assignment]


    def test_connection_probe_has_enough_output_budget_for_current_models(self):
        calls = []

        def fake_chat(messages, max_tokens=0, timeout=0, thinking=None):
            calls.append((messages, max_tokens, timeout, thinking))
            return "正常"

        with patch.object(agnes, "chat", side_effect=fake_chat):
            result = agnes.test_connection()

        self.assertIn("连接正常", result)
        self.assertEqual(calls[0][0], [{"role": "user", "content": "只回复两个字:正常"}])
        self.assertGreaterEqual(calls[0][1], 64)
        self.assertEqual(calls[0][2], 30)
        self.assertIs(calls[0][3], False)


if __name__ == "__main__":
    unittest.main()
