"""Read-only projection of the GUI's existing StrictKeyringStore cache."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Callable

from ..license.models import FULL_ACCESS, LicenseCache, LicenseStatus
from .keychain import KeychainBoundary, _secure_read


_CACHE_KEY = "license_cache_v1"
_PENDING_KEY = "license_pending_deactivation_v1"


class ReadOnlySharedLicenseProjection:
    def __init__(
        self,
        keychain: KeychainBoundary,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._keychain = keychain
        self._now = now

    @staticmethod
    def _locked(code: str, message: str) -> LicenseStatus:
        return LicenseStatus(False, code, message)

    def cached_status_projection(self) -> LicenseStatus:
        pending = _secure_read(self._keychain, _PENDING_KEY)
        if pending:
            return self._locked(
                "deactivation_pending", "本机授权停用状态需要核清。"
            )
        raw = _secure_read(self._keychain, _CACHE_KEY)
        if not raw:
            return self._locked("not_activated", "本机尚无可用授权缓存。")
        try:
            cache = LicenseCache.from_json(json.loads(raw))
        except Exception:
            return self._locked("secure_store_error", "授权缓存已损坏。")
        now = self._now()
        if now + timedelta(minutes=5) < cache.last_local_seen_at:
            return self._locked("time_anomaly", "系统时间异常。")
        if FULL_ACCESS not in cache.entitlements:
            return self._locked("no_entitlement", "授权不包含 Lulu 使用权限。")
        if now > cache.grace_until:
            return self._locked("expired", "本机授权缓存已到期。")
        code = "cached_grace" if now >= cache.next_check_at else "cached_allowed"
        return LicenseStatus(True, code, "本机授权缓存有效。", cache=cache)
