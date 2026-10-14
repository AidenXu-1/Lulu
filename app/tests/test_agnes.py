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

    def test_chat_payload_uses_chat_completions_and_bearer(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append((url, headers, json, timeout))
            return _Resp(data={"choices": [{"message": {"content": "正常"}}]})

        with patch.object(requests, "post", fake_post):
            reply = agnes.chat([{"role": "user", "content": "hi"}], max_tokens=12, timeout=7)

        self.assertEqual(reply, "正常")
        url, headers, payload, timeout = calls[0]
        self.assertEqual(url, "https://apihub.agnes-ai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(payload["model"], "agnes-2.0-flash")
        self.assertEqual(payload["messages"][0]["content"], "hi")
        self.assertEqual(payload["max_tokens"], 12)
        self.assertNotIn("chat_template_kwargs", payload)
        self.assertEqual(timeout, 7)

    def test_delivery_chat_disables_provider_thinking_without_changing_default_chat(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return _Resp(data={"choices": [{
                "message": {"content": "完整正文"},
                "finish_reason": "stop",
            }]})

        with patch.object(requests, "post", fake_post):
            reply = agnes.chat(
                [{"role": "user", "content": "校正文案"}],
                max_tokens=2400,
                thinking=False,
            )

        self.assertEqual(reply, "完整正文")
        self.assertEqual(
            calls[0]["chat_template_kwargs"],
            {"enable_thinking": False},
        )

    def test_image_generation_puts_response_format_in_extra_body(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append((url, json))
            return _Resp(data={"data": [{"url": "https://example.test/image.png"}]})

        with patch.object(requests, "post", fake_post):
            result = agnes.generate_image("封面图", image=["https://example.test/ref.png"], response_format="url")

        self.assertEqual(result["url"], "https://example.test/image.png")
        url, payload = calls[0]
        self.assertEqual(url, "https://apihub.agnes-ai.com/v1/images/generations")
        self.assertEqual(payload["model"], "agnes-image-2.1-flash")
        self.assertEqual(payload["extra_body"]["image"], ["https://example.test/ref.png"])
        self.assertEqual(payload["extra_body"]["response_format"], "url")

    def test_video_task_payload_and_ids(self):
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append((url, json))
            return _Resp(data={"task_id": "task-1", "video_id": "video-1", "status": "queued"})

        with patch.object(requests, "post", fake_post):
            result = agnes.create_video_task("动态封面", image="https://example.test/cover.png")

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["video_id"], "video-1")
        url, payload = calls[0]
        self.assertEqual(url, "https://apihub.agnes-ai.com/v1/videos")
        self.assertEqual(payload["model"], "agnes-video-v2.0")
        self.assertEqual(payload["image"], "https://example.test/cover.png")

    def test_video_result_uses_video_id_query(self):
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append((url, headers, timeout))
            return _Resp(data={"status": "succeeded", "url": "https://example.test/video.mp4"})

        with patch.object(requests, "get", fake_get):
            result = agnes.get_video_result("video-1", timeout=9)

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("video_id=video-1", calls[0][0])
        self.assertEqual(calls[0][1]["Authorization"], "Bearer sk-test")
        self.assertEqual(calls[0][2], 9)

    def test_http_errors_are_redacted_chinese_messages(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            return _Resp(status_code=401, text="raw secret sk-test")

        with patch.object(requests, "post", fake_post):
            with self.assertRaisesRegex(agnes.AgnesError, "API Key 无效"):
                agnes.chat([{"role": "user", "content": "hi"}])

    def test_network_timeout_is_chinese_message(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            raise requests.RequestException("timeout")

        with patch.object(requests, "post", fake_post):
            with self.assertRaisesRegex(agnes.AgnesError, "连接 Agnes 失败"):
                agnes.chat([{"role": "user", "content": "hi"}])

    def test_missing_fields_fail_cleanly(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            return _Resp(data={"choices": []})

        with patch.object(requests, "post", fake_post):
            with self.assertRaisesRegex(agnes.AgnesError, "文本结果缺少"):
                agnes.chat([{"role": "user", "content": "hi"}])

    def test_truncated_chat_completion_is_rejected_instead_of_returning_partial_text(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            return _Resp(data={
                "choices": [{
                    "message": {"content": "只有前半段"},
                    "finish_reason": "length",
                }],
            })

        with patch.object(requests, "post", fake_post):
            with self.assertRaisesRegex(agnes.AgnesOutputTruncatedError, "输出未完成.*length"):
                agnes.chat([{"role": "user", "content": "hi"}])

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

    def test_save_config_uses_secret_store_slot(self):
        saved = {}
        old_set_config = agnes.secrets.set_config
        try:
            agnes.secrets.set_config = lambda key, value: saved.update({key: value})  # type: ignore[assignment]
            agnes.save_config({"api_key": "sk-real", "base_url": "https://example.test/v1"})
        finally:
            agnes.secrets.set_config = old_set_config  # type: ignore[assignment]

        self.assertIn(agnes.CONFIG_KEY, saved)
        self.assertEqual(saved[agnes.CONFIG_KEY]["api_key"], "sk-real")
        self.assertEqual(saved[agnes.CONFIG_KEY]["base_url"], "https://example.test/v1")


if __name__ == "__main__":
    unittest.main()
