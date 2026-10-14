from __future__ import annotations

from datetime import datetime, timedelta
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .models import DeviceLimitSummary, FULL_ACCESS, LicenseCache, LicenseResult
from .redaction import redact_sensitive


class NetworkUnavailable(RuntimeError):
    pass


class LicenseTransportError(RuntimeError):
    pass


class LicenseTransport:
    def activate(self, *, license_code: str, device_hash: str, app_version: str, platform: str) -> LicenseResult:
        raise NotImplementedError

    def verify(self, *, cache: LicenseCache, app_version: str) -> LicenseResult:
        raise NotImplementedError

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        raise NotImplementedError


class MisconfiguredLicenseTransport(LicenseTransport):
    """Fail-closed transport used when a real environment is incomplete.

    Production/staging builds must never fall back to mock behavior. Returning a
    transport that always fails lets the lock page stay visible while preventing
    accidental activation or cache writes.
    """

    def __init__(self, reason: str = "授权服务配置不完整。"):
        self.reason = redact_sensitive(reason)

    def activate(self, *, license_code: str, device_hash: str, app_version: str, platform: str) -> LicenseResult:
        raise LicenseTransportError(self.reason)

    def verify(self, *, cache: LicenseCache, app_version: str) -> LicenseResult:
        raise LicenseTransportError(self.reason)

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        raise LicenseTransportError(self.reason)


class HttpsLicenseTransport(LicenseTransport):
    """Production/staging HTTPS transport for the public license API.

    This transport never carries admin credentials. It sends only authorization
    metadata to the configured endpoint and maps all upstream errors to the
    product-level statuses understood by LicenseService.
    """

    def __init__(self, endpoint: str, *, timeout_seconds: float = 8.0):
        endpoint = (endpoint or "").strip().rstrip("/") + "/"
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("授权服务 endpoint 必须是 https URL。")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context()

    def activate(self, *, license_code: str, device_hash: str, app_version: str, platform: str) -> LicenseResult:
        payload = {
            "license_code": license_code,
            "device_hash": device_hash,
            "device_label": "Lulu device",
            "app_version": app_version,
            "platform": platform,
        }
        data, retry_after = self._post("v1/activate", payload)
        return self._result_from_response(data, device_hash=device_hash, retry_after=retry_after)

    def verify(self, *, cache: LicenseCache, app_version: str) -> LicenseResult:
        payload = {
            "license_id": cache.license_id,
            "device_id": cache.device_id,
            "device_hash": cache.device_hash,
            "lease_token": cache.lease_token,
            "app_version": app_version,
            "last_seen_at": cache.last_verified_at.isoformat(),
        }
        data, retry_after = self._post("v1/verify", payload)
        return self._result_from_response(data, device_hash=cache.device_hash, retry_after=retry_after)

    def deactivate(self, *, cache: LicenseCache, request_id: str) -> LicenseResult:
        payload = {
            "license_id": cache.license_id,
            "device_id": cache.device_id,
            "device_hash": cache.device_hash,
            "lease_token": cache.lease_token,
            "request_id": request_id,
        }
        data, retry_after = self._post("v1/deactivate", payload)
        status = str(data.get("status") or "invalid")
        message = redact_sensitive(data.get("message") or "")
        device_summary, invalid_summary = _device_summary(
            data,
            required=status == "ok",
        )
        if invalid_summary:
            return LicenseResult(
                "invalid_server_response",
                invalid_summary,
                request_id=request_id,
                retry_after_seconds=retry_after,
            )
        if status != "ok":
            return LicenseResult(
                status,
                message,
                active_device_count=_summary_count(device_summary),
                max_devices=_summary_max(device_summary),
                request_id=request_id,
                retry_after_seconds=retry_after,
            )
        if data.get("deactivation_status") != "stopped" or not isinstance(data.get("replayed"), bool):
            return LicenseResult(
                "invalid_server_response",
                _invalid_response_message(),
                request_id=request_id,
                retry_after_seconds=retry_after,
            )
        return LicenseResult(
            "ok",
            message or "本机已停用，设备名额已释放。",
            active_device_count=_summary_count(device_summary),
            max_devices=_summary_max(device_summary),
            request_id=request_id,
            deactivation_status="stopped",
            replayed=data["replayed"],
            retry_after_seconds=retry_after,
        )

    def _post(self, path: str, payload: dict) -> tuple[dict, int | None]:
        url = urljoin(self.endpoint, path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "content-type": "application/json; charset=utf-8",
                "accept": "application/json",
                "user-agent": "Lulu/0.2.2 license-client",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self._ssl_context) as response:
                raw = response.read(512 * 1024)
                retry_after = _retry_after(response.headers.get("retry-after"))
        except HTTPError as exc:
            raw = exc.read(128 * 1024)
            retry_after = _retry_after(exc.headers.get("retry-after"))
            return self._decode_response(raw, retry_after=retry_after)
        except (TimeoutError, URLError, OSError) as exc:
            raise NetworkUnavailable("授权服务暂不可达") from exc
        return self._decode_response(raw, retry_after=retry_after)

    def _decode_response(self, raw: bytes, *, retry_after: int | None) -> tuple[dict, int | None]:
        try:
            data = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_fields,
            )
        except Exception as exc:  # noqa: BLE001
            raise LicenseTransportError("授权服务返回格式异常。") from exc
        if not isinstance(data, dict):
            raise LicenseTransportError("授权服务返回格式异常。")
        return data, retry_after

    def _result_from_response(
        self,
        data: dict,
        *,
        device_hash: str,
        retry_after: int | None,
    ) -> LicenseResult:
        status = str(data.get("status") or "invalid")
        message = redact_sensitive(data.get("message") or "")
        device_summary, invalid_summary = _device_summary(
            data,
            required=status == "device_limit",
        )
        if invalid_summary:
            return LicenseResult(
                "invalid_server_response",
                invalid_summary,
                retry_after_seconds=retry_after,
            )
        if status != "ok":
            return LicenseResult(
                status,
                message,
                retry_after_seconds=retry_after,
                active_device_count=_summary_count(device_summary),
                max_devices=_summary_max(device_summary),
            )
        invalid_message = _invalid_ok_response_message(data)
        if invalid_message:
            return LicenseResult("invalid_server_response", invalid_message, retry_after_seconds=retry_after)
        now = _parse_dt(data.get("server_time")) or datetime.now()
        cache = LicenseCache(
            schema_version=1,
            identity_id=str(data.get("identity_id")).strip(),
            identity_kind=str(data.get("identity_kind") or "anonymous").strip() or "anonymous",
            license_id=str(data.get("license_id")).strip(),
            device_id=str(data.get("device_id")).strip(),
            device_hash=device_hash,
            entitlements=tuple(str(x).strip() for x in data.get("entitlements") or ()),
            lease_token=str(data.get("lease_token")).strip(),
            last_verified_at=now,
            next_check_at=_parse_dt(data.get("next_check_at")) or (now + timedelta(days=1)),
            grace_until=_parse_dt(data.get("grace_until")) or (now + timedelta(days=7)),
            last_local_seen_at=now,
        )
        return LicenseResult(
            "ok",
            message or "授权已激活。",
            cache=cache,
            server_time=now,
            retry_after_seconds=retry_after,
            active_device_count=_summary_count(device_summary),
            max_devices=_summary_max(device_summary),
        )


