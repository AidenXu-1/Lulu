from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from src.core.license.models import LicenseStatus
from src import main as app_main
from src.ui import theme as T
from src.ui.control_foundation import LuluButton
from src.ui import license_device_page as license_device_page_module
from src.ui.license_device_page import LicenseDevicePage
from src.ui.main_window import MainWindow
from src.ui.settings_page import SettingsPage


def _status(
    code: str,
    message: str,
    *,
    allowed: bool = False,
    active: int | None = None,
    maximum: int | None = None,
    deactivation_status: str | None = None,
) -> LicenseStatus:
    return LicenseStatus(
        allowed,
        code,
        message,
        active_device_count=active,
        max_devices=maximum,
        deactivation_status=deactivation_status,
    )


class _ScriptedService:
    def __init__(self, *, refresh=(), deactivate=(), resume=(), delay: float = 0.0):
        self.steps = {
            "refresh": list(refresh),
            "deactivate": list(deactivate),
            "resume": list(resume),
        }
        self.calls: list[str] = []
        self.delay = delay

    def _next(self, action: str):
        self.calls.append(action)
        if self.delay:
            time.sleep(self.delay)
        step = self.steps[action].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def refresh_device_summary(self):
        return self._next("refresh")

    def deactivate_current_device(self):
        return self._next("deactivate")

    def resume_pending_deactivation(self):
        return self._next("resume")


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

    def _wait(self, page: LicenseDevicePage, timeout_ms: int = 2000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while page.operation_active() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.app.processEvents()
        self.assertFalse(page.operation_active(), "license device worker did not finish")

    @staticmethod
    def _visible_copy(widget) -> str:
        return " ".join(label.text() for label in widget.findChildren(QLabel))

    @staticmethod
    def _shown_actions(page: LicenseDevicePage) -> list[QPushButton]:
        return [
            button
            for button in (page.deactivate_button, page.recovery_button)
            if not button.isHidden()
        ]

    def _ready_page(self, service: _ScriptedService, *, active=1, maximum=5):
        service.steps["refresh"].insert(
            0,
            _status(
                "ready",
                "本机授权状态已刷新。",
                allowed=True,
                active=active,
                maximum=maximum,
            ),
        )
        page = LicenseDevicePage(service)
        page.on_activated()
        self._wait(page)
        self.assertEqual(page.state, "ready")
        return page


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
