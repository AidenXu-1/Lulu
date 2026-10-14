"""Source-test-only license transport.

The external-candidate PyInstaller graph explicitly excludes this module.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .models import FULL_ACCESS, LicenseCache, LicenseResult
from .transport import LicenseTransport, NetworkUnavailable


class MockLicenseTransport(LicenseTransport):
    VALID_CODE = "LULU-MOCK-VALID"
    THIRD_DEVICE_CODE = "LULU-MOCK-THIRD"
    REVOKED_CODE = "LULU-MOCK-REVOKED"
    EXPIRED_CODE = "LULU-MOCK-EXPIRED"
    SERVER_DOWN_CODE = "LULU-MOCK-SERVER"

    def __init__(self, *, now=None):
        self.now = now or (lambda: datetime.now())
        self.verify_mode = "valid"
        self.active_device_count = 1
        self.max_devices = 2

    def activate(self, *, license_code: str, device_hash: str, app_version: str, platform: str) -> LicenseResult:
        code = (license_code or "").strip().upper()
        if code == self.SERVER_DOWN_CODE:
            raise NetworkUnavailable("授权服务暂不可达")
        if code == self.THIRD_DEVICE_CODE:
            return LicenseResult("device_limit", "该授权码已达到设备上限,请联系授权方重置设备。")
        if code == self.REVOKED_CODE:
            return LicenseResult("revoked", "授权已停用,请联系授权方。")
        if code == self.EXPIRED_CODE:
            return LicenseResult("expired", "授权已过期,请联系授权方续期。")
        if code != self.VALID_CODE:
            return LicenseResult("invalid", "授权码无效,请检查后重试。")
        now = self.now()
        cache = LicenseCache(
            schema_version=1,
            identity_id="anon_mock",
            identity_kind="anonymous",
            license_id="lic_mock",
            device_id="dev_mock_1",
            device_hash=device_hash,
            entitlements=(FULL_ACCESS,),
            lease_token="lease_mock_token",
            last_verified_at=now,
            next_check_at=now + timedelta(days=1),
            grace_until=now + timedelta(days=7),
            last_local_seen_at=now,
        )
        return LicenseResult(
            "ok",
            "授权已激活。",
            cache=cache,
            server_time=now,
            active_device_count=self.active_device_count,
            max_devices=self.max_devices,
        )

    def verify(self, *, cache: LicenseCache, app_version: str) -> LicenseResult:
        if self.verify_mode == "network":
            raise NetworkUnavailable("授权服务暂不可达")
        if self.verify_mode == "revoked":
            return LicenseResult("revoked", "授权已停用,请联系授权方。")
        if self.verify_mode == "expired":
            return LicenseResult("expired", "授权已过期,请联系授权方续期。")
        now = self.now()
        updated = replace(
            cache,
            last_verified_at=now,
            next_check_at=now + timedelta(days=1),
            grace_until=now + timedelta(days=7),
            last_local_seen_at=now,
        )
        return LicenseResult(
            "ok",
            "授权复验成功。",
            cache=updated,
            server_time=now,
            active_device_count=self.active_device_count,
            max_devices=self.max_devices,
        )

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        self.active_device_count = max(0, self.active_device_count - 1)
        return LicenseResult(
            "ok",
            "本机已停用，设备名额已释放。",
            active_device_count=self.active_device_count,
            max_devices=self.max_devices,
            request_id=request_id,
            deactivation_status="stopped",
            replayed=False,
        )