def _invalid_ok_response_message(data: dict) -> str | None:
    required_text_fields = ("identity_id", "license_id", "device_id", "lease_token")
    for field in required_text_fields:
        if not str(data.get(field) or "").strip():
            return "授权服务返回数据不完整,请稍后重试或联系授权方。"
    entitlements = data.get("entitlements")
    if not isinstance(entitlements, (list, tuple)):
        return "授权服务返回数据不完整,请稍后重试或联系授权方。"
    normalized = tuple(str(item).strip() for item in entitlements if str(item or "").strip())
    if not normalized or FULL_ACCESS not in normalized:
        return "授权服务返回数据不完整,请稍后重试或联系授权方。"
    return None


def _device_summary(
    data: dict,
    *,
    required: bool,
) -> tuple[DeviceLimitSummary | None, str | None]:
    has_count = "active_device_count" in data
    has_max = "max_devices" in data
    if not has_count and not has_max and not required:
        return None, None
    if not has_count or not has_max:
        return None, _invalid_response_message()
    try:
        summary = DeviceLimitSummary.from_values(
            data.get("active_device_count"),
            data.get("max_devices"),
        )
    except ValueError:
        return None, _invalid_response_message()
    return summary, None


def _summary_count(summary: DeviceLimitSummary | None) -> int | None:
    return summary.active_device_count if summary is not None else None


def _summary_max(summary: DeviceLimitSummary | None) -> int | None:
    return summary.max_devices if summary is not None else None


def _reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _invalid_response_message() -> str:
    return "授权服务返回数据不完整,请稍后重试或联系授权方。"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _retry_after(value) -> int | None:
    try:
        if value is None:
            return None
        parsed = int(str(value).strip())
        return parsed if parsed >= 0 else None
    except ValueError:
        return None
