import json
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path

from src.core.synthesis import minimax_tts


class MiniMaxTtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._config: dict = {}
        self._old_get_config = minimax_tts.secrets.get_config
        self._old_set_config = minimax_tts.secrets.set_config
        self._old_app_data_dir = minimax_tts.app_data_dir
        self._old_analyze_recording_file = minimax_tts.analyze_recording_file
        minimax_tts.secrets.get_config = lambda _key: dict(self._config)  # type: ignore[assignment]
        minimax_tts.secrets.set_config = self._set_config  # type: ignore[assignment]
        minimax_tts.app_data_dir = lambda: self.root / "app_data"  # type: ignore[assignment]
        minimax_tts.analyze_recording_file = lambda _path: {"duration": 12.0, "is_silent": False}  # type: ignore[assignment]

    def tearDown(self):
        minimax_tts.secrets.get_config = self._old_get_config  # type: ignore[assignment]
        minimax_tts.secrets.set_config = self._old_set_config  # type: ignore[assignment]
        minimax_tts.app_data_dir = self._old_app_data_dir  # type: ignore[assignment]
        minimax_tts.analyze_recording_file = self._old_analyze_recording_file  # type: ignore[assignment]
        self.tmp.cleanup()

    def _set_config(self, _key: str, value: dict) -> None:
        self._config = dict(value)

    def test_config_requires_api_key_but_not_group_id(self):
        minimax_tts.save_config({"api_key": "  test-key  ", "group_id": ""})

        self.assertTrue(minimax_tts.is_configured())
        req = minimax_tts.request_from_config(text="你好", output_path=self.root / "a.mp3")
        self.assertEqual(req.api_key, "test-key")
        self.assertEqual(req.group_id, "")
        self.assertEqual(req.base_url, minimax_tts.DEFAULT_BASE_URL)

    def test_validate_blocks_empty_missing_key_and_too_long_text(self):
        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "请输入"):
            minimax_tts.validate_request(
                minimax_tts.MiniMaxTtsRequest(text="", output_path=self.root / "a.mp3", api_key="k")
            )
        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "API Key"):
            minimax_tts.validate_request(
                minimax_tts.MiniMaxTtsRequest(text="你好", output_path=self.root / "a.mp3", api_key="")
            )
        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "3000"):
            minimax_tts.validate_request(
                minimax_tts.MiniMaxTtsRequest(
                    text="你" * (minimax_tts.MAX_SYNC_CHARS + 1),
                    output_path=self.root / "a.mp3",
                    api_key="k",
                )
            )
        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "10000"):
            minimax_tts.validate_request(
                minimax_tts.MiniMaxTtsRequest(
                    text="你" * minimax_tts.MAX_API_CHARS,
                    output_path=self.root / "a.mp3",
                    api_key="k",
                )
            )

    def test_synthesize_uses_hex_response_and_writes_local_file(self):
        calls = []
        api_key = "unit_test_key"

        def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> bytes:
            calls.append((url, headers, json.loads(body.decode("utf-8")), timeout))
            return json.dumps(
                {
                    "base_resp": {"status_code": 0, "status_msg": "ok"},
                    "data": {"audio": "000102"},
                    "extra_info": {"audio_length": 1234, "usage_characters": 2},
                    "trace_id": "trace-1234567890",
                }
            ).encode("utf-8")

        out = self.root / "voice.mp3"
        result = minimax_tts.synthesize(
            minimax_tts.MiniMaxTtsRequest(text="你好", output_path=out, api_key=api_key),
            post=fake_post,
        )

        self.assertEqual(out.read_bytes(), b"\x00\x01\x02")
        self.assertEqual(result.output_path, out)
        self.assertEqual(result.duration_seconds, 1.234)
        self.assertEqual(result.meta["engine"], "minimax")
        self.assertTrue(result.meta["cloud"])
        self.assertEqual(calls[0][0], "https://api.minimax.io/v1/t2a_v2")
        self.assertEqual(calls[0][1]["Authorization"], f"Bearer {api_key}")
        self.assertEqual(calls[0][2]["output_format"], "hex")
        self.assertEqual(calls[0][2]["text"], "你好")
        self.assertEqual(calls[0][2]["audio_setting"]["format"], "mp3")

    def test_remote_error_is_user_safe_and_does_not_write_audio(self):
        text = "这是一段不该出现在错误里的完整文案"
        out = self.root / "voice.mp3"
        api_key = "unit_test_key"

        def fake_post(_url: str, _headers: dict[str, str], _body: bytes, _timeout: int) -> bytes:
            payload = json.dumps(
                {"base_resp": {"status_code": 1008, "status_msg": "unauthorized " + api_key + " " + text}}
            ).encode("utf-8")
            raise urllib.error.HTTPError("https://api.minimax.io/v1/t2a_v2", 401, "Unauthorized", {}, BytesIO(payload))

        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.synthesize(
                minimax_tts.MiniMaxTtsRequest(text=text, output_path=out, api_key=api_key),
                post=fake_post,
            )

        message = minimax_tts.safe_error_message(ctx.exception)
        self.assertIn("API Key", message)
        self.assertNotIn(api_key, message)
        self.assertNotIn(text, message)
        self.assertFalse(out.exists())

    def test_non_json_response_is_safe(self):
        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.synthesize(
                minimax_tts.MiniMaxTtsRequest(text="你好", output_path=self.root / "voice.mp3", api_key="unit_test_key"),
                post=lambda *_args: b"not json",
            )

        self.assertEqual(minimax_tts.safe_error_message(ctx.exception), "MiniMax 返回了无法识别的结果，未保存音频。")

    def test_voice_catalog_uses_builtin_fallback_without_network(self):
        catalog = minimax_tts.voice_catalog()

        self.assertTrue(catalog.used_fallback)
        self.assertGreater(len(catalog.official), 0)
        self.assertEqual(catalog.official[0].provider, "MiniMax")

    def test_refresh_voice_catalog_is_explicit_and_caches_low_sensitive_fields(self):
        minimax_tts.save_config({"api_key": "unit-key"})
        calls = []

        def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> bytes:
            calls.append((url, headers, json.loads(body.decode("utf-8")), timeout))
            return json.dumps(
                {
                    "base_resp": {"status_code": 0, "status_msg": "ok"},
                    "system_voice": [{"voice_id": "official_voice", "voice_name": "官方音色"}],
                    "voice_cloning": [{"voice_id": "clone_voice", "voice_name": "我的克隆"}],
                    "trace_id": "do-not-store-full-response",
                }
            ).encode("utf-8")

        catalog = minimax_tts.refresh_voice_catalog(post=fake_post)
        cache = json.loads((self.root / "app_data" / "minimax" / "voices.json").read_text(encoding="utf-8"))

        self.assertEqual(calls[0][0], "https://api.minimax.io/v1/get_voice")
        self.assertEqual(calls[0][2]["voice_type"], "all")
        self.assertEqual(catalog.official[0].voice_id, "official_voice")
        self.assertEqual(catalog.cloned[0].voice_id, "clone_voice")
        self.assertNotIn("trace_id", json.dumps(cache))
        self.assertIn("official", cache)
        self.assertIn("cloned", cache)

    def test_clone_voice_requires_authorization_and_valid_sample(self):
        sample = self.root / "sample.wav"
        sample.write_bytes(b"not empty")

        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "确认"):
            minimax_tts.clone_voice(
                minimax_tts.MiniMaxCloneRequest(sample_path=sample, display_name="test", authorization_confirmed=False)
            )

        bad = self.root / "sample.txt"
        bad.write_text("x")
        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "MP3"):
            minimax_tts.validate_clone_sample(bad)

    def test_clone_voice_uploads_without_preview_text_and_saves_record_after_success(self):
        sample = self.root / "sample.wav"
        sample.write_bytes(b"fake wav")
        minimax_tts.save_config({"api_key": "unit-key"})
        multipart_calls = []
        clone_calls = []

        def fake_multipart(url, headers, fields, file_field, file_path, timeout):
            multipart_calls.append((url, headers, fields, file_field, file_path, timeout))
            return json.dumps(
                {"base_resp": {"status_code": 0, "status_msg": "ok"}, "file": {"file_id": "file_1234567890"}}
            ).encode("utf-8")

        def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> bytes:
            payload = json.loads(body.decode("utf-8"))
            clone_calls.append((url, headers, payload, timeout))
            return json.dumps({"base_resp": {"status_code": 0, "status_msg": "ok"}, "trace_id": "trace-abc"}).encode(
                "utf-8"
            )

        result = minimax_tts.clone_voice(
            minimax_tts.MiniMaxCloneRequest(
                sample_path=sample,
                display_name="我的克隆",
                voice_id="Lulu_clone_001",
                authorization_confirmed=True,
            ),
            post=fake_post,
            multipart_post=fake_multipart,
        )
        catalog = minimax_tts.voice_catalog()

        self.assertEqual(multipart_calls[0][0], "https://api.minimax.io/v1/files/upload")
        self.assertEqual(multipart_calls[0][2]["purpose"], "voice_clone")
        self.assertEqual(clone_calls[0][0], "https://api.minimax.io/v1/voice_clone")
        self.assertEqual(clone_calls[0][2]["file_id"], "file_1234567890")
        self.assertEqual(clone_calls[0][2]["voice_id"], "Lulu_clone_001")
        self.assertNotIn("text", clone_calls[0][2])
        self.assertNotIn("model", clone_calls[0][2])
        self.assertEqual(result.voice.voice_id, "Lulu_clone_001")
        self.assertEqual(catalog.cloned[0].voice_id, "Lulu_clone_001")

    def test_clone_failure_after_upload_requests_remote_file_delete(self):
        sample = self.root / "sample.wav"
        sample.write_bytes(b"fake wav")
        minimax_tts.save_config({"api_key": "unit-key"})
        post_calls = []

        def fake_multipart(*_args):
            return json.dumps(
                {"base_resp": {"status_code": 0, "status_msg": "ok"}, "file": {"file_id": "file_1234567890"}}
            ).encode("utf-8")

        def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> bytes:
            payload = json.loads(body.decode("utf-8"))
            post_calls.append((url, headers, payload, timeout))
            if url.endswith("/v1/voice_clone"):
                return json.dumps({"base_resp": {"status_code": 1001, "status_msg": "server failed"}}).encode("utf-8")
            return json.dumps({"base_resp": {"status_code": 0, "status_msg": "ok"}}).encode("utf-8")

        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.clone_voice(
                minimax_tts.MiniMaxCloneRequest(
                    sample_path=sample,
                    display_name="我的克隆",
                    voice_id="Lulu_clone_002",
                    authorization_confirmed=True,
                ),
                post=fake_post,
                multipart_post=fake_multipart,
            )

        urls = [call[0] for call in post_calls]
        delete_call = post_calls[urls.index("https://api.minimax.io/v1/files/delete")]
        self.assertEqual(delete_call[2], {"file_id": "file_1234567890", "purpose": "voice_clone"})
        self.assertIn("已请求 MiniMax 删除远端样本文件", minimax_tts.safe_error_message(ctx.exception))
        self.assertNotIn("file_1234567890", minimax_tts.safe_error_message(ctx.exception))
        self.assertEqual(minimax_tts.voice_catalog().cloned, [])

    def test_clone_cleanup_failure_is_sanitized(self):
        sample = self.root / "sample.wav"
        sample.write_bytes(b"fake wav")
        minimax_tts.save_config({"api_key": "unit-key"})

        def fake_multipart(*_args):
            return json.dumps(
                {"base_resp": {"status_code": 0, "status_msg": "ok"}, "file": {"file_id": "file_sensitive_123"}}
            ).encode("utf-8")

        def fake_post(url: str, _headers: dict[str, str], _body: bytes, _timeout: int) -> bytes:
            if url.endswith("/v1/files/delete"):
                payload = json.dumps(
                    {"base_resp": {"status_code": 403, "status_msg": "forbidden file_sensitive_123 unit-key"}}
                ).encode("utf-8")
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, BytesIO(payload))
            return json.dumps({"base_resp": {"status_code": 1001, "status_msg": "server failed"}}).encode("utf-8")

        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.clone_voice(
                minimax_tts.MiniMaxCloneRequest(
                    sample_path=sample,
                    display_name="我的克隆",
                    voice_id="Lulu_clone_003",
                    authorization_confirmed=True,
                ),
                post=fake_post,
                multipart_post=fake_multipart,
            )

        msg = minimax_tts.safe_error_message(ctx.exception)
        self.assertIn("未确认成功", msg)
        self.assertNotIn("file_sensitive_123", msg)
        self.assertNotIn("unit-key", msg)
        self.assertEqual(minimax_tts.voice_catalog().cloned, [])

    def test_clone_cancel_after_upload_requests_cleanup_and_saves_no_record(self):
        sample = self.root / "sample.wav"
        sample.write_bytes(b"fake wav")
        minimax_tts.save_config({"api_key": "unit-key"})
        cancelled = {"value": False}
        post_calls = []

        def fake_multipart(*_args):
            cancelled["value"] = True
            return json.dumps(
                {"base_resp": {"status_code": 0, "status_msg": "ok"}, "file": {"file_id": "file_cancel_123"}}
            ).encode("utf-8")

        def fake_post(url: str, _headers: dict[str, str], body: bytes, _timeout: int) -> bytes:
            post_calls.append((url, json.loads(body.decode("utf-8"))))
            return json.dumps({"base_resp": {"status_code": 0, "status_msg": "ok"}}).encode("utf-8")

        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.clone_voice(
                minimax_tts.MiniMaxCloneRequest(
                    sample_path=sample,
                    display_name="我的克隆",
                    voice_id="Lulu_clone_004",
                    authorization_confirmed=True,
                    is_cancelled=lambda: cancelled["value"],
                ),
                post=fake_post,
                multipart_post=fake_multipart,
            )

        self.assertEqual(ctx.exception.code, "CANCELLED")
        self.assertEqual(post_calls[0][0], "https://api.minimax.io/v1/files/delete")
        self.assertEqual(post_calls[0][1]["purpose"], "voice_clone")
        self.assertEqual(minimax_tts.voice_catalog().cloned, [])

    def test_local_remove_and_remote_delete_have_separate_semantics(self):
        minimax_tts.save_config({"api_key": "unit-key"})
        voice = minimax_tts.MiniMaxVoice(
            voice_id="Lulu_clone_delete_001",
            name="待删除",
            source="voice_cloning",
            category="我的云端克隆音色",
        )
        minimax_tts._save_cloned_voice(voice)
        calls = []

        result = minimax_tts.delete_cloned_voice("Lulu_clone_delete_001", delete_remote=False)

        self.assertFalse(result.remote_attempted)
        self.assertTrue(result.local_removed)
        self.assertIn("远端音色未删除", result.user_message)
        self.assertEqual(calls, [])
        self.assertEqual(minimax_tts.voice_catalog().cloned, [])

        minimax_tts._save_cloned_voice(voice)

        def fake_post(url: str, _headers: dict[str, str], body: bytes, _timeout: int) -> bytes:
            calls.append((url, json.loads(body.decode("utf-8"))))
            return json.dumps({"base_resp": {"status_code": 0, "status_msg": "ok"}}).encode("utf-8")

        result = minimax_tts.delete_cloned_voice("Lulu_clone_delete_001", delete_remote=True, post=fake_post)

        self.assertTrue(result.remote_attempted)
        self.assertTrue(result.remote_succeeded)
        self.assertTrue(result.local_removed)
        self.assertEqual(calls[0][0], "https://api.minimax.io/v1/delete_voice")
        self.assertEqual(calls[0][1]["voice_type"], "voice_cloning")
        self.assertEqual(calls[0][1]["voice_id"], "Lulu_clone_delete_001")
        self.assertEqual(minimax_tts.voice_catalog().cloned, [])

    def test_remote_delete_failure_keeps_local_record_and_is_safe(self):
        minimax_tts.save_config({"api_key": "unit-key"})
        voice = minimax_tts.MiniMaxVoice(
            voice_id="Lulu_clone_keep_001",
            name="保留",
            source="voice_cloning",
            category="我的云端克隆音色",
        )
        minimax_tts._save_cloned_voice(voice)

        def fake_post(url: str, _headers: dict[str, str], _body: bytes, _timeout: int) -> bytes:
            payload = json.dumps(
                {"base_resp": {"status_code": 403, "status_msg": "forbidden Lulu_clone_keep_001 unit-key"}}
            ).encode("utf-8")
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, BytesIO(payload))

        with self.assertRaises(minimax_tts.MiniMaxTtsError) as ctx:
            minimax_tts.delete_cloned_voice("Lulu_clone_keep_001", delete_remote=True, post=fake_post)

        msg = minimax_tts.safe_error_message(ctx.exception)
        self.assertIn("API Key", msg)
        self.assertNotIn("Lulu_clone_keep_001", msg)
        self.assertNotIn("unit-key", msg)
        self.assertEqual(minimax_tts.voice_catalog().cloned[0].voice_id, "Lulu_clone_keep_001")

    def test_clone_sample_duration_boundaries_are_blocked(self):
        sample = self.root / "short.wav"
        sample.write_bytes(b"fake wav")
        minimax_tts.analyze_recording_file = lambda _path: {"duration": 9.9, "is_silent": False}  # type: ignore[assignment]

        with self.assertRaisesRegex(minimax_tts.MiniMaxTtsError, "至少"):
            minimax_tts.validate_clone_sample(sample)


if __name__ == "__main__":
    unittest.main()
