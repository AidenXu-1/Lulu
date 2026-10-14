import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from src.core.license.device import privacy_preserving_device_hash
from src.core.license.models import LicenseStatus
from src.core.license.redaction import redact_sensitive
from src.core.license.service import LicenseService, build_transport_from_environment
from src.core.license.store import MemorySecureStore
from src.core.license.mock_transport import MockLicenseTransport
from src.core.license.transport import HttpsLicenseTransport, MisconfiguredLicenseTransport
from src.ui import theme as T
from src.ui import license_gate as license_gate_module
from src.ui.license_gate import LicenseGateWindow


class LicenseActivationTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.now = datetime(2026, 7, 7, 8, 0, 0)
        self.transport = MockLicenseTransport(now=lambda: self.now)
        self.store = MemorySecureStore()
        self.service = LicenseService(self.store, self.transport, now=lambda: self.now)

    def _wait_until(self, predicate, *, timeout_ms=2000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for Qt state")

    def test_unauthorized_startup_locks_before_main_ui(self):
        status = self.service.startup_status()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "not_activated")
        self.assertEqual(status.low_privilege_actions, ("activate", "contact", "open_data_folder", "exit"))

    def test_mock_valid_code_activates_and_enters_on_next_start(self):
        status = self.service.activate(MockLicenseTransport.VALID_CODE)
        self.assertTrue(status.allowed)

        restart = self.service.startup_status()
        self.assertTrue(restart.allowed)
        self.assertEqual(restart.code, "allowed")
        self.assertIsNotNone(self.store.load_cache())

    def test_third_device_over_limit_does_not_write_valid_cache(self):
        status = self.service.activate(MockLicenseTransport.THIRD_DEVICE_CODE)
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "device_limit")
        self.assertIn("设备上限", status.message)
        self.assertIsNone(self.store.load_cache())

    def test_offline_grace_allows_within_7_days_but_blocks_after(self):
        self.assertTrue(self.service.activate(MockLicenseTransport.VALID_CODE).allowed)
        cache = self.store.load_cache()
        assert cache is not None
        stale = cache.to_json()
        stale["next_check_at"] = (self.now - timedelta(minutes=1)).isoformat()
        stale["grace_until"] = (self.now + timedelta(days=1)).isoformat()
        self.store.save_cache(type(cache).from_json(stale))
        self.transport.verify_mode = "network"

        within = self.service.startup_status()
        self.assertTrue(within.allowed)
        self.assertEqual(within.code, "offline_grace")

        expired = cache.to_json()
        expired["next_check_at"] = (self.now - timedelta(days=8)).isoformat()
        expired["grace_until"] = (self.now - timedelta(days=1)).isoformat()
        expired["last_local_seen_at"] = self.now.isoformat()
        self.store.save_cache(type(cache).from_json(expired))
        after = self.service.startup_status()
        self.assertFalse(after.allowed)
        self.assertEqual(after.code, "grace_expired")

    def test_time_rollback_blocks_without_deleting_cache(self):
        self.assertTrue(self.service.activate(MockLicenseTransport.VALID_CODE).allowed)
        self.now = self.now - timedelta(days=1)
        status = self.service.startup_status()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "time_anomaly")
        self.assertIsNotNone(self.store.load_cache())

    def test_revoked_and_expired_verify_lock_without_deleting_cache(self):
        self.assertTrue(self.service.activate(MockLicenseTransport.VALID_CODE).allowed)
        cache = self.store.load_cache()
        assert cache is not None
        d = cache.to_json()
        d["next_check_at"] = (self.now - timedelta(minutes=1)).isoformat()
        self.store.save_cache(type(cache).from_json(d))

        self.transport.verify_mode = "revoked"
        revoked = self.service.startup_status()
        self.assertFalse(revoked.allowed)
        self.assertEqual(revoked.code, "revoked")
        self.assertIsNotNone(self.store.load_cache())

        self.transport.verify_mode = "expired"
        expired = self.service.startup_status()
        self.assertFalse(expired.allowed)
        self.assertEqual(expired.code, "expired")
        self.assertIsNotNone(self.store.load_cache())

    def test_secure_store_failure_does_not_plaintext_downgrade(self):
        failing = LicenseService(MemorySecureStore(fail=True), self.transport, now=lambda: self.now)
        status = failing.activate(MockLicenseTransport.VALID_CODE)
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "secure_store_error")
        self.assertIn("安全存储", status.message)

    def test_redaction_covers_license_token_hash_authorization_and_worker_details(self):
        raw = (
            "license_code=LULU-MOCK-VALID lease_token=lease_mock_token "
            "device_hash=sha256:abcdef123456abcdef123456abcdef123456 "
            "Authorization: Bearer secret-token-123 D1 binding DB failed"
        )
        clean = redact_sensitive(raw)
        for leaked in ("LULU-MOCK-VALID", "lease_mock_token", "abcdef123456", "secret-token-123", "D1 binding"):
            self.assertNotIn(leaked, clean)

    def test_device_hash_has_no_raw_machine_labels(self):
        h = privacy_preserving_device_hash("stable-test-salt")
        self.assertTrue(h.startswith("sha256:"))
        self.assertNotIn("stable-test-salt", h)
        user = getpass.getuser()
        if user:
            self.assertNotIn(user, h)

    def test_lock_window_exposes_only_low_privilege_buttons(self):
        entered = []
        gate = LicenseGateWindow(self.service, on_unlocked=lambda status: entered.append(status))
        self.app.processEvents()
        labels = {button.text().strip() for button in gate.findChildren(QPushButton)}
        self.assertIn("激活 Lulu", labels)
        self.assertIn("联系授权方", labels)
        self.assertIn("打开本地数据文件夹", labels)
        self.assertIn("退出", labels)
        forbidden = {"文稿库", "输出库", "文案处理", "直播切片", "文案配音"}
        self.assertFalse(labels & forbidden)
        self.assertFalse(entered)
        gate.close()

    def test_lock_window_consumer_copy_object_tree_and_tab_order(self):
        gate = LicenseGateWindow(self.service, on_unlocked=lambda _status: None)
        self.app.processEvents()

        self.assertEqual(gate.minimumWidth(), 760)
        self.assertEqual(gate.minimumHeight(), 520)
        self.assertEqual((gate.width(), gate.height()), (820, 560))
        self.assertEqual(gate.findChild(QLabel, "licenseTitle").text(), "激活 Lulu")
        logo = gate.findChild(QLabel, "licenseLogo")
        self.assertEqual(logo.property("brandAsset"), "logo_mascot_transparent.png")
        self.assertFalse(logo.pixmap().isNull())
        logo_asset = Path(T.asset("logo_mascot_transparent.png"))
        self.assertEqual(
            hashlib.sha256(logo_asset.read_bytes()).hexdigest(),
            "af2c12d8529d11de7e935ace13fab58e4bba6b24941dd851a029fc5f7444ccbc",
        )
        for removed_copy in (
            "licenseTrustBadge",
            "licenseSub",
            "licenseInputHint",
            "licenseSecurity",
            "licenseBoundaryNote",
        ):
            self.assertIsNone(gate.findChild(QLabel, removed_copy))
        self.assertEqual(gate.activate_btn.text(), "激活 Lulu")
        self.assertEqual(gate.code.placeholderText(), "LULU-XXXX-XXXX-XXXX-XXXX")
        self.assertTrue(gate.code.isClearButtonEnabled())
        self.assertEqual(gate.code.accessibleName(), "授权码")

        def next_focusable(widget):
            candidate = widget.nextInFocusChain()
            while candidate is not widget and candidate.focusPolicy() == Qt.NoFocus:
                candidate = candidate.nextInFocusChain()
            return candidate

        self.assertIs(next_focusable(gate.code), gate.activate_btn)
        self.assertIs(next_focusable(gate.activate_btn), gate.contact_btn)
        self.assertIs(next_focusable(gate.contact_btn), gate.folder_btn)
        self.assertIs(next_focusable(gate.folder_btn), gate.exit_btn)
        gate.close()

    def test_all_locked_status_codes_map_to_expected_tones_and_unknown_is_error(self):
        gate = LicenseGateWindow(self.service, on_unlocked=lambda _status: None)
        cases = {
            "not_activated": "neutral",
            "empty_code": "warning",
            "invalid": "warning",
            "expired": "warning",
            "revoked": "warning",
            "suspended": "warning",
            "device_limit": "warning",
            "rate_limited": "warning",
            "network_unavailable": "warning",
            "time_anomaly": "error",
            "no_entitlement": "error",
            "grace_expired": "error",
            "lease_invalid": "error",
            "invalid_server_response": "error",
            "secure_store_error": "error",
            "server_error": "error",
            "future_unknown": "error",
        }
        for code, tone in cases.items():
            with self.subTest(code=code):
                gate.apply_status(LicenseStatus(False, code, "脱敏状态说明"))
                self.assertEqual(gate.status_box.property("tone"), tone)
                self.assertEqual(gate.message.text(), "脱敏状态说明")
                self.assertEqual(
                    bool(gate.code.property("invalid")),
                    code in {"empty_code", "invalid"},
                )
        gate.close()

    def test_allowed_and_offline_grace_still_unlock_without_success_page(self):
        entered = []
        gate = LicenseGateWindow(self.service, on_unlocked=lambda status: entered.append(status.code))
        gate.apply_status(LicenseStatus(True, "allowed", "授权有效。"))
        self.assertEqual(gate.status_box.property("tone"), "success")
        self.assertEqual(gate.message.text(), "授权已激活，正在进入 Lulu…")
        self._wait_until(lambda: entered == ["allowed"])

        gate = LicenseGateWindow(self.service, on_unlocked=lambda status: entered.append(status.code))
        gate.apply_status(LicenseStatus(True, "offline_grace", "离线宽限中。"))
        self.assertEqual(gate.status_box.property("tone"), "success")
        self.assertEqual(gate.message.text(), "授权已激活，正在进入 Lulu…")
        self._wait_until(lambda: entered == ["allowed", "offline_grace"])

    def test_failed_enter_activation_keeps_input_editable_and_retryable(self):
        class StubService:
            def __init__(self):
                self.calls = []

            def startup_status(self):
                return LicenseStatus(False, "not_activated", "请输入授权码。")

            def activate(self, code):
                self.calls.append(code)
                return LicenseStatus(False, "invalid", "授权码无效,请检查后重试。")

        service = StubService()
        gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
        gate.code.setText("LULU-UI-TEST")
        gate.code.returnPressed.emit()
        self._wait_until(lambda: not gate._busy)

        self.assertEqual(service.calls, ["LULU-UI-TEST"])
        self.assertEqual(gate.code.text(), "LULU-UI-TEST")
        self.assertTrue(gate.code.isEnabled())
        self.assertTrue(gate.activate_btn.isEnabled())
        self.assertEqual(gate.status_box.property("tone"), "warning")

        gate.code.setText("LULU-UI-RETRY")
        gate.activate_btn.click()
        self._wait_until(lambda: len(service.calls) == 2 and not gate._busy)
        self.assertEqual(service.calls[-1], "LULU-UI-RETRY")
        gate.close()

    def test_activation_is_non_blocking_prevents_duplicates_and_keeps_low_privilege_exits(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingService:
            def __init__(self):
                self.calls = []

            def startup_status(self):
                return LicenseStatus(False, "not_activated", "请输入授权码。")

            def activate(self, code):
                self.calls.append(code)
                started.set()
                release.wait(2)
                return LicenseStatus(False, "network_unavailable", "首次激活需要联网。")

        service = BlockingService()
        gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
        gate.show()
        self.app.processEvents()
        gate.code.setText("LULU-UI-BUSY")
        QTest.keyClick(gate.code, Qt.Key_Return)
        self._wait_until(started.is_set)

        self.assertTrue(gate._busy)
        self.assertFalse(gate.code.isEnabled())
        self.assertFalse(gate.activate_btn.isEnabled())
        self.assertEqual(gate.activate_btn.text(), "正在激活…")
        self.assertEqual(gate.status_box.property("tone"), "busy")
        for button in (gate.contact_btn, gate.folder_btn, gate.exit_btn):
            self.assertTrue(button.isEnabled())

        gate.code.returnPressed.emit()
        gate.activate_btn.click()
        self.assertEqual(service.calls, ["LULU-UI-BUSY"])

        release.set()
        self._wait_until(lambda: not gate._busy)
        self.assertEqual(service.calls, ["LULU-UI-BUSY"])
        self.assertEqual(gate.status_box.property("tone"), "warning")
        self.assertTrue(gate.code.isEnabled())
        self.assertTrue(gate.activate_btn.isEnabled())
        gate.close()

    def test_async_success_shows_success_state_then_unlocks(self):
        class SuccessService:
            def startup_status(self):
                return LicenseStatus(False, "not_activated", "请输入授权码。")

            def activate(self, _code):
                return LicenseStatus(True, "allowed", "授权有效。")

        entered = []
        gate = LicenseGateWindow(
            SuccessService(),
            on_unlocked=lambda status: entered.append(status.code),
        )
        gate.show()
        self.app.processEvents()
        gate.code.setText("LULU-UI-SUCCESS")
        QTest.keyClick(gate.code, Qt.Key_Return)
        self._wait_until(lambda: gate.status_box.property("tone") == "success")

        self.assertEqual(gate.status_box.property("tone"), "success")
        self.assertEqual(gate.message.text(), "授权已激活，正在进入 Lulu…")
        self.assertEqual(gate.activate_btn.text(), "激活成功")
        self.assertTrue(gate.isVisible())
        self._wait_until(lambda: entered == ["allowed"])
        self.assertFalse(gate.isVisible())

    def test_exit_during_activation_does_not_unlock_or_destroy_running_thread(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingSuccessService:
            def startup_status(self):
                return LicenseStatus(False, "not_activated", "请输入授权码。")

            def activate(self, _code):
                started.set()
                release.wait(2)
                return LicenseStatus(True, "allowed", "授权有效。")

        entered = []
        gate = LicenseGateWindow(
            BlockingSuccessService(),
            on_unlocked=lambda status: entered.append(status.code),
        )
        gate.show()
        self.app.processEvents()
        gate.code.setText("LULU-UI-CLOSE-BUSY")
        QTest.keyClick(gate.code, Qt.Key_Return)
        self._wait_until(started.is_set)
        worker = gate._activation_worker
        self.assertIsNotNone(worker)

        QTest.keyClick(gate, Qt.Key_Escape)
        self.app.processEvents()
        self.assertFalse(gate.isVisible())
        self.assertIs(gate._activation_worker, worker)
        release.set()
        self._wait_until(lambda: worker not in license_gate_module._RUNNING_ACTIVATIONS)

        self.assertEqual(entered, [])
        self.assertIsNone(gate._activation_worker)

    def test_paper_two_sizes_and_long_status_stay_inside_window(self):
        long_message = "授权服务暂时不可用,请稍后重试。" * 8
        for theme_name in ("warm",):
            T.apply_theme(theme_name)
            for size in ((760, 520), (820, 560)):
                with self.subTest(theme=theme_name, size=size):
                    gate = LicenseGateWindow(self.service, on_unlocked=lambda _status: None)
                    gate.resize(*size)
                    gate.code.setText("LULU-UI-TEST")
                    gate.apply_status(LicenseStatus(False, "server_error", long_message))
                    gate.show()
                    self.app.processEvents()

                    self.assertEqual(gate._consumer.name, theme_name)
                    self.assertEqual(gate.message.text(), long_message)
                    self.assertTrue(gate.message.wordWrap())
                    self.assertGreaterEqual(gate.status_box.height(), 54)
                    self.assertEqual(gate.code.text(), "LULU-UI-TEST")
                    top_left = gate.shell.mapTo(gate, QPoint(0, 0))
                    self.assertGreaterEqual(top_left.x(), 0)
                    self.assertGreaterEqual(top_left.y(), 0)
                    self.assertLessEqual(top_left.x() + gate.shell.width(), gate.width())
                    self.assertLessEqual(top_left.y() + gate.shell.height(), gate.height())
                    self.assertFalse(gate.grab().isNull())
                    gate.close()
        T.apply_theme("warm")

    def test_tab_enter_escape_keyboard_contract(self):
        gate = LicenseGateWindow(self.service, on_unlocked=lambda _status: None)
        gate.show()
        self.app.processEvents()
        gate.code.setFocus(Qt.TabFocusReason)
        QTest.keyClick(gate.code, Qt.Key_Tab)
        self.assertIs(gate.focusWidget(), gate.activate_btn)
        QTest.keyClick(gate, Qt.Key_Escape)
        self.app.processEvents()
        self.assertFalse(gate.isVisible())

    def test_license_gate_qss_covers_paper_theme_and_control_states(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "ui" / "license_gate.py").read_text(
            encoding="utf-8"
        )
        for paper_contract in (
            "T.consumer_theme(T.ACTIVE.name)",
            "PaperTextureFrame",
            "paper_canvas_opacity",
            "paper_texture_opacity",
            "surface_focal",
            "accent_hairline_active",
            'tone="busy"',
            'tone="success"',
        ):
            self.assertIn(paper_contract, source)
        for state in (":hover", ":pressed", ":disabled", ":focus"):
            self.assertIn(state, source)
        for old_warm_literal in ("#F7F3ED", "#302822", "#DF7448", "#E9DED2"):
            self.assertNotIn(old_warm_literal, source)

    def test_startup_theme_is_applied_before_workbench_and_unknown_falls_back_warm(self):
        from src import main as app_main
        from src import config
        from src.ui import theme

        with patch("src.config.AppConfig.load") as load, patch("src.ui.theme.apply_theme") as apply:
            load.return_value.theme = "dark"
            app_main._apply_startup_theme()
            apply.assert_called_once_with("warm")
            load.assert_not_called()
        main_source = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text(encoding="utf-8")
        self.assertLess(
            main_source.index("_apply_startup_theme()"),
            main_source.index("    open_main()"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with patch.object(config, "_CONFIG_PATH", config_path):
                self.assertEqual(app_main._apply_startup_theme().name, "warm")
                config_path.write_text("{broken", encoding="utf-8")
                self.assertEqual(app_main._apply_startup_theme().name, "warm")
                config_path.write_text('{"theme":"future-theme"}', encoding="utf-8")
                self.assertEqual(app_main._apply_startup_theme().name, "warm")
        theme.apply_theme("warm")

    def test_default_transport_stays_mock_without_explicit_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            transport = build_transport_from_environment()
        self.assertIsInstance(transport, MockLicenseTransport)

    def test_production_transport_requires_real_https_endpoint_and_never_falls_back_to_mock(self):
        cases = (
            {"LULU_LICENSE_ENV": "production"},
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "https://staging-license.lulu.invalid"},
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "http://license.lulu.invalid"},
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "https://mock-license.lulu.invalid"},
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "https://local-license.lulu.invalid"},
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "https://license-test.lulu.invalid"},
            {"LULU_LICENSE_TRANSPORT": "production"},
        )
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                transport = build_transport_from_environment()
                self.assertIsInstance(transport, MisconfiguredLicenseTransport)
                status = LicenseService(MemorySecureStore(), transport, now=lambda: self.now).activate("LULU-MOCK-VALID")
                self.assertFalse(status.allowed)
                self.assertEqual(status.code, "server_error")

    def test_production_transport_uses_https_when_endpoint_is_explicit(self):
        with patch.dict(
            os.environ,
            {"LULU_LICENSE_ENV": "production", "LULU_LICENSE_ENDPOINT": "https://license.lulu.invalid"},
            clear=True,
        ):
            transport = build_transport_from_environment()
        self.assertIsInstance(transport, HttpsLicenseTransport)

    def test_staging_transport_rejects_production_marker(self):
        with patch.dict(
            os.environ,
            {"LULU_LICENSE_ENV": "staging", "LULU_LICENSE_ENDPOINT": "https://production-license.lulu.invalid"},
            clear=True,
        ):
            transport = build_transport_from_environment()
        self.assertIsInstance(transport, MisconfiguredLicenseTransport)

    def test_https_transport_requires_explicit_https_endpoint(self):
        with self.assertRaises(ValueError):
            HttpsLicenseTransport("http://license.example.invalid")

    def test_https_transport_parses_success_without_admin_credentials(self):
        captured = {}

        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return (
                    b'{"status":"ok","identity_id":"anon_real","identity_kind":"anonymous",'
                    b'"license_id":"lic_real","device_id":"dev_real","entitlements":["lulu.full_access"],'
                    b'"lease_token":"lease_real_token","server_time":"2026-07-07T10:00:00",'
                    b'"next_check_at":"2026-07-08T10:00:00","grace_until":"2026-07-14T10:00:00",'
                    b'"message":"ok"}'
                )

        def fake_urlopen(request, **_kwargs):
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        with patch("src.core.license.transport.urlopen", fake_urlopen):
            result = HttpsLicenseTransport("https://license.example.invalid").activate(
                license_code="LULU-USER-CODE",
                device_hash="sha256:device",
                app_version="0.2.2",
                platform="Darwin",
            )
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.cache)
        assert result.cache is not None
        self.assertEqual(result.cache.license_id, "lic_real")
        self.assertEqual(result.cache.device_hash, "sha256:device")
        self.assertIn('"license_code"', captured["body"])
        self.assertNotIn("Authorization", captured["headers"])
        self.assertNotIn("x-admin-token", {k.lower(): v for k, v in captured["headers"].items()})

    def test_https_transport_maps_rate_limit_retry_after(self):
        class FakeHeaders(dict):
            def get(self, key, default=None):
                return super().get(key.lower(), default)

        class FakeHttpError(Exception):
            pass

        from urllib.error import HTTPError

        error = HTTPError(
            "https://license.example.invalid/v1/activate",
            429,
            "too many",
            FakeHeaders({"retry-after": "180"}),
            io.BytesIO(b'{"status":"rate_limited","message":"too many"}'),
        )

        def fake_urlopen(_request, **_kwargs):
            raise error

        with patch("src.core.license.transport.urlopen", fake_urlopen):
            result = HttpsLicenseTransport("https://license.example.invalid").activate(
                license_code="bad",
                device_hash="sha256:device",
                app_version="0.2.2",
                platform="Darwin",
            )
        self.assertEqual(result.status, "rate_limited")
        self.assertEqual(result.retry_after_seconds, 180)

    def test_https_ok_response_missing_required_fields_does_not_activate_or_cache(self):
        base_payload = {
            "status": "ok",
            "identity_id": "anon_real",
            "identity_kind": "anonymous",
            "license_id": "lic_real",
            "device_id": "dev_real",
            "entitlements": ["lulu.full_access"],
            "lease_token": "lease_real_token",
            "server_time": "2026-07-07T10:00:00",
            "next_check_at": "2026-07-08T10:00:00",
            "grace_until": "2026-07-14T10:00:00",
            "message": "ok",
        }
        missing = object()
        cases = (
            ("missing license_id", "license_id", missing),
            ("missing device_id", "device_id", missing),
            ("missing lease_token", "lease_token", missing),
            ("empty lease_token", "lease_token", ""),
            ("missing entitlements", "entitlements", missing),
            ("empty entitlements", "entitlements", []),
            ("missing full access entitlement", "entitlements", ["lulu.read_only"]),
        )

        for label, key, value in cases:
            with self.subTest(label=label):
                payload = dict(base_payload)
                if value is missing:
                    payload.pop(key, None)
                else:
                    payload[key] = value
                store = MemorySecureStore()
                service = LicenseService(
                    store,
                    HttpsLicenseTransport("https://license.example.invalid"),
                    now=lambda: self.now,
                )
                with patch("src.core.license.transport.urlopen", _urlopen_for_payload(payload)):
                    status = service.activate("LULU-USER-CODE")
                self.assertFalse(status.allowed)
                self.assertEqual(status.code, "invalid_server_response")
                self.assertIsNone(store.load_cache())
                self.assertIn("数据不完整", status.message)
                for leaked in ("license_id", "device_id", "lease_token", "entitlements", "Worker", "D1", "token"):
                    self.assertNotIn(leaked, status.message)

    def test_https_transport_network_failure_fails_closed_without_cache(self):
        def failing_urlopen(_request, **_kwargs):
            raise OSError("network down")

        store = MemorySecureStore()
        service = LicenseService(
            store,
            HttpsLicenseTransport("https://license.lulu.invalid"),
            now=lambda: self.now,
        )
        with patch("src.core.license.transport.urlopen", failing_urlopen):
            status = service.activate("LULU-USER-CODE")
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "network_unavailable")
        self.assertIsNone(store.load_cache())

    def test_https_transport_malformed_json_fails_closed_without_sensitive_details(self):
        class BadResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return b"not json"

        def bad_urlopen(_request, **_kwargs):
            return BadResponse()

        store = MemorySecureStore()
        service = LicenseService(
            store,
            HttpsLicenseTransport("https://license.lulu.invalid"),
            now=lambda: self.now,
        )
        with patch("src.core.license.transport.urlopen", bad_urlopen):
            status = service.activate("LULU-USER-CODE")
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "server_error")
        self.assertIsNone(store.load_cache())
        for leaked in ("not json", "license.lulu.invalid", "Worker", "D1", "token"):
            self.assertNotIn(leaked, status.message)

    def test_https_verify_invalid_ok_response_locks_without_deleting_existing_cache(self):
        self.assertTrue(self.service.activate(MockLicenseTransport.VALID_CODE).allowed)
        cache = self.store.load_cache()
        assert cache is not None
        stale = cache.to_json()
        stale["next_check_at"] = (self.now - timedelta(minutes=1)).isoformat()
        self.store.save_cache(type(cache).from_json(stale))

        payload = {
            "status": "ok",
            "identity_id": "anon_real",
            "identity_kind": "anonymous",
            "license_id": "lic_real",
            "device_id": "dev_real",
            "entitlements": [],
            "lease_token": "lease_real_token",
            "message": "ok",
        }
        service = LicenseService(
            self.store,
            HttpsLicenseTransport("https://license.example.invalid"),
            now=lambda: self.now,
        )
        with patch("src.core.license.transport.urlopen", _urlopen_for_payload(payload)):
            status = service.startup_status()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "invalid_server_response")
        kept = self.store.load_cache()
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual(kept.license_id, cache.license_id)

    def test_worker_source_contains_p1_controls_without_real_secrets(self):
        base = Path(__file__).resolve().parents[1] / "workers" / "license"
        worker = (base / "worker.js").read_text(encoding="utf-8")
        migration = (base / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
        admin = (base / "tools" / "admin.mjs").read_text(encoding="utf-8")
        readme = (base / "README.md").read_text(encoding="utf-8")

        for required in (
            "lease_tokens",
            "token_hash",
            "revokeActiveLeases",
            "recordFailure",
            "rate_limits",
            "ADMIN_TOKEN",
            "ADMIN_WEB_DEV_IDENTITY",
            "LULU_LICENSE_ENV",
            "adminResetDevice",
            "adminNotFound",
            "auditStatement",
            "env.DB.batch(statements)",
            "Lulu 授权码管理后台",
            "license_id",
            "plaintext code was not stored",
            "一次性 CSV 台账",
            "recipient_name",
            "active_device_count",
            "ADMIN_NO_STORE_HEADERS",
            "no-store",
            "x-lulu-admin-csrf",
            "same-origin",
            "allowedAdminOrigin",
            "给谁",
            "联系方式",
            "批次",
            'existing.status === "revoked"',
            "activate_reactivated",
        ):
            self.assertIn(required, worker)
        self.assertNotIn("ADMIN_ALLOWED_EMAILS", worker)
        self.assertNotIn('request.headers.get("cf-access-authenticated-user-email")', worker)
        self.assertNotIn(
            '["status", "expires_at", "max_devices", "note",',
            worker,
        )
        self.assertIn("CREATE TABLE IF NOT EXISTS lease_tokens", migration)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_leases_license_device_status", migration)
        self.assertIn("LULU_LICENSE_ADMIN_TOKEN", admin)
        forbidden_literals = ("sk-", "cf_", "Bearer ", "LULU-REAL", "cloudflare.com/client/v4/accounts/")
        combined = "\n".join((worker, migration, admin))
        for literal in forbidden_literals:
            self.assertNotIn(literal, combined)
        self.assertIn("RATE_LIMIT_PEPPER", worker)
        self.assertIn("RATE_LIMIT_PEPPER", readme)
        self.assertIn("cf-connecting-ip", worker)
        self.assertNotIn('request.headers.get("x-forwarded-for")', worker)
        self.assertIn(":sha256:", worker)
        self.assertIn("Refusing --code", admin)
        self.assertIn("--code-stdin", admin)
        self.assertNotIn("--code <one-time-code>", admin)
        for unsafe in (
            "printf '%s' '<one-time-code>'",
            "printf '%s' 'not-a-real-code'",
            "<one-time-code>",
        ):
            self.assertNotIn(unsafe, admin)
            self.assertNotIn(unsafe, readme)
        self.assertIn("License code (hidden):", admin)
        self.assertIn("--code-file", admin)
        self.assertIn("owner-only permissions", admin)
        self.assertIn("hidden prompt", readme)
        self.assertIn("GET /admin", readme)
        self.assertNotIn("lulu_admin_token", worker)
        self.assertNotIn("sessionStorage", worker)
        self.assertIn("Browser JavaScript must not store or send the long-lived admin token", readme)


class _PayloadResponse:
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _urlopen_for_payload(payload):
    def fake_urlopen(_request, **_kwargs):
        return _PayloadResponse(payload)

    return fake_urlopen


if __name__ == "__main__":
    unittest.main()
