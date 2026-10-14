"""Phase 2 unified authorization guard contract tests."""

from __future__ import annotations

from types import SimpleNamespace
import unittest


class _ProjectionServiceStub:
    def __init__(self, status) -> None:
        self.status = status
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return self.status


class Phase2AuthGuardTests(unittest.TestCase):
    def test_active_projection_allows_and_binds_the_supplied_license_service(self):
        from src.core.auth_guard import UnifiedLicenseAuthGuard

        service = _ProjectionServiceStub(
            SimpleNamespace(allowed=True, code="cached_allowed")
        )
        guard = UnifiedLicenseAuthGuard(service)

        guard.assert_local_file_transcription_allowed(object())

        self.assertIs(guard.license_service, service)
        self.assertEqual(service.calls, 1)

    def test_denied_projection_maps_to_stable_authorization_failures(self):
        from src.core.auth_contracts import AuthorizationError
        from src.core.auth_guard import UnifiedLicenseAuthGuard

        cases = (
            ("not_activated", "AUTH_REQUIRED"),
            ("expired", "AUTH_REQUIRED"),
            ("time_anomaly", "AUTH_REQUIRED"),
            ("no_entitlement", "AUTH_REQUIRED"),
            ("revoked", "AUTH_REQUIRED"),
            ("suspended", "AUTH_REQUIRED"),
            ("deactivation_pending", "AUTH_REQUIRED"),
            ("secure_store_error", "SECURE_STORE_UNAVAILABLE"),
        )
        for projection_code, expected_code in cases:
            with self.subTest(projection_code=projection_code):
                service = _ProjectionServiceStub(
                    SimpleNamespace(allowed=False, code=projection_code)
                )
                guard = UnifiedLicenseAuthGuard(service)

                with self.assertRaises(AuthorizationError) as caught:
                    guard.assert_local_file_transcription_allowed(object())

                self.assertEqual(caught.exception.failure.code, expected_code)
                self.assertEqual(service.calls, 1)

    def test_revoked_guard_fails_auth_required_without_reading_projection(self):
        from src.core.auth_contracts import AuthorizationError
        from src.core.auth_guard import UnifiedLicenseAuthGuard

        service = _ProjectionServiceStub(
            SimpleNamespace(allowed=True, code="cached_allowed")
        )
        guard = UnifiedLicenseAuthGuard(service)

        guard.revoke()
        guard.revoke()
        with self.assertRaises(AuthorizationError) as caught:
            guard.assert_local_file_transcription_allowed(object())

        self.assertEqual(caught.exception.failure.code, "AUTH_REQUIRED")
        self.assertEqual(service.calls, 0)


if __name__ == "__main__":
    unittest.main()
