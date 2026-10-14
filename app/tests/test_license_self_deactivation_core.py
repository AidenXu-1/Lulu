import json
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src.core.license._service_core import LicenseService
from src.core.license.models import (
    FULL_ACCESS,
    LicenseCache,
    LicenseResult,
    PendingDeactivation,
)
from src.core.license.redaction import redact_sensitive
from src.core.license.store import (
    CACHE_KEY,
    PENDING_DEACTIVATION_KEY,
    MemorySecureStore,
    SecureStoreError,
)
from src.core.license.transport import (
    HttpsLicenseTransport,
    LicenseTransport,
    NetworkUnavailable,
)


NOW = datetime(2026, 7, 28, 9, 0, 0)


def _cache(*, device_id: str = "dev_current") -> LicenseCache:
    return LicenseCache(
        schema_version=1,
        identity_id="anon_current",
        identity_kind="anonymous",
        license_id="lic_current",
        device_id=device_id,
        device_hash="sha256:abcdef123456abcdef123456abcdef123456abcdef123456abcdef123456",
        entitlements=(FULL_ACCESS,),
        lease_token="lease_current_secret",
        last_verified_at=NOW,
        next_check_at=NOW + timedelta(days=1),
        grace_until=NOW + timedelta(days=7),
        last_local_seen_at=NOW,
    )


def _ok_result(
    cache: LicenseCache,
    *,
    active_device_count: int = 1,
    max_devices: int = 2,
    request_id: str | None = None,
    replayed: bool = False,
) -> LicenseResult:
    return LicenseResult(
        "ok",
        "本机已停用，设备名额已释放。",
        cache=cache,
        active_device_count=active_device_count,
        max_devices=max_devices,
        request_id=request_id,
        deactivation_status="stopped",
        replayed=replayed,
    )


class _ScriptedTransport(LicenseTransport):
    def __init__(self, *, verify_result: LicenseResult | None = None, deactivation_steps=()):
        self.verify_result = verify_result
        self.deactivation_steps = list(deactivation_steps)
        self.verify_calls = 0
        self.deactivate_calls: list[str] = []

    def activate(self, *, license_code: str, device_hash: str, app_version: str, platform: str) -> LicenseResult:
        raise AssertionError("activation is outside this test")

    def verify(self, *, cache: LicenseCache, app_version: str) -> LicenseResult:
        self.verify_calls += 1
        if self.verify_result is None:
            raise AssertionError("unexpected verify")
        return self.verify_result

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        self.deactivate_calls.append(request_id)
        if not self.deactivation_steps:
            raise AssertionError("unexpected deactivate")
        step = self.deactivation_steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _BlockingTransport(_ScriptedTransport):
    def __init__(self, result: LicenseResult):
        super().__init__()
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        self.deactivate_calls.append(request_id)
        self.started.set()
        if not self.release.wait(2):
            raise AssertionError("test did not release transport")
        return self.result


class _DeleteFailureStore(MemorySecureStore):
    def __init__(self):
        super().__init__()
        self.fail_cache_delete = True

    def delete_text(self, key: str) -> None:
        if key == CACHE_KEY and self.fail_cache_delete:
            raise SecureStoreError("模拟本地授权缓存清理失败")
        super().delete_text(key)


class _PendingDeleteFailureStore(MemorySecureStore):
    def __init__(self):
        super().__init__()
        self.fail_pending_delete = True

    def delete_text(self, key: str) -> None:
        if key == PENDING_DEACTIVATION_KEY and self.fail_pending_delete:
            raise SecureStoreError("模拟停用恢复标记清理失败")
        super().delete_text(key)


class _SilentCacheDeleteStore(MemorySecureStore):
    def delete_text(self, key: str) -> None:
        if key == CACHE_KEY:
            return
        super().delete_text(key)


class _Response:
    headers = {}

    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _urlopen_payload(payload: dict, captured: dict | None = None):
    def fake(request, **_kwargs):
        if captured is not None:
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.header_items())
        return _Response(payload)

    return fake


