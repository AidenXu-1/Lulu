import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from src.core.updater.models import UpdateSnapshot
from src.core.user_notifications import AuthorizationStatus, PermissionResult
from src.ui import theme as T
from src.ui.consumer_select import ConsumerSelect
from src.ui.settings_page import AuthorContactDialog, SettingsPage


class _Controller(QObject):
    changed = Signal(object)
    install_accepted = Signal()

    def __init__(self):
        super().__init__()
        self.snapshot = UpdateSnapshot(current_version="0.2.4", current_build="20260712.1")
        self.check_calls = []

    def check(self, *, manual):
        self.check_calls.append(bool(manual))

    def download(self):
        return None

    def cancel(self):
        return None

    def install_gate(self):
        return "ok"

    def publish(self):
        self.changed.emit(self.snapshot)

    def shutdown(self, _wait_ms=0):
        return True


class _NotificationService:
    def __init__(self):
        self.callbacks = []
        self.raise_on_request = False

    def request_enable(self, callback):
        if self.raise_on_request:
            raise RuntimeError("native bridge unavailable")
        self.callbacks.append(callback)

    def answer(self, *, granted, status, reason):
        self.callbacks.pop(0)(PermissionResult(granted, status, reason))


class _Cfg:
    def __init__(self, root: Path):
        self.theme = "warm"
        self.last_output_dir = str(root / "exports")
        self.media_dir = str(root / "media")
        self.recording_dir = str(root / "recordings")
        self.tts_output_dir = str(root / "voices")
        self.voice_library_dir = str(root / "voice-library")
        self.models_dir = str(root / "models")
        self.auto_update_enabled = True
        self.notification_policy = "never"
        self.notify_on_failure = True
        self.notification_sound_enabled = True
        self.prevent_sleep_during_tasks = False
        self.confirm_exit_during_tasks = True
        self.completion_action = "stay"
        self.save_result = True
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        return self.save_result


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _Cfg(self.root)
        self.notifications = _NotificationService()
        self.controller = _Controller()
        # Keep every settings/shell probe away from the real profile and library.
        from src.core import library, models_manager
        from src.core.agent_workspace import skills
        for boundary in (
            patch.object(Path, "home", return_value=self.root/"home"),
            patch.object(library, "LIBRARY_DIR", self.root/"library"),
            patch.object(library, "FOLDERS_FILE", self.root/"library/folders.json"),
            patch.object(models_manager, "app_data_dir", return_value=self.root),
            patch.object(skills, "skill_root", return_value=self.root/"skills"),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def tearDown(self):
        T.apply_theme("warm")
        self.tmp.cleanup()

    def _page(self, notification_service=True, *, external_storage_busy=None):
        return SettingsPage(
            self.cfg,
            update_controller=self.controller,
            notification_service=self.notifications if notification_service else None,
            external_storage_busy=external_storage_busy,
        )

    def _wait_skill(self, page):
        limit = time.monotonic() + 2
        while page.agent_skill_state == "checking" and time.monotonic() < limit:
            QTest.qWait(5)
        self.assertNotEqual(page.agent_skill_state, "checking")

    @staticmethod
    def _visible_copy(widget) -> str:
        return " ".join(label.text() for label in widget.findChildren(QLabel))

    def test_legacy_settings_survive_font_and_independent_notice_preferences(self):
        import json
        from src import config

        legacy = {
            "notification_policy": "always", "notify_on_failure": False,
            "notification_sound_enabled": False, "prevent_sleep_during_tasks": True,
            "confirm_exit_during_tasks": False, "completion_action": "open_result",
            "media_dir": str(self.root / "legacy-media"), "voice_library_dir": str(self.root / "voices"),
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        with patch.object(config, "_CONFIG_PATH", path), patch.object(config, "_CONFIG_DIR", self.root), \
                patch("src.core.secrets.get_config", return_value={}):
            cfg = config.AppConfig.load()
            page = SettingsPage(cfg, notification_service=self.notifications, lazy_categories=True)
            self.addCleanup(page.close)
            page.resize(620, 660)
            page.show()
            self.assertEqual(page.font_slider.value(), 100)
            QTest.keyClick(page.font_slider, Qt.Key_End)
            self.assertEqual(page.font_percent.text(), "130%")
            QTest.qWait(220)
            self.assertEqual(config.AppConfig.load().interface_font_percent, 130)
            for key in page.category_order:
                page.set_category(key)
                page.resize(620, 660)
                self.app.processEvents()
                self.assertEqual(page.width(), 620, key)
                self.assertGreater(page.category_stack.currentWidget().height(), 150)
            page.set_category("general")
            page.notification_events["notify_on_completion"].click()
            page.notification_events["notify_on_attention"].click()
            page.notification_select.set_value("never", emit=True)
            self.assertTrue(all(not check.isEnabled() for check in page.notification_events.values()))
            again = config.AppConfig.load()
            for key, value in legacy.items():
                if key != "notification_policy":
                    self.assertEqual(getattr(again, key), value)
            self.assertFalse(again.notify_on_completion)
            self.assertTrue(again.notify_on_attention)
            self.assertEqual(again.notification_policy, "never")
            page.shutdown_workers()


if __name__ == "__main__":
    unittest.main()
