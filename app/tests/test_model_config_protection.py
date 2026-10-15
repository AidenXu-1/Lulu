"""Temporary files, fake credentials, and fake HTTP only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.core import agnes, models_manager as mm, summarize


class ModelConfigProtectionTests(unittest.TestCase):
    def test_failed_location_save_restores_files_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / "old", root / "new"
            (old / "models/asr").mkdir(parents=True)
            (old / "models/asr/weights").write_bytes(b"original")
            cfg = SimpleNamespace(models_dir="", save=Mock(return_value=False))
            with patch.object(mm, "models_root", return_value=old), patch.object(mm, "app_data_dir", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "模型已恢复原位置"):
                    mm.relocate_and_save(new, cfg)
            self.assertEqual(cfg.models_dir, "")
            self.assertEqual((old / "models/asr/weights").read_bytes(), b"original")
            self.assertFalse((new / "models").exists())


    def test_connection_uses_draft_without_reading_or_writing_secrets(self):
        draft = {"api_key": "draft-key", "base_url": "https://draft.example.test", "model": "draft-model"}
        for client in (summarize, agnes):
            with self.subTest(client=client.__name__), patch.object(
                client.secrets, "get_config", side_effect=AssertionError("must not read saved credentials")
            ), patch.object(client.secrets, "set_config", side_effect=AssertionError("must not save draft")), patch(
                "requests.post", return_value=SimpleNamespace(
                    status_code=401, text="denied", json=lambda: {}
                )
            ) as post:
                with self.assertRaises(RuntimeError):
                    client.test_connection(draft)
                self.assertEqual(post.call_args.args[0], "https://draft.example.test/chat/completions")
                self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer draft-key")
                self.assertEqual(post.call_args.kwargs["json"]["model"], "draft-model")


if __name__ == "__main__":
    unittest.main()
