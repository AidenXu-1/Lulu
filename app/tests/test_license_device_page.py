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

    def test_dynamic_summary_uses_server_values_without_remote_device_details(self):
        for active, maximum in ((0, 1), (2, 5), (7, 12)):
            with self.subTest(active=active, maximum=maximum):
                service = _ScriptedService()
                page = self._ready_page(service, active=active, maximum=maximum)
                visible = self._visible_copy(page)
                self.assertIn(f"当前授权已使用 {active}/{maximum} 台设备", visible)
                self.assertNotIn("设备名称", visible)
                self.assertNotIn("设备标识", visible)
                self.assertNotIn("踢下线", visible)
                self.assertNotIn("替换设备", visible)
                page.close()

    def test_legacy_page_refresh_has_only_one_danger_action(self):
        service = _ScriptedService(
            refresh=[
                _status(
                    "ready",
                    "第一次刷新。",
                    allowed=True,
                    active=4,
                    maximum=5,
                ),
                _status(
                    "ready",
                    "第二次刷新。",
                    allowed=True,
                    active=3,
                    maximum=5,
                ),
            ]
        )
        page = LicenseDevicePage(service)
        page.on_activated()
        self._wait(page)
        self.assertEqual(service.calls, ["refresh"])
        self.assertEqual(page.usage_label.text(), "当前授权已使用 4/5 台设备")
        self.assertEqual(
            [button.text() for button in self._shown_actions(page)],
            ["停用这台设备"],
        )
        self.assertIsInstance(page.deactivate_button, LuluButton)
        self.assertEqual(page.deactivate_button.property("role"), "destructive")
        self.assertTrue(page.recovery_button.isHidden())

        page.on_activated()
        self._wait(page)
        self.assertEqual(service.calls, ["refresh", "refresh"])
        self.assertEqual(page.usage_label.text(), "当前授权已使用 3/5 台设备")
        self.assertEqual(
            [button.text() for button in self._shown_actions(page)],
            ["停用这台设备"],
        )
        page.close()

    def test_ui_layer_has_no_secret_network_cache_or_file_mutation_implementation(self):
        source = Path(license_device_page_module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "keyring",
            "urlopen(",
            "HttpsLicenseTransport",
            "license_code",
            "device_id",
            "device_hash",
            "lease_token",
            ".clear_cache(",
            ".unlink(",
            ".remove(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_offline_hides_stale_summary_and_never_enables_deactivation(self):
        service = _ScriptedService(
            refresh=[
                _status(
                    "network_unavailable",
                    "连接授权服务后才能刷新本机设备状态。",
                )
            ]
        )
        page = LicenseDevicePage(service)
        page.on_activated()
        self._wait(page)
        self.assertEqual(page.state, "offline")
        self.assertEqual(page.usage_label.text(), "使用名额：联网后显示")
        self.assertFalse(page.deactivate_button.isEnabled())
        self.assertTrue(page.deactivate_button.isHidden())
        self.assertFalse(page.recovery_button.isHidden())
        self.assertTrue(page.recovery_button.isEnabled())
        self.assertEqual(page.recovery_button.text(), "重新连接并检查")
        self.assertEqual(self._shown_actions(page), [page.recovery_button])
        self.assertEqual(service.calls, ["refresh"])
        page.request_deactivation()
        self.assertEqual(service.calls, ["refresh"])
        page.close()

    def test_cancelled_confirmation_submits_nothing_and_explains_file_safety(self):
        service = _ScriptedService()
        page = self._ready_page(service)
        with patch(
            "src.ui.license_device_page.dialogs.confirm",
            return_value=False,
        ) as confirm:
            page.request_deactivation()
        self.assertEqual(service.calls, ["refresh"])
        self.assertEqual(page.state, "ready")
        body = confirm.call_args.args[2]
        self.assertIn("本机文件不会被删除", body)
        self.assertIn("返回激活页", body)
        page.close()

    def test_only_committed_stopped_result_returns_to_activation_gate(self):
        service = _ScriptedService(
            deactivate=[
                _status(
                    "stopped",
                    "本机已停用。",
                    active=1,
                    maximum=5,
                    deactivation_status="stopped",
                )
            ]
        )
        page = self._ready_page(service, active=2, maximum=5)
        emitted = []
        page.deactivated.connect(emitted.append)
        with patch("src.ui.license_device_page.dialogs.confirm", return_value=True):
            page.request_deactivation()
        self._wait(page)
        QTest.qWait(1)
        self.app.processEvents()
        self.assertEqual(page.state, "stopped")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(service.calls, ["refresh", "deactivate"])
        self.assertFalse(page.deactivate_button.isEnabled())
        self.assertIn("授权缓存已清理", page.message_label.text())
        page.close()

    def test_failure_unknown_and_cleanup_required_never_emit_success(self):
        cases = (
            (
                _status(
                    "lease_invalid",
                    "授权凭证已失效。",
                    deactivation_status="failed",
                ),
                "failed",
                "重新尝试停用本机",
            ),
            (
                _status(
                    "outcome_unknown",
                    "本机停用请求的结果正在确认。",
                    deactivation_status="resolving",
                ),
                "outcome_unknown",
                "继续确认停用结果",
            ),
            (
                _status(
                    "local_cleanup_incomplete",
                    "本机授权已在服务端停用，但本地授权状态未完全清理。",
                    active=0,
                    maximum=1,
                    deactivation_status="cleanup_required",
                ),
                "cleanup_required",
                "重试清理并返回激活页",
            ),
        )
        for result, expected_state, expected_button in cases:
            with self.subTest(state=expected_state):
                service = _ScriptedService(deactivate=[result])
                page = self._ready_page(service)
                emitted = []
                page.deactivated.connect(emitted.append)
                with patch("src.ui.license_device_page.dialogs.confirm", return_value=True):
                    page.request_deactivation()
                self._wait(page)
                self.assertEqual(page.state, expected_state)
                self.assertEqual(emitted, [])
                button = (
                    page.deactivate_button
                    if expected_state == "failed"
                    else page.recovery_button
                )
                self.assertEqual(button.text(), expected_button)
                self.assertEqual(self._shown_actions(page), [button])
                page.close()

    def test_unknown_retry_uses_resume_and_never_creates_a_second_deactivation(self):
        service = _ScriptedService(
            deactivate=[
                _status(
                    "outcome_unknown",
                    "正在确认。",
                    deactivation_status="resolving",
                )
            ],
            resume=[
                _status(
                    "stopped",
                    "本机已停用。",
                    active=0,
                    maximum=2,
                    deactivation_status="stopped",
                )
            ],
        )
        page = self._ready_page(service)
        with patch("src.ui.license_device_page.dialogs.confirm", return_value=True):
            page.request_deactivation()
        self._wait(page)
        self.assertEqual(page.state, "outcome_unknown")
        page.retry_current_state()
        self._wait(page)
        QTest.qWait(1)
        self.assertEqual(service.calls, ["refresh", "deactivate", "resume"])
        page.close()

    def test_busy_state_rejects_duplicate_clicks_and_worker_is_collected(self):
        service = _ScriptedService(
            deactivate=[
                _status(
                    "outcome_unknown",
                    "正在确认。",
                    deactivation_status="resolving",
                )
            ],
            delay=0.08,
        )
        page = self._ready_page(service)
        with patch("src.ui.license_device_page.dialogs.confirm", return_value=True) as confirm:
            page.request_deactivation()
            page.request_deactivation()
        self._wait(page)
        self.assertEqual(confirm.call_count, 1)
        self.assertEqual(service.calls.count("deactivate"), 1)
        self.assertIsNone(page._worker)
        page.close()

    def test_loading_uses_lulu_button_contract_and_keeps_one_action(self):
        service = _ScriptedService(
            refresh=[
                _status(
                    "ready",
                    "本机授权状态已刷新。",
                    allowed=True,
                    active=4,
                    maximum=5,
                )
            ],
            deactivate=[
                _status(
                    "lease_invalid",
                    "本机仍保持授权。",
                    deactivation_status="failed",
                )
            ],
            delay=0.08,
        )
        page = LicenseDevicePage(service)
        page.show()
        page.on_activated()
        self.app.processEvents()
        self.assertEqual(page.state, "loading")
        self.assertEqual(self._shown_actions(page), [page.recovery_button])
        self.assertEqual(page.recovery_button.property("state"), "loading")
        self.assertFalse(page.recovery_button.isEnabled())
        self._wait(page)
        self.assertEqual(page.state, "ready")
        self.assertEqual(page.recovery_button.property("state"), "default")

        with patch("src.ui.license_device_page.dialogs.confirm", return_value=True):
            page.request_deactivation()
        self.app.processEvents()
        self.assertEqual(page.state, "submitting")
        self.assertEqual(self._shown_actions(page), [page.deactivate_button])
        self.assertEqual(page.deactivate_button.property("state"), "loading")
        self.assertFalse(page.deactivate_button.isEnabled())
        self._wait(page)
        self.assertEqual(page.state, "failed")
        self.assertEqual(page.deactivate_button.property("state"), "default")
        page.close()

    def test_paper_and_two_target_sizes_have_no_horizontal_scroll(self):
        for theme_name in ("warm",):
            for size in ((1440, 900), (1200, 760)):
                with self.subTest(theme=theme_name, size=size):
                    T.apply_theme(theme_name)
                    service = _ScriptedService(
                        refresh=[
                            _status(
                                "ready",
                                "本机授权状态已刷新。",
                                allowed=True,
                                active=4,
                                maximum=9,
                            )
                        ]
                    )
                    settings = SettingsPage(_Cfg(Path(self.tmp.name)))
                    page = LicenseDevicePage(service)
                    page.resize(*size)
                    page.show()
                    page.on_activated()
                    self._wait(page)
                    self.app.processEvents()
                    self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
                    self.assertTrue(page.deactivate_button.isVisible())
                    self.assertFalse(page.recovery_button.isVisible())
                    self.assertEqual(self._shown_actions(page), [page.deactivate_button])
                    self.assertIsInstance(page.deactivate_button, LuluButton)
                    self.assertIsInstance(page.recovery_button, LuluButton)
                    self.assertEqual(page.deactivate_button.height(), 34)
                    self.assertEqual(page.recovery_button.height(), 34)
                    self.assertEqual(page.deactivate_button.property("role"), "destructive")
                    self.assertEqual(page.recovery_button.property("role"), "secondary")
                    consumer = T.consumer_theme(theme_name)
                    action_qss = page.styleSheet()
                    settings_qss = settings.styleSheet()
                    for token in (
                        consumer.danger,
                        consumer.secondary_hover,
                        consumer.accent_wash,
                        consumer.accent_hairline_active,
                        consumer.surface_work,
                        consumer.ink_muted,
                        consumer.line,
                    ):
                        self.assertIn(token, action_qss)
                    for pseudo_state in (
                        ":hover",
                        ":pressed",
                        ":focus",
                        ":disabled",
                        '[state="loading"]',
                    ):
                        self.assertIn(pseudo_state, action_qss)
                    self.assertIn(
                        f"border-radius: {consumer.radius_control}px",
                        action_qss,
                    )
                    self.assertIn(
                        f"background: {consumer.secondary}",
                        settings_qss,
                    )
                    page._show_offline("连接授权服务后才能停用本机。")
                    self.app.processEvents()
                    self.assertEqual(self._shown_actions(page), [page.recovery_button])
                    self.assertEqual(page.recovery_button.text(), "重新连接并检查")
                    self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
                    self.assertGreaterEqual(page.summary_card.accessibleName().find("本机"), 0)
                    self.assertNotIn("license_device", settings._all_category_order)
                    page.close()
                    settings.close()

    def test_keyboard_order_and_accessible_names_are_explicit(self):
        service = _ScriptedService(
            refresh=[
                _status(
                    "ready",
                    "本机授权状态已刷新。",
                    allowed=True,
                    active=2,
                    maximum=5,
                )
            ]
        )
        page = self._ready_page(service)
        page.resize(900, 700)
        page.show()
        self.app.processEvents()
        page.summary_card.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        QTest.keyClick(page.summary_card, Qt.Key_Tab)
        self.assertTrue(page.deactivate_button.hasFocus())
        self.assertTrue(page.recovery_button.isHidden())
        self.assertEqual(
            page.deactivate_button.accessibleName(),
            "停用这台设备，需要二次确认",
        )
        self.assertIn("本机已授权", page.summary_card.accessibleDescription())
        with patch("src.ui.license_device_page.dialogs.confirm", return_value=False) as confirm:
            page.deactivate_button.setFocus()
            QTest.keyClick(page.deactivate_button, Qt.Key_Space)
        confirm.assert_called_once()
        page.close()

        retry_service = _ScriptedService(
            refresh=[
                _status("network_unavailable", "暂时无法联网。"),
                _status(
                    "ready",
                    "已经恢复连接。",
                    allowed=True,
                    active=2,
                    maximum=5,
                ),
            ]
        )
        retry_page = LicenseDevicePage(retry_service)
        retry_page.resize(900, 700)
        retry_page.show()
        retry_page.on_activated()
        self._wait(retry_page)
        retry_page.summary_card.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(retry_page.summary_card, Qt.Key_Tab)
        self.assertTrue(retry_page.recovery_button.hasFocus())
        self.assertEqual(
            retry_page.recovery_button.accessibleName(),
            "重新连接并检查",
        )
        QTest.keyClick(retry_page.recovery_button, Qt.Key_Return)
        self._wait(retry_page)
        self.assertEqual(retry_service.calls, ["refresh", "refresh"])
        retry_page.close()

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