class LicenseTransportDeviceSummaryTests(unittest.TestCase):
    def _activation_payload(self, active: int, maximum: int) -> dict:
        return {
            "status": "ok",
            "identity_id": "anon_real",
            "identity_kind": "anonymous",
            "license_id": "lic_real",
            "device_id": "dev_real",
            "entitlements": [FULL_ACCESS],
            "lease_token": "lease_real",
            "server_time": NOW.isoformat(),
            "next_check_at": (NOW + timedelta(days=1)).isoformat(),
            "grace_until": (NOW + timedelta(days=7)).isoformat(),
            "active_device_count": active,
            "max_devices": maximum,
        }

    def test_https_transport_parses_dynamic_1_2_5_and_higher_limits(self):
        transport = HttpsLicenseTransport("https://license.example.invalid")
        for active, maximum in ((0, 1), (1, 2), (4, 5), (11, 27), (29, 27)):
            with self.subTest(active=active, maximum=maximum), patch(
                "src.core.license.transport.urlopen",
                _urlopen_payload(self._activation_payload(active, maximum)),
            ):
                result = transport.activate(
                    license_code="LULU-USER-CODE",
                    device_hash="sha256:device",
                    app_version="0.2.2",
                    platform="Darwin",
                )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.active_device_count, active)
            self.assertEqual(result.max_devices, maximum)

    def test_invalid_dynamic_summary_fails_closed_without_default_two(self):
        transport = HttpsLicenseTransport("https://license.example.invalid")
        cases = (
            ("zero max", 0, 0),
            ("negative max", 0, -1),
            ("negative count", -1, 5),
            ("boolean max", 0, True),
            ("string max", 0, "5"),
            ("missing max", 0, None),
            ("missing count", None, 5),
        )
        for label, active, maximum in cases:
            with self.subTest(label=label):
                payload = self._activation_payload(0, 2)
                if active is None:
                    payload.pop("active_device_count")
                else:
                    payload["active_device_count"] = active
                if maximum is None:
                    payload.pop("max_devices")
                else:
                    payload["max_devices"] = maximum
                with patch("src.core.license.transport.urlopen", _urlopen_payload(payload)):
                    result = transport.activate(
                        license_code="LULU-USER-CODE",
                        device_hash="sha256:device",
                        app_version="0.2.2",
                        platform="Darwin",
                    )
                self.assertEqual(result.status, "invalid_server_response")
                self.assertIsNone(result.active_device_count)
                self.assertIsNone(result.max_devices)

    def test_deactivate_sends_request_id_and_accepts_only_committed_shape(self):
        request_id = "11111111-2222-4333-8444-555555555555"
        captured = {}
        payload = {
            "status": "ok",
            "deactivation_status": "stopped",
            "replayed": True,
            "active_device_count": 4,
            "max_devices": 9,
            "message": "本机已停用，设备名额已释放。",
        }
        with patch(
            "src.core.license.transport.urlopen",
            _urlopen_payload(payload, captured),
        ):
            result = HttpsLicenseTransport("https://license.example.invalid").deactivate(
                cache=_cache(),
                request_id=request_id,
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.deactivation_status, "stopped")
        self.assertEqual(result.request_id, request_id)
        self.assertTrue(result.replayed)
        self.assertEqual((result.active_device_count, result.max_devices), (4, 9))
        self.assertEqual(captured["body"]["request_id"], request_id)
        self.assertNotIn("Authorization", captured["headers"])
        self.assertNotIn("x-admin-token", {key.lower() for key in captured["headers"]})

    def test_deactivate_rejects_uncommitted_or_malformed_success(self):
        base = {
            "status": "ok",
            "deactivation_status": "stopped",
            "replayed": False,
            "active_device_count": 0,
            "max_devices": 1,
        }
        cases = (
            {**base, "deactivation_status": "pending"},
            {**base, "replayed": "false"},
            {**base, "active_device_count": -1},
            {**base, "max_devices": 0},
        )
        for payload in cases:
            with self.subTest(payload=payload), patch(
                "src.core.license.transport.urlopen",
                _urlopen_payload(payload),
            ):
                result = HttpsLicenseTransport("https://license.example.invalid").deactivate(
                    cache=_cache(),
                    request_id="11111111-2222-4333-8444-555555555555",
                )
            self.assertEqual(result.status, "invalid_server_response")


