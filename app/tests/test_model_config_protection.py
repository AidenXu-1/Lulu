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

    def test_success_saves_exact_resolved_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            (old / "tts").mkdir(parents=True)
            cfg = SimpleNamespace(models_dir=str(old), save=Mock(return_value=True))
            with patch.object(mm, "models_root", return_value=old), patch.object(mm, "app_data_dir", return_value=root):
                mm.relocate_and_save(root / "new", cfg)
            self.assertEqual(cfg.models_dir, str((root / "new").resolve()))
            self.assertTrue((root / "new/tts").exists())

    def test_failed_restore_reports_both_locations(self):
        cfg = SimpleNamespace(models_dir="/old", save=Mock(return_value=False))
        with patch.object(mm, "models_root", return_value=Path("/old")), patch.object(
            mm, "relocate_models", side_effect=[None, OSError("unplugged")]
        ):
            with self.assertRaisesRegex(RuntimeError, "保留两处目录") as raised:
                mm.relocate_and_save(Path("/new"), cfg)
        self.assertIn("/old", str(raised.exception))
        self.assertIn("/new", str(raised.exception))
        self.assertEqual(cfg.models_dir, "/old")

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
