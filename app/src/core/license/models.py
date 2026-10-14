from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import string
import uuid


FULL_ACCESS = "lulu.full_access"
MAX_DEVICE_SUMMARY_INTEGER = (1 << 53) - 1


@dataclass(frozen=True)
class DeviceLimitSummary:
    """Validated server truth for the current active/max device count."""

    active_device_count: int
    max_devices: int

    @classmethod
    def from_values(
        cls,
        active_device_count: object,
        max_devices: object,
    ) -> "DeviceLimitSummary":
        if (
            isinstance(active_device_count, bool)
            or not isinstance(active_device_count, int)
            or active_device_count < 0
            or active_device_count > MAX_DEVICE_SUMMARY_INTEGER
            or isinstance(max_devices, bool)
            or not isinstance(max_devices, int)
            or max_devices < 1
            or max_devices > MAX_DEVICE_SUMMARY_INTEGER
        ):
            raise ValueError("invalid device limit summary")
        return cls(
            active_device_count=active_device_count,
            max_devices=max_devices,
        )

    @property
    def fraction(self) -> str:
        return f"{self.active_device_count}/{self.max_devices}"


@dataclass(frozen=True)
class LicenseCache:
    schema_version: int
    identity_id: str
    identity_kind: str
    license_id: str
    device_id: str
    device_hash: str
    entitlements: tuple[str, ...]
    lease_token: str
    last_verified_at: datetime
    next_check_at: datetime
    grace_until: datetime
    last_local_seen_at: datetime

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "identity_id": self.identity_id,
            "identity_kind": self.identity_kind,
            "license_id": self.license_id,
            "device_id": self.device_id,
            "device_hash": self.device_hash,
            "entitlements": list(self.entitlements),
            "lease_token": self.lease_token,
            "last_verified_at": self.last_verified_at.isoformat(),
            "next_check_at": self.next_check_at.isoformat(),
            "grace_until": self.grace_until.isoformat(),
            "last_local_seen_at": self.last_local_seen_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict) -> "LicenseCache":
        return cls(
            schema_version=int(data.get("schema_version") or 1),
            identity_id=str(data.get("identity_id") or ""),
            identity_kind=str(data.get("identity_kind") or "anonymous"),
            license_id=str(data.get("license_id") or ""),
            device_id=str(data.get("device_id") or ""),
            device_hash=str(data.get("device_hash") or ""),
            entitlements=tuple(str(x) for x in data.get("entitlements") or ()),
            lease_token=str(data.get("lease_token") or ""),
            last_verified_at=_dt(data.get("last_verified_at")),
            next_check_at=_dt(data.get("next_check_at")),
            grace_until=_dt(data.get("grace_until")),
            last_local_seen_at=_dt(data.get("last_local_seen_at")),
        )

    def with_local_seen(self, now: datetime) -> "LicenseCache":
        d = self.to_json()
        d["last_local_seen_at"] = now.isoformat()
        return LicenseCache.from_json(d)


@dataclass(frozen=True)
class PendingDeactivation:
    """Credential-free marker used to recover an uncertain deactivation."""

    schema_version: int
    request_id: str
    fingerprint: str
    phase: str
    created_at: datetime
    active_device_count: int | None = None
    max_devices: int | None = None
    replayed: bool = False

    @classmethod
    def for_cache(
        cls,
        cache: LicenseCache,
        *,
        request_id: str,
        created_at: datetime,
    ) -> "PendingDeactivation":
        return cls(
            schema_version=1,
            request_id=request_id,
            fingerprint=_cache_fingerprint(cache),
            phase="resolving",
            created_at=created_at,
        )

    def matches_cache(self, cache: LicenseCache) -> bool:
        return hmac.compare_digest(self.fingerprint, _cache_fingerprint(cache))

    def with_server_commit(
        self,
        *,
        active_device_count: int,
        max_devices: int,
        replayed: bool,
    ) -> "PendingDeactivation":
        return PendingDeactivation(
            schema_version=self.schema_version,
            request_id=self.request_id,
            fingerprint=self.fingerprint,
            phase="server_committed",
            created_at=self.created_at,
            active_device_count=active_device_count,
            max_devices=max_devices,
            replayed=replayed,
        )

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "phase": self.phase,
            "created_at": self.created_at.isoformat(),
            "active_device_count": self.active_device_count,
            "max_devices": self.max_devices,
            "replayed": self.replayed,
        }

    @classmethod
    def from_json(cls, data: dict) -> "PendingDeactivation":
        if not isinstance(data, dict):
            raise ValueError("pending deactivation must be an object")
        schema_version = data.get("schema_version")
        request_id = data.get("request_id")
        fingerprint = data.get("fingerprint")
        phase = data.get("phase")
        active_device_count = data.get("active_device_count")
        max_devices = data.get("max_devices")
        replayed = data.get("replayed")
        if schema_version != 1:
            raise ValueError("unsupported pending deactivation schema")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("invalid pending request id")
        try:
            parsed_request_id = uuid.UUID(request_id.strip())
        except ValueError as exc:
            raise ValueError("invalid pending request id") from exc
        if request_id.strip() != str(parsed_request_id):
            raise ValueError("invalid pending request id")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint.startswith("sha256:")
            or len(fingerprint) != len("sha256:") + 64
            or any(char not in string.hexdigits for char in fingerprint[len("sha256:"):])
        ):
            raise ValueError("invalid pending fingerprint")
        if phase not in {"resolving", "server_committed"}:
            raise ValueError("invalid pending phase")
        if not isinstance(replayed, bool):
            raise ValueError("invalid pending replay state")
        if (active_device_count is None) != (max_devices is None):
            raise ValueError("incomplete pending device summary")
        if active_device_count is not None and (
            isinstance(active_device_count, bool)
            or not isinstance(active_device_count, int)
            or active_device_count < 0
        ):
            raise ValueError("invalid pending active device count")
        if max_devices is not None and (
            isinstance(max_devices, bool)
            or not isinstance(max_devices, int)
            or max_devices < 1
        ):
            raise ValueError("invalid pending maximum devices")
        if phase == "server_committed" and active_device_count is None:
            raise ValueError("committed pending state needs a device summary")
        created_at = data.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValueError("invalid pending creation time")
        try:
            parsed_created_at = _dt(created_at)
        except ValueError as exc:
            raise ValueError("invalid pending creation time") from exc
        return cls(
            schema_version=schema_version,
            request_id=request_id.strip(),
            fingerprint=fingerprint,
            phase=phase,
            created_at=parsed_created_at,
            active_device_count=active_device_count,
            max_devices=max_devices,
            replayed=replayed,
        )


@dataclass(frozen=True)
class LicenseResult:
    status: str
    message: str
    cache: LicenseCache | None = None
    server_time: datetime | None = None
    retry_after_seconds: int | None = None
    active_device_count: int | None = None
    max_devices: int | None = None
    request_id: str | None = None
    deactivation_status: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class LicenseStatus:
    allowed: bool
    code: str
    message: str
    cache: LicenseCache | None = None
    active_device_count: int | None = None
    max_devices: int | None = None
    request_id: str | None = None
    deactivation_status: str | None = None
    replayed: bool = False
    low_privilege_actions: tuple[str, ...] = field(
        default=("activate", "contact", "open_data_folder", "exit")
    )


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.fromtimestamp(0)
    return datetime.fromisoformat(str(value))


def _cache_fingerprint(cache: LicenseCache) -> str:
    payload = json.dumps(
        {
            "license_id": cache.license_id,
            "device_id": cache.device_id,
            "device_hash": cache.device_hash,
            "lease_token": cache.lease_token,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
