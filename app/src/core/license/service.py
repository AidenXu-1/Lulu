from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys

from ._service_core import LicenseService
from .store import StrictKeyringStore
from .transport import HttpsLicenseTransport, LicenseTransport, MisconfiguredLicenseTransport

LICENSE_RUNTIME_CONFIG = "license_runtime_config.json"
_REAL_MODES = {"production", "staging"}
_LOCAL_MODES = {"", "local", "mock"}


def build_default_service() -> LicenseService:
    return LicenseService(StrictKeyringStore(), build_transport_from_environment())


def build_transport_from_environment() -> LicenseTransport:
    """Select a source/staging/production transport without secret fallback.

    Local source checkouts keep mock by default. Staging/production must be
    explicit and complete; any missing or suspicious config returns a fail-closed
    transport instead of silently falling back to mock.
    """

    config = _load_license_runtime_config()
    frozen = bool(getattr(sys, "frozen", False))
    env_mode = "" if frozen else (os.environ.get("LULU_LICENSE_ENV") or os.environ.get("LULU_LICENSE_TRANSPORT") or "").strip().lower()
    file_mode = str(config.get("environment") or config.get("transport") or "").strip().lower()
    mode = env_mode or file_mode or "mock"
    endpoint = (str(config.get("endpoint") or "") if frozen else (os.environ.get("LULU_LICENSE_ENDPOINT") or str(config.get("endpoint") or ""))).strip()

    if frozen and config.get("build_profile") != "external_candidate":
        return MisconfiguredLicenseTransport("构建身份与授权配置不一致。")

    if mode in _REAL_MODES:
        error = _validate_real_license_config(mode, endpoint)
        if error:
            return MisconfiguredLicenseTransport(error)
        try:
            return HttpsLicenseTransport(endpoint)
        except Exception:  # noqa: BLE001
            return MisconfiguredLicenseTransport("授权服务配置不完整或不安全。")
    if mode in _LOCAL_MODES:
        if frozen:
            return MisconfiguredLicenseTransport("对外候选不允许本地授权模式。")
        from .mock_transport import MockLicenseTransport

        return MockLicenseTransport()
    return MisconfiguredLicenseTransport("授权服务环境配置无效。")


def _load_license_runtime_config() -> dict:
    path = _license_runtime_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"environment": "invalid"}
    return data if isinstance(data, dict) else {"environment": "invalid"}


def _license_runtime_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path.cwd())) / LICENSE_RUNTIME_CONFIG
    return Path(__file__).resolve().parents[3] / LICENSE_RUNTIME_CONFIG


def _validate_real_license_config(mode: str, endpoint: str) -> str | None:
    if not endpoint:
        return "授权服务配置不完整。"
    lowered = endpoint.lower()
    blocked_common = ("localhost", "127.0.0.1", "::1", "example.invalid", "mock")
    if any(marker in lowered for marker in blocked_common):
        return "授权服务配置不完整或不安全。"
    if mode == "production" and any(marker in lowered for marker in ("staging", "stage", "test", "local")):
        return "授权服务配置不完整或不安全。"
    if mode == "staging" and any(marker in lowered for marker in ("production", "prod")):
        return "授权服务配置不完整或不安全。"
    try:
        HttpsLicenseTransport(endpoint)
    except Exception:  # noqa: BLE001
        return "授权服务配置不完整或不安全。"
    return None
