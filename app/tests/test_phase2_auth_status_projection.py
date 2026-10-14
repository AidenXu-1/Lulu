"""Phase 2 read-only cached license projection contract tests."""

from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from src.core.license.models import FULL_ACCESS, LicenseCache
from src.core.license.service import LicenseService
from src.core.license.store import SecureStoreError


_NOW = datetime(2026, 8, 20, 12, 0, 0)


def _active_cache() -> LicenseCache:
    return LicenseCache(
        schema_version=1,
        identity_id="identity-test",
        identity_kind="anonymous",
        license_id="license-test",
        device_id="device-test",
        device_hash="sha256:" + "a" * 64,
        entitlements=(FULL_ACCESS,),
        lease_token="lease-test",
        last_verified_at=_NOW - timedelta(hours=1),
        next_check_at=_NOW + timedelta(hours=1),
        grace_until=_NOW + timedelta(days=7),
        last_local_seen_at=_NOW - timedelta(hours=1),
    )


def _cache_with(**changes: object) -> LicenseCache:
    payload = _active_cache().to_json()
    for key, value in changes.items():
        payload[key] = value.isoformat() if isinstance(value, datetime) else value
    return LicenseCache.from_json(payload)


class _ReadOnlyStoreSpy:
    def __init__(self, cache: LicenseCache | None, *, pending: object = None) -> None:
        self.cache = cache
        self.pending = pending
        self.calls: list[str] = []

    def load_pending_deactivation(self):
        self.calls.append("load_pending_deactivation")
        return self.pending

    def load_cache(self):
        self.calls.append("load_cache")
        return self.cache

    def __getattr__(self, name: str):
        if name.startswith(("save_", "clear_", "delete_", "get_or_create_")):
            raise AssertionError(f"read-only projection attempted mutation: {name}")
        raise AttributeError(name)


class _NoTransportSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        self.calls.append(name)
        raise AssertionError(f"read-only projection attempted transport: {name}")


class _FailingReadStore(_ReadOnlyStoreSpy):
    def __init__(self, *, fail_on: str, error: Exception) -> None:
        super().__init__(_active_cache())
        self.fail_on = fail_on
        self.error = error

    def load_pending_deactivation(self):
        self.calls.append("load_pending_deactivation")
        if self.fail_on == "pending":
            raise self.error
        return None

    def load_cache(self):
        self.calls.append("load_cache")
        if self.fail_on == "cache":
            raise self.error
        return self.cache


class Phase2AuthStatusProjectionTests(unittest.TestCase):
    def test_active_cached_entitlement_is_allowed_without_transport_or_writes(self):
        store = _ReadOnlyStoreSpy(_active_cache())
        transport = _NoTransportSpy()
        service = LicenseService(store, transport, now=lambda: _NOW)

        status = service.cached_status_projection()

        self.assertTrue(status.allowed)
        self.assertEqual(status.code, "cached_allowed")
        self.assertIs(status.cache, store.cache)
        self.assertEqual(store.calls, ["load_pending_deactivation", "load_cache"])
        self.assertEqual(transport.calls, [])

    def test_pending_deactivation_is_denied_before_cache_read(self):
        store = _ReadOnlyStoreSpy(_active_cache(), pending=object())
        transport = _NoTransportSpy()
        service = LicenseService(store, transport, now=lambda: _NOW)

        status = service.cached_status_projection()

        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "deactivation_pending")
        self.assertEqual(store.calls, ["load_pending_deactivation"])
        self.assertEqual(transport.calls, [])

    def test_missing_cache_is_not_activated_without_transport_or_writes(self):
        store = _ReadOnlyStoreSpy(None)
        transport = _NoTransportSpy()
        service = LicenseService(store, transport, now=lambda: _NOW)

        status = service.cached_status_projection()

        self.assertFalse(status.allowed)
        self.assertEqual(status.code, "not_activated")
        self.assertEqual(store.calls, ["load_pending_deactivation", "load_cache"])
        self.assertEqual(transport.calls, [])

    def test_secure_store_access_or_parse_failure_is_locked(self):
        cases = (
            ("pending", SecureStoreError("安全存储不可用")),
            ("pending", ValueError("invalid pending payload")),
            ("cache", SecureStoreError("安全存储不可用")),
            ("cache", ValueError("invalid cache payload")),
        )
        for fail_on, error in cases:
            with self.subTest(fail_on=fail_on, error=type(error).__name__):
                store = _FailingReadStore(fail_on=fail_on, error=error)
                transport = _NoTransportSpy()
                service = LicenseService(store, transport, now=lambda: _NOW)

                status = service.cached_status_projection()

                self.assertFalse(status.allowed)
                self.assertEqual(status.code, "secure_store_error")
                self.assertEqual(transport.calls, [])

    def test_time_entitlement_and_expiry_failures_remain_read_only(self):
        cases = (
            (_cache_with(last_local_seen_at=_NOW + timedelta(minutes=6)), "time_anomaly"),
            (_cache_with(entitlements=[]), "no_entitlement"),
            (
                _cache_with(
                    next_check_at=_NOW + timedelta(hours=1),
                    grace_until=_NOW - timedelta(seconds=1),
                ),
                "expired",
            ),
        )
        for cache, expected_code in cases:
            with self.subTest(expected_code=expected_code, cache=cache):
                store = _ReadOnlyStoreSpy(cache)
                transport = _NoTransportSpy()
                service = LicenseService(store, transport, now=lambda: _NOW)

                status = service.cached_status_projection()

                self.assertFalse(status.allowed)
                self.assertEqual(status.code, expected_code)
                self.assertEqual(
                    store.calls,
                    ["load_pending_deactivation", "load_cache"],
                )
                self.assertEqual(transport.calls, [])

    def test_due_verification_projects_cached_grace_without_effects(self):
        cache = _cache_with(
            next_check_at=_NOW - timedelta(seconds=1),
            grace_until=_NOW + timedelta(days=2),
        )
        store = _ReadOnlyStoreSpy(cache)
        transport = _NoTransportSpy()
        service = LicenseService(store, transport, now=lambda: _NOW)

        status = service.cached_status_projection()

        self.assertTrue(status.allowed)
        self.assertEqual(status.code, "cached_grace")
        self.assertIs(status.cache, cache)
        self.assertEqual(store.calls, ["load_pending_deactivation", "load_cache"])
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