class LicenseSelfDeactivationServiceTests(unittest.TestCase):
    def _service(self, store, transport):
        return LicenseService(store, transport, now=lambda: NOW)

    def _activated_store(self, store=None):
        selected = store or MemorySecureStore()
        selected.save_cache(_cache())
        return selected

    def test_refresh_device_summary_uses_server_values_and_updates_cache(self):
        store = self._activated_store()
        refreshed = _cache()
        result = LicenseResult(
            "ok",
            "授权复验成功。",
            cache=refreshed,
            active_device_count=5,
            max_devices=12,
        )
        transport = _ScriptedTransport(verify_result=result)
        status = self._service(store, transport).refresh_device_summary()
        self.assertTrue(status.allowed)
        self.assertEqual(status.code, "ready")
        self.assertEqual((status.active_device_count, status.max_devices), (5, 12))
        self.assertEqual(transport.verify_calls, 1)

    def test_refresh_rejects_missing_summary_and_keeps_cache(self):
        store = self._activated_store()
        original = store.load_cache()
        transport = _ScriptedTransport(
            verify_result=LicenseResult("ok", "ok", cache=_cache())
        )
        status = self._service(store, transport).refresh_device_summary()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "invalid_server_response")
        self.assertEqual(store.load_cache(), original)

    def test_committed_success_clears_only_license_cache_and_requires_activation(self):
        store = self._activated_store()
        store.set_text("unrelated_business_state", "keep-me")
        transport = _ScriptedTransport(
            deactivation_steps=[_ok_result(_cache(), active_device_count=1, max_devices=5)]
        )
        status = self._service(store, transport).deactivate_current_device()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "stopped")
        self.assertEqual(status.deactivation_status, "stopped")
        self.assertEqual((status.active_device_count, status.max_devices), (1, 5))
        self.assertIsNone(store.load_cache())
        self.assertIsNone(store.load_pending_deactivation())
        self.assertEqual(store.get_text("unrelated_business_state"), "keep-me")
        self.assertEqual(
            self._service(store, transport).startup_status().code,
            "not_activated",
        )

    def test_explicit_failure_and_offline_never_clear_cache(self):
        for label, step, expected_code in (
            (
                "explicit failure",
                LicenseResult("lease_invalid", "凭证需要重新核清。"),
                "lease_invalid",
            ),
            (
                "offline or timeout",
                NetworkUnavailable("lease_token=do-not-leak network down"),
                "outcome_unknown",
            ),
        ):
            with self.subTest(label=label):
                store = self._activated_store()
                transport = _ScriptedTransport(deactivation_steps=[step])
                status = self._service(store, transport).deactivate_current_device()
                self.assertIsNotNone(store.load_cache())
                self.assertEqual(status.code, expected_code)
                if expected_code == "lease_invalid":
                    self.assertIsNone(store.load_pending_deactivation())
                else:
                    self.assertIsNotNone(store.load_pending_deactivation())
                self.assertNotIn("do-not-leak", status.message)

    def test_timeout_restart_reuses_same_request_id_and_fingerprint(self):
        store = self._activated_store()
        first_transport = _ScriptedTransport(
            deactivation_steps=[NetworkUnavailable("timeout")]
        )
        first = self._service(store, first_transport).deactivate_current_device()
        pending = store.load_pending_deactivation()
        self.assertEqual(first.code, "outcome_unknown")
        self.assertIsInstance(pending, PendingDeactivation)
        assert pending is not None
        self.assertEqual(first.request_id, pending.request_id)

        second_transport = _ScriptedTransport(
            deactivation_steps=[
                _ok_result(
                    _cache(),
                    active_device_count=0,
                    max_devices=2,
                    request_id=pending.request_id,
                    replayed=True,
                )
            ]
        )
        resumed = self._service(store, second_transport).resume_pending_deactivation()
        self.assertEqual(resumed.code, "stopped")
        self.assertTrue(resumed.replayed)
        self.assertEqual(second_transport.deactivate_calls, [pending.request_id])
        self.assertIsNone(store.load_cache())
        self.assertIsNone(store.load_pending_deactivation())

    def test_new_click_resolves_existing_pending_before_creating_any_new_request(self):
        store = self._activated_store()
        transport = _ScriptedTransport(
            deactivation_steps=[
                NetworkUnavailable("timeout"),
                LicenseResult("lease_invalid", "已确认未提交。"),
            ]
        )
        service = self._service(store, transport)
        first = service.deactivate_current_device()
        second = service.deactivate_current_device()
        self.assertEqual(first.code, "outcome_unknown")
        self.assertEqual(second.code, "lease_invalid")
        self.assertEqual(len(set(transport.deactivate_calls)), 1)
        self.assertIsNone(store.load_pending_deactivation())
        self.assertIsNotNone(store.load_cache())

    def test_server_error_stays_pending_and_retries_the_same_request(self):
        store = self._activated_store()
        transport = _ScriptedTransport(
            deactivation_steps=[
                LicenseResult("server_error", "暂时不可用。"),
                _ok_result(_cache(), active_device_count=0, max_devices=2),
            ]
        )
        service = self._service(store, transport)
        first = service.deactivate_current_device()
        pending = store.load_pending_deactivation()
        self.assertEqual(first.code, "outcome_unknown")
        self.assertIsNotNone(pending)
        assert pending is not None
        second = service.resume_pending_deactivation()
        self.assertEqual(second.code, "stopped")
        self.assertEqual(transport.deactivate_calls, [pending.request_id, pending.request_id])

    def test_startup_with_pending_resolves_same_request_before_local_allow(self):
        store = self._activated_store()
        request_id = "11111111-2222-4333-8444-555555555555"
        store.save_pending_deactivation(
            PendingDeactivation.for_cache(_cache(), request_id=request_id, created_at=NOW)
        )
        transport = _ScriptedTransport(
            deactivation_steps=[
                _ok_result(
                    _cache(),
                    active_device_count=0,
                    max_devices=1,
                    request_id=request_id,
                    replayed=True,
                )
            ]
        )
        status = self._service(store, transport).startup_status()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "stopped")
        self.assertEqual(transport.deactivate_calls, [request_id])

    def test_activation_attempt_cannot_replace_cache_before_pending_is_resolved(self):
        store = self._activated_store()
        request_id = "11111111-2222-4333-8444-555555555555"
        store.save_pending_deactivation(
            PendingDeactivation.for_cache(_cache(), request_id=request_id, created_at=NOW)
        )
        transport = _ScriptedTransport(
            deactivation_steps=[NetworkUnavailable("still unknown")]
        )
        status = self._service(store, transport).activate("LULU-NEW-CODE")
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "outcome_unknown")
        self.assertEqual(transport.deactivate_calls, [request_id])
        self.assertEqual(store.load_cache(), _cache())

    def test_pending_marker_contains_no_license_token_or_device_identifiers(self):
        store = self._activated_store()
        transport = _ScriptedTransport(
            deactivation_steps=[NetworkUnavailable("timeout")]
        )
        self._service(store, transport).deactivate_current_device()
        raw = store.get_text(PENDING_DEACTIVATION_KEY)
        self.assertIsNotNone(raw)
        assert raw is not None
        for secret in (
            _cache().license_id,
            _cache().device_id,
            _cache().device_hash,
            _cache().lease_token,
        ):
            self.assertNotIn(secret, raw)
        parsed = json.loads(raw)
        self.assertEqual(set(parsed), {
            "schema_version",
            "request_id",
            "fingerprint",
            "phase",
            "created_at",
            "active_device_count",
            "max_devices",
            "replayed",
        })

    def test_pending_fingerprint_mismatch_fails_closed_without_network(self):
        store = self._activated_store()
        pending = PendingDeactivation.for_cache(
            _cache(device_id="dev_old"),
            request_id="11111111-2222-4333-8444-555555555555",
            created_at=NOW,
        )
        store.save_pending_deactivation(pending)
        transport = _ScriptedTransport()
        status = self._service(store, transport).resume_pending_deactivation()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "pending_deactivation_mismatch")
        self.assertEqual(transport.deactivate_calls, [])
        self.assertIsNotNone(store.load_cache())
        self.assertIsNotNone(store.load_pending_deactivation())

    def test_cache_clear_failure_after_server_commit_is_fail_closed_and_recoverable(self):
        store = self._activated_store(_DeleteFailureStore())
        transport = _ScriptedTransport(
            deactivation_steps=[
                _ok_result(_cache(), active_device_count=1, max_devices=2)
            ]
        )
        service = self._service(store, transport)
        failed = service.deactivate_current_device()
        self.assertFalse(failed.allowed)
        self.assertEqual(failed.code, "local_cleanup_incomplete")
        self.assertIn("未完全清理", failed.message)
        self.assertIsNotNone(store.load_cache())
        pending = store.load_pending_deactivation()
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.phase, "server_committed")
        self.assertEqual(len(transport.deactivate_calls), 1)

        still_locked = service.startup_status()
        self.assertFalse(still_locked.allowed)
        self.assertEqual(still_locked.code, "local_cleanup_incomplete")
        self.assertEqual(len(transport.deactivate_calls), 1)

        store.fail_cache_delete = False
        recovered = service.startup_status()
        self.assertFalse(recovered.allowed)
        self.assertEqual(recovered.code, "stopped")
        self.assertIsNone(store.load_cache())
        self.assertIsNone(store.load_pending_deactivation())
        self.assertEqual(len(transport.deactivate_calls), 1)

    def test_pending_clear_failure_after_cache_clear_is_fail_closed_and_recoverable(self):
        store = self._activated_store(_PendingDeleteFailureStore())
        transport = _ScriptedTransport(
            deactivation_steps=[
                _ok_result(_cache(), active_device_count=0, max_devices=1)
            ]
        )
        service = self._service(store, transport)
        failed = service.deactivate_current_device()
        self.assertFalse(failed.allowed)
        self.assertEqual(failed.code, "local_cleanup_incomplete")
        self.assertIsNone(store.load_cache())
        pending = store.load_pending_deactivation()
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.phase, "server_committed")
        self.assertEqual(len(transport.deactivate_calls), 1)

        store.fail_pending_delete = False
        recovered = service.startup_status()
        self.assertEqual(recovered.code, "stopped")
        self.assertIsNone(store.load_pending_deactivation())
        self.assertEqual(len(transport.deactivate_calls), 1)

    def test_silent_cache_delete_is_detected_instead_of_reporting_stopped(self):
        store = self._activated_store(_SilentCacheDeleteStore())
        transport = _ScriptedTransport(
            deactivation_steps=[
                _ok_result(_cache(), active_device_count=0, max_devices=1)
            ]
        )
        status = self._service(store, transport).deactivate_current_device()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "local_cleanup_incomplete")
        self.assertIsNotNone(store.load_cache())
        self.assertEqual(store.load_pending_deactivation().phase, "server_committed")

    def test_corrupt_pending_marker_fails_closed_without_transport_call(self):
        store = self._activated_store()
        store.set_text(
            PENDING_DEACTIVATION_KEY,
            '{"schema_version":1,"request_id":"not-a-uuid","fingerprint":"plaintext"}',
        )
        transport = _ScriptedTransport()
        status = self._service(store, transport).startup_status()
        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "secure_store_error")
        self.assertEqual(transport.deactivate_calls, [])
        self.assertIsNotNone(store.load_cache())

    def test_concurrent_duplicate_click_is_single_flight(self):
        store = self._activated_store()
        transport = _BlockingTransport(
            _ok_result(_cache(), active_device_count=0, max_devices=2)
        )
        service = self._service(store, transport)
        first_result = []
        worker = threading.Thread(
            target=lambda: first_result.append(service.deactivate_current_device())
        )
        worker.start()
        self.assertTrue(transport.started.wait(1))
        duplicate = service.deactivate_current_device()
        self.assertFalse(duplicate.allowed)
        self.assertEqual(duplicate.code, "deactivation_in_progress")
        self.assertEqual(len(transport.deactivate_calls), 1)
        transport.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_result[0].code, "stopped")
        self.assertEqual(len(transport.deactivate_calls), 1)

    def test_redaction_covers_pending_and_deactivation_sensitive_labels(self):
        raw = (
            "license_id=lic_secret device_id=dev_secret "
            "request_fingerprint=sha256:abcdef123456abcdef123456abcdef123456 "
            "lease_token=lease_secret device_hash=sha256:123456abcdef123456abcdef123456abcdef"
        )
        redacted = redact_sensitive(raw)
        for leaked in ("lic_secret", "dev_secret", "abcdef123456", "lease_secret"):
            self.assertNotIn(leaked, redacted)


if __name__ == "__main__":
    unittest.main()
