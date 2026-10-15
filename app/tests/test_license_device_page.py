"""Current settings navigation keeps integrations and has no activation page."""
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from src.ui import theme as T
from src.ui.main_window import MainWindow
from src.ui.settings_page import SettingsPage


class _Cfg:
    def __init__(self, root: Path):
        self.theme = "warm"
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voices")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models")
        self.auto_update_enabled = False
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"

    def save(self):
        return True


class LicenseDevicePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_theme = T.ACTIVE.name
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        T.apply_theme(self.old_theme)
        self.tmp.cleanup()

    def test_settings_navigation_keeps_feishu_without_license_entry(self):
        settings = SettingsPage(_Cfg(Path(self.tmp.name)))
        self.assertEqual(
            settings.category_order,
            ("general", "storage", "skills", "cli", "feishu", "about"),
        )
        self.assertIn("feishu", settings.category_buttons)
        self.assertNotIn("license_device", settings.category_buttons)
        self.assertFalse(hasattr(settings, "license_device_page"))
        self.assertFalse(hasattr(settings, "license_deactivated"))
        self.assertFalse(hasattr(MainWindow, "_handle_license_deactivated"))
        settings.close()


if __name__ == "__main__":
    unittest.main()
