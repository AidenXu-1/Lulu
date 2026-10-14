import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from src.core import models_manager
from src.core.synthesis import voices
from src.ui import icons, theme as T
from src.ui.settings_page import SettingsPage, StorageRelocateWorker


class _Cfg:
    def __init__(self, root: Path):
        self.theme = "warm"
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voice-outputs")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models-root")
        self.auto_update_enabled = True
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"
        self.save_result = True

    def save(self):
        return self.save_result


class SettingsStorageVisualTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _Cfg(self.root)

    def tearDown(self):
        T.apply_theme("warm")
        self.tmp.cleanup()

    def test_six_real_directories_have_unique_semantic_vector_icons(self):
        page = SettingsPage(self.cfg)
        self.assertEqual(tuple(page.path_labels), (
            "last_output_dir", "media_dir", "recording_dir", "tts_output_dir",
            "voice_library_dir", "models_dir",
        ))
        self.assertEqual(len(page.path_icon_names), 6)
        self.assertEqual(len(set(page.path_icon_names.values())), 6)
        for field, icon_name in page.path_icon_names.items():
            pixmap = icons.pixmap(icon_name, T.TERRA, 18, 2.0)
            self.assertFalse(pixmap.isNull(), field)
            self.assertEqual(pixmap.size().toTuple(), (36, 36))
            label = page.path_icon_labels[field]
            self.assertIsInstance(label, QLabel)
            self.assertEqual(label.accessibleName(), f"{page.path_titles[field]}图标")

    def test_media_copy_matches_current_output_choices(self):
        page = SettingsPage(self.cfg)
        copy = page.path_description_labels["media_dir"].text()
        self.assertIn("视频和封面", copy)
        self.assertIn("主页采集", copy)
        self.assertNotIn("“保存到”", copy)
        self.assertIn("先保存在音频库", page.path_description_labels["tts_output_dir"].text())

    def test_managed_storage_changes_disable_both_relocation_buttons(self):
        page = SettingsPage(self.cfg)
        page._set_managed_storage_buttons_enabled(False)
        self.assertFalse(page.path_buttons["voice_library_dir"].isEnabled())
        self.assertFalse(page.path_buttons["models_dir"].isEnabled())
        self.assertTrue(page.path_buttons["media_dir"].isEnabled())
        page._set_managed_storage_buttons_enabled(True)
        self.assertTrue(page.path_buttons["voice_library_dir"].isEnabled())
        self.assertTrue(page.path_buttons["models_dir"].isEnabled())

    def test_empty_voice_and_model_fields_show_effective_managed_paths(self):
        self.cfg.voice_library_dir = ""
        self.cfg.models_dir = ""
        voice_default = self.root / "managed-voices"
        model_default = self.root / "managed-models"
        with patch.object(voices, "current_voice_library_dir", return_value=voice_default), \
                patch.object(models_manager, "models_root", return_value=model_default):
            page = SettingsPage(self.cfg)
        self.assertEqual(page.path_labels["voice_library_dir"].text(), str(voice_default))
        self.assertEqual(page.path_labels["models_dir"].text(), str(model_default))

    def test_model_worker_saves_after_move_and_rolls_back_on_save_failure(self):
        old_root = self.root / "old-models"
        new_root = self.root / "new-models"
        self.cfg.models_dir = str(old_root)
        calls = []

        def relocate(target, **kwargs):
            calls.append((Path(target), Path(kwargs["source_root"])))

        with patch.object(models_manager, "relocate_models", side_effect=relocate):
            worker = StorageRelocateWorker("models_dir", old_root, new_root, self.cfg)
            succeeded = []
            worker.succeeded.connect(lambda field, path: succeeded.append((field, path)))
            worker.run()
        self.assertEqual(calls, [(new_root, old_root)])
        self.assertEqual(self.cfg.models_dir, str(new_root))
        self.assertEqual(succeeded, [("models_dir", str(new_root))])

        self.cfg.models_dir = str(old_root)
        self.cfg.save_result = False
        calls.clear()
        with patch.object(models_manager, "relocate_models", side_effect=relocate):
            worker = StorageRelocateWorker("models_dir", old_root, new_root, self.cfg)
            failed = []
            worker.failed.connect(
                lambda field, message, restored, old, new: failed.append(
                    (field, message, restored, old, new)
                )
            )
            worker.run()
        self.assertEqual(calls, [(new_root, old_root), (old_root, new_root)])
        self.assertEqual(self.cfg.models_dir, str(old_root))
        self.assertTrue(failed)

    def test_voice_worker_restores_consumer_on_save_failure(self):
        old_root = self.root / "old-voices"
        new_root = self.root / "new-voices"
        self.cfg.voice_library_dir = str(old_root)
        self.cfg.save_result = False
        configured = []
        with patch.object(voices, "relocate_voice_library", return_value=new_root), \
                patch.object(voices, "configure_voice_library_dir", side_effect=lambda path: configured.append(Path(path))):
            worker = StorageRelocateWorker("voice_library_dir", old_root, new_root, self.cfg)
            failed = []
            worker.failed.connect(
                lambda field, message, restored, old, new: failed.append(
                    (field, message, restored, old, new)
                )
            )
            worker.run()
        self.assertEqual(configured, [old_root])
        self.assertEqual(self.cfg.voice_library_dir, str(old_root))
        self.assertTrue(failed)

    def test_failed_physical_rollback_marks_location_as_needing_check_without_false_claim(self):
        page = SettingsPage(self.cfg)
        old_root = self.root / "old-models"
        new_root = self.root / "new-models"
        self.cfg.models_dir = str(old_root)

        with patch("src.ui.settings_page.dialogs.alert") as alert:
            page._storage_relocation_failed(
                "models_dir",
                "磁盘恢复失败",
                False,
                str(old_root),
                str(new_root),
            )

        self.assertEqual(page.path_name_labels["models_dir"].text(), "位置需要检查")
        self.assertIn("旧位置", page.path_labels["models_dir"].text())
        self.assertIn("新位置", page.path_labels["models_dir"].text())
        message = alert.call_args.args[2]
        self.assertIn("自动恢复未完全成功", message)
        self.assertNotIn("仍使用原来的位置", message)

    def test_worker_reports_physical_rollback_failure_as_unresolved_truth(self):
        old_root = self.root / "old-models"
        new_root = self.root / "new-models"
        (old_root / "models").mkdir(parents=True)
        (old_root / "models" / "asr.bin").write_text("asr", encoding="utf-8")
        self.cfg.models_dir = str(old_root)
        self.cfg.save_result = False
        calls = []

        def relocate(target, **kwargs):
            target = Path(target)
            source = Path(kwargs["source_root"])
            calls.append((target, source))
            if len(calls) == 1:
                target.mkdir(parents=True, exist_ok=True)
                (source / "models").rename(target / "models")
            if len(calls) == 2:
                raise RuntimeError("rollback disk error")

        with patch.object(models_manager, "relocate_models", side_effect=relocate):
            worker = StorageRelocateWorker("models_dir", old_root, new_root, self.cfg)
            failed = []
            worker.failed.connect(lambda *args: failed.append(args))
            worker.run()

        self.assertEqual(calls, [(new_root, old_root), (old_root, new_root)])
        self.assertEqual(self.cfg.models_dir, str(old_root))
        self.assertEqual(failed[0][0], "models_dir")
        self.assertFalse(failed[0][2])
        self.assertEqual(failed[0][3:], (str(old_root), str(new_root)))

    def test_worker_repairs_split_layout_after_core_relocation_rollback_fails(self):
        old_root = self.root / "old-models"
        new_root = self.root / "new-models"
        (old_root / "models").mkdir(parents=True)
        (old_root / "models" / "asr.bin").write_text("asr", encoding="utf-8")
        (old_root / "tts").mkdir()
        (old_root / "tts" / "voice.bin").write_text("tts", encoding="utf-8")
        self.cfg.models_dir = str(old_root)
        calls = []

        def relocate(target, **kwargs):
            target = Path(target)
            source = Path(kwargs["source_root"])
            calls.append((target, source))
            if len(calls) == 1:
                target.mkdir(parents=True, exist_ok=True)
                (source / "models").rename(target / "models")
                raise RuntimeError("second move and core rollback both failed")
            (source / "models").rename(target / "models")

        with patch.object(models_manager, "relocate_models", side_effect=relocate):
            worker = StorageRelocateWorker("models_dir", old_root, new_root, self.cfg)
            failed = []
            worker.failed.connect(lambda *args: failed.append(args))
            worker.run()

        self.assertEqual(calls, [(new_root, old_root), (old_root, new_root)])
        self.assertTrue((old_root / "models" / "asr.bin").is_file())
        self.assertTrue((old_root / "tts" / "voice.bin").is_file())
        self.assertFalse((new_root / "models").exists())
        self.assertTrue(failed[0][2])


if __name__ == "__main__":
    unittest.main()
