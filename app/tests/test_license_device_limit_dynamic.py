from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src.core.license._service_core import LicenseService
from src.core.license.models import (
    DeviceLimitSummary,
    FULL_ACCESS,
    LicenseResult,
    MAX_DEVICE_SUMMARY_INTEGER,
)
from src.core.license.store import MemorySecureStore
from src.core.license.transport import (
    HttpsLicenseTransport,
    LicenseTransport,
    LicenseTransportError,
)


NOW = datetime(2026, 7, 28, 12, 0, 0)


class _Response:
    headers = {}

    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.raw


def _device_limit_payload(active: object, maximum: object) -> dict:
    return {
        "status": "device_limit",
        "message": "untrusted upstream wording",
        "active_device_count": active,
        "max_devices": maximum,
    }


def _urlopen_json(payload: dict):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return lambda *_args, **_kwargs: _Response(raw)


class _ActivationTransport(LicenseTransport):
    def __init__(self, result: LicenseResult):
        self.result = result

    def activate(self, **_kwargs) -> LicenseResult:
        return self.result

    def verify(self, **_kwargs) -> LicenseResult:
        raise AssertionError("verify is outside this test")

    def deactivate(self, **_kwargs) -> LicenseResult:
        raise AssertionError("deactivate is outside this test")


class LicenseDeviceLimitDynamicTests(unittest.TestCase):
    def _transport_result(self, active: object, maximum: object) -> LicenseResult:
        with patch(
            "src.core.license.transport.urlopen",
            _urlopen_json(_device_limit_payload(active, maximum)),
        ):
            return HttpsLicenseTransport(
                "https://license.example.invalid"
            ).activate(
                license_code="LULU-TEST-CODE",
                device_hash="sha256:fixture",
                app_version="0.2.2",
                platform="Darwin",
            )

    def test_structured_summary_accepts_dynamic_limits_and_overfull_truth(self):
        for active, maximum in (
            (1, 1),
            (2, 2),
            (5, 5),
            (17, 17),
            (18, 17),
            (101, 257),
        ):
            with self.subTest(active=active, maximum=maximum):
                summary = DeviceLimitSummary.from_values(active, maximum)
                self.assertEqual(summary.fraction, f"{active}/{maximum}")
                result = self._transport_result(active, maximum)
                self.assertEqual(result.status, "device_limit")
                self.assertEqual(
                    (result.active_device_count, result.max_devices),
                    (active, maximum),
                )

    def test_service_builds_dynamic_gate_copy_from_fields_not_server_message(self):
        for active, maximum in ((1, 1), (2, 2), (5, 5), (17, 17), (18, 17)):
            with self.subTest(active=active, maximum=maximum):
                result = LicenseResult(
                    "device_limit",
                    "server says max=2 and lease_token=secret",
                    active_device_count=active,
                    max_devices=maximum,
                )
                service = LicenseService(
                    MemorySecureStore(),
                    _ActivationTransport(result),
                    now=lambda: NOW,
                )
                status = service.activate("LULU-TEST-CODE")
                self.assertFalse(status.allowed)
                self.assertEqual(status.code, "device_limit")
                self.assertIn(f"{active}/{maximum}", status.message)
                self.assertIn("设置 > 授权与设备", status.message)
                self.assertIn("停用本机", status.message)
                self.assertIn("旧设备已无法使用", status.message)
                self.assertIn("联系授权方", status.message)
                self.assertNotIn("lease_token", status.message)
                self.assertIsNone(service.store.load_cache())

    def test_missing_or_invalid_device_limit_fields_fail_closed(self):
        invalid_cases = (
            ("missing count", _device_limit_payload(1, 2), "active_device_count"),
            ("missing max", _device_limit_payload(1, 2), "max_devices"),
            ("bool count", _device_limit_payload(True, 2), None),
            ("bool max", _device_limit_payload(1, False), None),
            ("zero max", _device_limit_payload(0, 0), None),
            ("negative count", _device_limit_payload(-1, 2), None),
            ("negative max", _device_limit_payload(0, -2), None),
            ("float count", _device_limit_payload(1.0, 2), None),
            ("float max", _device_limit_payload(1, 2.0), None),
            ("string count", _device_limit_payload("1", 2), None),
            ("string max", _device_limit_payload(1, "2"), None),
            (
                "count over safe integer",
                _device_limit_payload(MAX_DEVICE_SUMMARY_INTEGER + 1, 2),
                None,
            ),
            (
                "max over safe integer",
                _device_limit_payload(1, MAX_DEVICE_SUMMARY_INTEGER + 1),
                None,
            ),
        )
        for label, payload, removed in invalid_cases:
            with self.subTest(label=label):
                if removed is not None:
                    payload.pop(removed)
                with patch(
                    "src.core.license.transport.urlopen",
                    _urlopen_json(payload),
                ):
                    result = HttpsLicenseTransport(
                        "https://license.example.invalid"
                    ).activate(
                        license_code="LULU-TEST-CODE",
                        device_hash="sha256:fixture",
                        app_version="0.2.2",
                        platform="Darwin",
                    )
                self.assertEqual(result.status, "invalid_server_response")
                self.assertIsNone(result.active_device_count)
                self.assertIsNone(result.max_devices)

    def test_duplicate_conflicting_summary_fields_are_rejected(self):
        raw = (
            b'{"status":"device_limit","active_device_count":2,'
            b'"max_devices":2,"max_devices":5}'
        )
        with patch(
            "src.core.license.transport.urlopen",
            lambda *_args, **_kwargs: _Response(raw),
        ), self.assertRaises(LicenseTransportError):
            HttpsLicenseTransport("https://license.example.invalid").activate(
                license_code="LULU-TEST-CODE",
                device_hash="sha256:fixture",
                app_version="0.2.2",
                platform="Darwin",
            )

    def test_regular_success_keeps_optional_device_summary_compatibility(self):
        payload = {
            "status": "ok",
            "identity_id": "anon",
            "identity_kind": "anonymous",
            "license_id": "lic",
            "device_id": "dev",
            "entitlements": [FULL_ACCESS],
            "lease_token": "lease",
            "server_time": NOW.isoformat(),
            "next_check_at": (NOW + timedelta(days=1)).isoformat(),
            "grace_until": (NOW + timedelta(days=7)).isoformat(),
        }
        with patch(
            "src.core.license.transport.urlopen",
            _urlopen_json(payload),
        ):
            result = HttpsLicenseTransport(
                "https://license.example.invalid"
            ).activate(
                license_code="LULU-TEST-CODE",
                device_hash="sha256:fixture",
                app_version="0.2.2",
                platform="Darwin",
            )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.active_device_count)
        self.assertIsNone(result.max_devices)


if __name__ == "__main__":
    unittest.main()
