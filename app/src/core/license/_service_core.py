"""Transport-agnostic license state machine shared by source and external builds."""

from __future__ import annotations

from datetime import datetime, timedelta
import platform
import threading
import uuid

from .device import new_device_salt, privacy_preserving_device_hash
from .models import (
    DeviceLimitSummary,
    FULL_ACCESS,
    LicenseCache,
    LicenseStatus,
    PendingDeactivation,
)
from .redaction import redact_sensitive
from .store import SecureStoreError
from .transport import LicenseTransport, NetworkUnavailable
from ...version import __build__, __version__, runtime_channel


def app_version() -> str:
    return f"{__version__}+{__build__}.{runtime_channel()}"


APP_VERSION = app_version()
_EXPLICIT_DEACTIVATION_FAILURES = {
    "invalid",
    "expired",
    "revoked",
    "suspended",
    "lease_invalid",
    "rate_limited",
}


class LicenseService:
    def __init__(self, store, transport: LicenseTransport, *, now=None):
        self.store = store
        self.transport = transport
        self.now = now or (lambda: datetime.now())
        self._deactivation_lock = threading.Lock()

    def cached_status_projection(self) -> LicenseStatus:
        """Project the existing local authorization state without effects."""
        try:
            pending = self.store.load_pending_deactivation()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "secure_store_error",
                self._safe_error("读取停用恢复状态失败,Lulu 已保持锁定。", exc),
            )
        if pending is not None:
            return self._locked("deactivation_pending", "本机授权停用状态需要核清。")
        try:
            cache = self.store.load_cache()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "secure_store_error",
                self._safe_error("读取授权缓存失败,Lulu 已保持锁定。", exc),
            )
        if cache is None:
            return self._locked("not_activated", "本机尚无可用授权缓存。")
        now = self.now()
        if now + timedelta(minutes=5) < cache.last_local_seen_at:
            return self._locked("time_anomaly", "系统时间异常,请校准后联网复验。", cache=cache)
        if FULL_ACCESS not in cache.entitlements:
            return self._locked("no_entitlement", "授权不包含 Lulu 使用权限。", cache=cache)
        if now > cache.grace_until:
            return self._locked("expired", "本机授权缓存已到期,请由可信 GUI 复验。", cache=cache)
        if now >= cache.next_check_at:
            return LicenseStatus(
                True,
                "cached_grace",
                f"本机授权缓存待复验,离线宽限至 {cache.grace_until.date()}。",
                cache=cache,
            )
        return LicenseStatus(True, "cached_allowed", "本机授权缓存有效。", cache=cache)

    def startup_status(self) -> LicenseStatus:
        try:
            pending = self.store.load_pending_deactivation()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "secure_store_error",
                self._safe_error("读取停用恢复状态失败,Lulu 已保持锁定。", exc),
            )
        if pending is not None:
            return self.resume_pending_deactivation()
        try:
            cache = self.store.load_cache()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        if cache is None:
            return self._locked("not_activated", "此安装包需要授权码激活。请输入授权码后继续使用 Lulu。")
        now = self.now()
        if now + timedelta(minutes=5) < cache.last_local_seen_at:
            return self._locked("time_anomaly", "系统时间异常,请校准后联网复验。", cache=cache)
        if FULL_ACCESS not in cache.entitlements:
            return self._locked("no_entitlement", "授权不包含 Lulu 使用权限,请联系授权方。", cache=cache)
        if now < cache.next_check_at:
            self._save_local_seen(cache, now)
            return LicenseStatus(True, "allowed", "授权有效。", cache=cache.with_local_seen(now))
        try:
            result = self.transport.verify(cache=cache, app_version=APP_VERSION)
        except NetworkUnavailable:
            if now <= cache.grace_until:
                self._save_local_seen(cache, now)
                return LicenseStatus(True, "offline_grace", f"授权服务器暂不可达,离线宽限至 {cache.grace_until.date()}。", cache=cache.with_local_seen(now))
            return self._locked("grace_expired", "授权服务器暂不可达且已超过 7 天离线宽限,请联网复验。", cache=cache)
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc), cache=cache)
        except Exception as exc:  # noqa: BLE001
            return self._locked("server_error", self._safe_error("授权复验失败,请稍后重试。", exc), cache=cache)
        if result.status == "ok" and result.cache is not None:
            try:
                self.store.save_cache(result.cache.with_local_seen(now))
            except SecureStoreError as exc:
                return self._locked("secure_store_error", str(exc), cache=cache)
            return LicenseStatus(
                True,
                "allowed",
                "授权有效。",
                cache=result.cache.with_local_seen(now),
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
            )
        return self._locked(
            result.status,
            self._business_message(
                result.status,
                result.message,
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
            ),
            cache=cache,
            active_device_count=result.active_device_count,
            max_devices=result.max_devices,
        )

    def activate(self, license_code: str) -> LicenseStatus:
        try:
            pending = self.store.load_pending_deactivation()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "secure_store_error",
                self._safe_error("读取停用恢复状态失败,Lulu 已保持锁定。", exc),
            )
        if pending is not None:
            return self.resume_pending_deactivation()
        code = (license_code or "").strip()
        if not code:
            return self._locked("empty_code", "请输入授权码。")
        try:
            salt = self.store.get_or_create_device_salt(new_device_salt)
            device_hash = privacy_preserving_device_hash(salt)
            result = self.transport.activate(
                license_code=code,
                device_hash=device_hash,
                app_version=APP_VERSION,
                platform=platform.system(),
            )
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except NetworkUnavailable:
            return self._locked("network_unavailable", "首次激活需要联网,请检查网络后重试。")
        except Exception as exc:  # noqa: BLE001
            return self._locked("server_error", self._safe_error("授权服务暂时不可用,请稍后重试。", exc))
        if result.status == "ok" and result.cache is not None:
            try:
                self.store.save_cache(result.cache.with_local_seen(self.now()))
            except SecureStoreError as exc:
                return self._locked("secure_store_error", str(exc))
            return LicenseStatus(
                True,
                "allowed",
                "授权已激活。",
                cache=result.cache,
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
            )
        return self._locked(
            result.status,
            self._business_message(
                result.status,
                result.message,
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
            ),
            active_device_count=result.active_device_count,
            max_devices=result.max_devices,
        )

    def refresh_device_summary(self) -> LicenseStatus:
        try:
            pending = self.store.load_pending_deactivation()
            cache = self.store.load_cache()
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "secure_store_error",
                self._safe_error("读取本机授权状态失败,Lulu 已保持锁定。", exc),
            )
        if pending is not None:
            return self.resume_pending_deactivation()
        if cache is None:
            return self._locked("not_activated", "本机需要先激活 Lulu。")
        try:
            result = self.transport.verify(cache=cache, app_version=APP_VERSION)
        except NetworkUnavailable:
            return self._locked(
                "network_unavailable",
                "连接授权服务后才能刷新本机设备状态。",
                cache=cache,
            )
        except Exception as exc:  # noqa: BLE001
            return self._locked(
                "server_error",
                self._safe_error("授权状态刷新失败,请稍后重试。", exc),
                cache=cache,
            )
        if (
            result.status != "ok"
            or result.cache is None
            or not self._valid_device_summary(result.active_device_count, result.max_devices)
        ):
            code = result.status if result.status != "ok" else "invalid_server_response"
            return self._locked(
                code,
                self._business_message(
                    code,
                    result.message,
                    active_device_count=result.active_device_count,
                    max_devices=result.max_devices,
                ),
                cache=cache,
            )
        refreshed = result.cache.with_local_seen(self.now())
        try:
            self.store.save_cache(refreshed)
        except SecureStoreError as exc:
            return self._locked("secure_store_error", str(exc), cache=cache)
        return LicenseStatus(
            True,
            "ready",
            "本机授权状态已刷新。",
            cache=refreshed,
            active_device_count=result.active_device_count,
            max_devices=result.max_devices,
        )

    def deactivate_current_device(self) -> LicenseStatus:
        if not self._deactivation_lock.acquire(blocking=False):
            return self._locked(
                "deactivation_in_progress",
                "本机停用请求正在处理中,请等待当前结果。",
                deactivation_status="submitting",
            )
        try:
            try:
                pending = self.store.load_pending_deactivation()
                cache = self.store.load_cache()
            except SecureStoreError as exc:
                return self._locked("secure_store_error", str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._locked(
                    "secure_store_error",
                    self._safe_error("读取本机授权状态失败,Lulu 已保持锁定。", exc),
                )
            if pending is not None:
                return self._resume_pending_locked(pending, cache)
            if cache is None:
                return self._locked("not_activated", "本机当前没有可停用的授权。")
            pending = PendingDeactivation.for_cache(
                cache,
                request_id=str(uuid.uuid4()),
                created_at=self.now(),
            )
            try:
                self.store.save_pending_deactivation(pending)
                if self.store.load_pending_deactivation() != pending:
                    raise SecureStoreError("停用恢复状态未能可靠保存。")
            except SecureStoreError as exc:
                return self._locked("secure_store_error", str(exc), cache=cache)
            except Exception as exc:  # noqa: BLE001
                return self._locked(
                    "secure_store_error",
                    self._safe_error("保存停用恢复状态失败,本次请求尚未提交。", exc),
                    cache=cache,
                )
            return self._resolve_pending_locked(pending, cache)
        finally:
            self._deactivation_lock.release()

    def resume_pending_deactivation(self) -> LicenseStatus:
        if not self._deactivation_lock.acquire(blocking=False):
            return self._locked(
                "deactivation_in_progress",
                "本机停用结果正在核清,请等待当前结果。",
                deactivation_status="resolving",
            )
        try:
            try:
                pending = self.store.load_pending_deactivation()
                cache = self.store.load_cache()
            except SecureStoreError as exc:
                return self._locked("secure_store_error", str(exc))
            except Exception as exc:  # noqa: BLE001
                return self._locked(
                    "secure_store_error",
                    self._safe_error("读取停用恢复状态失败,Lulu 已保持锁定。", exc),
                )
            if pending is None:
                if cache is None:
                    return self._locked("not_activated", "本机当前需要重新激活 Lulu。")
                return LicenseStatus(True, "no_pending_deactivation", "没有待核清的本机停用请求。", cache=cache)
            return self._resume_pending_locked(pending, cache)
        finally:
            self._deactivation_lock.release()

    def clear_cache(self) -> None:
        self.store.clear_cache()

    def _resume_pending_locked(
        self,
        pending: PendingDeactivation,
        cache: LicenseCache | None,
    ) -> LicenseStatus:
        if pending.phase == "server_committed":
            return self._finish_committed_cleanup(pending, cache)
        if cache is None:
            return self._locked(
                "pending_deactivation_cache_missing",
                "本机停用结果尚未核清且授权缓存缺失,Lulu 已保持锁定,请联系授权方。",
                request_id=pending.request_id,
                deactivation_status="resolving",
            )
        if not pending.matches_cache(cache):
            return self._locked(
                "pending_deactivation_mismatch",
                "本机停用恢复状态与当前授权不一致,Lulu 已保持锁定,请联系授权方。",
                cache=cache,
                request_id=pending.request_id,
                deactivation_status="resolving",
            )
        return self._resolve_pending_locked(pending, cache)

    def _resolve_pending_locked(
        self,
        pending: PendingDeactivation,
        cache: LicenseCache,
    ) -> LicenseStatus:
        try:
            result = self.transport.deactivate(
                cache=cache,
                request_id=pending.request_id,
            )
        except NetworkUnavailable:
            return self._outcome_unknown(pending, cache)
        except Exception as exc:  # noqa: BLE001
            _ = redact_sensitive(exc)
            return self._outcome_unknown(pending, cache)

        if result.status == "ok":
            if (
                result.deactivation_status != "stopped"
                or not self._valid_device_summary(result.active_device_count, result.max_devices)
                or not isinstance(result.replayed, bool)
                or (result.request_id is not None and result.request_id != pending.request_id)
            ):
                return self._outcome_unknown(pending, cache)
            committed = pending.with_server_commit(
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
                replayed=result.replayed,
            )
            try:
                self.store.save_pending_deactivation(committed)
                if self.store.load_pending_deactivation() != committed:
                    raise SecureStoreError("服务端成功状态未能可靠保存。")
            except Exception:  # noqa: BLE001
                return self._local_cleanup_incomplete(
                    cache,
                    committed=True,
                    pending=committed,
                )
            return self._finish_committed_cleanup(committed, cache)

        if result.status not in _EXPLICIT_DEACTIVATION_FAILURES:
            return self._outcome_unknown(pending, cache)
        try:
            self.store.clear_pending_deactivation()
        except Exception:  # noqa: BLE001
            return self._local_cleanup_incomplete(
                cache,
                committed=False,
                pending=pending,
            )
        return self._locked(
            result.status,
            self._business_message(
                result.status,
                result.message,
                active_device_count=result.active_device_count,
                max_devices=result.max_devices,
            ),
            cache=cache,
            active_device_count=result.active_device_count,
            max_devices=result.max_devices,
            request_id=pending.request_id,
            deactivation_status="failed",
            replayed=result.replayed,
        )

    def _finish_committed_cleanup(
        self,
        pending: PendingDeactivation,
        cache: LicenseCache | None,
    ) -> LicenseStatus:
        if not self._valid_device_summary(pending.active_device_count, pending.max_devices):
            return self._local_cleanup_incomplete(
                cache,
                committed=True,
                pending=pending,
            )
        try:
            self.store.clear_cache()
            if self.store.load_cache() is not None:
                raise SecureStoreError("本机授权缓存仍然存在。")
        except Exception:  # noqa: BLE001
            return self._local_cleanup_incomplete(
                cache,
                committed=True,
                pending=pending,
            )
        try:
            self.store.clear_pending_deactivation()
            if self.store.load_pending_deactivation() is not None:
                raise SecureStoreError("停用恢复状态仍然存在。")
        except Exception:  # noqa: BLE001
            return self._local_cleanup_incomplete(
                None,
                committed=True,
                pending=pending,
            )
        return self._locked(
            "stopped",
            "本机已停用，设备名额已释放。请重新激活后继续使用 Lulu。",
            active_device_count=pending.active_device_count,
            max_devices=pending.max_devices,
            request_id=pending.request_id,
            deactivation_status="stopped",
            replayed=pending.replayed,
        )

    def _outcome_unknown(
        self,
        pending: PendingDeactivation,
        cache: LicenseCache,
    ) -> LicenseStatus:
        return self._locked(
            "outcome_unknown",
            "本机停用请求的结果正在确认。Lulu 已保留授权缓存并保持锁定,请联网后继续核清。",
            cache=cache,
            request_id=pending.request_id,
            deactivation_status="resolving",
        )

    def _local_cleanup_incomplete(
        self,
        cache: LicenseCache | None,
        *,
        committed: bool,
        pending: PendingDeactivation | None = None,
    ) -> LicenseStatus:
        message = (
            "本机授权已在服务端停用，但本地授权状态未完全清理。"
            "Lulu 已保持锁定,请重试清理后重新激活。"
            if committed
            else
            "本机停用状态未能在本地完整保存或清理。Lulu 已保持锁定,请重试核清。"
        )
        return self._locked(
            "local_cleanup_incomplete",
            message,
            cache=cache,
            active_device_count=pending.active_device_count if pending else None,
            max_devices=pending.max_devices if pending else None,
            request_id=pending.request_id if pending else None,
            deactivation_status="cleanup_required",
            replayed=pending.replayed if pending else False,
        )

    def _valid_device_summary(
        self,
        active_device_count: int | None,
        max_devices: int | None,
    ) -> bool:
        try:
            DeviceLimitSummary.from_values(active_device_count, max_devices)
        except ValueError:
            return False
        return True

    def _save_local_seen(self, cache, now: datetime) -> None:
        try:
            self.store.save_cache(cache.with_local_seen(now))
        except SecureStoreError:
            raise
        except Exception:  # noqa: BLE001
            pass

    def _locked(
        self,
        code: str,
        message: str,
        *,
        cache=None,
        active_device_count: int | None = None,
        max_devices: int | None = None,
        request_id: str | None = None,
        deactivation_status: str | None = None,
        replayed: bool = False,
    ) -> LicenseStatus:
        return LicenseStatus(
            False,
            code,
            redact_sensitive(message),
            cache=cache,
            active_device_count=active_device_count,
            max_devices=max_devices,
            request_id=request_id,
            deactivation_status=deactivation_status,
            replayed=replayed,
        )

    def _business_message(
        self,
        status: str,
        message: str,
        *,
        active_device_count: int | None = None,
        max_devices: int | None = None,
    ) -> str:
        if status == "device_limit" and self._valid_device_summary(active_device_count, max_devices):
            summary = DeviceLimitSummary.from_values(active_device_count, max_devices)
            return redact_sensitive(
                f"当前授权已使用 {summary.fraction} 台设备。"
                "请先在仍可使用的旧设备打开 Lulu 的“设置 > 授权与设备”，"
                "停用本机后再回来重试。若旧设备已无法使用，请联系授权方重置设备。"
            )
        mapping = {
            "invalid": "授权码无效,请检查后重试。",
            "expired": "授权已过期,请联系授权方续期。",
            "revoked": "授权已停用,请联系授权方。",
            "suspended": "授权已暂时停用,请联系授权方。",
            "device_limit": "该授权码已达到设备上限,请联系授权方重置设备。",
            "rate_limited": "尝试次数过多,请稍后再试。",
            "lease_invalid": "授权凭证已失效,请重新激活或联系授权方。",
            "deactivation_conflict": "本机授权状态刚发生变化,请联网核清后重试。",
            "idempotency_conflict": "本机停用请求与原设备不一致,Lulu 已保持锁定,请联系授权方。",
            "server_error": "授权服务暂时不可用,请稍后重试。",
            "invalid_server_response": "授权服务返回数据不完整,请稍后重试或联系授权方。",
        }
        return redact_sensitive(mapping.get(status, message or "授权不可用,请联系授权方。"))

    def _safe_error(self, fallback: str, exc: Exception) -> str:
        _ = redact_sensitive(exc)
        return fallback
