"""External-candidate-only license service construction."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from ._service_core import LicenseService
from .store import StrictKeyringStore
from .transport import HttpsLicenseTransport, MisconfiguredLicenseTransport


_CONFIG_NAME = "license_runtime_config.json"


def _config_path() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[3] / _CONFIG_NAME
    return Path(getattr(sys, "_MEIPASS", "")) / _CONFIG_NAME


def _load_external_config() -> dict:
    try:
        payload = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_endpoint(payload: dict) -> str:
    if payload.get("schema_version") != 1:
        return ""
    if payload.get("build_profile") != "external_candidate" or payload.get("environment") != "production":
        return ""
    endpoint = str(payload.get("endpoint") or "").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    lowered = endpoint.lower()
    if any(marker in lowered for marker in ("localhost", "127.0.0.1", "::1", "example.invalid", "mock", "staging", "stage", "test", "local")):
        return ""
    return endpoint


def build_core_license_service() -> LicenseService:
    endpoint = _safe_endpoint(_load_external_config())
    transport = HttpsLicenseTransport(endpoint) if endpoint else MisconfiguredLicenseTransport("授权服务配置不完整或不安全。")
    return LicenseService(StrictKeyringStore(), transport)


def build_external_service():
    if (getattr(sys, 'frozen', False) and
            Path(sys.executable) == Path('/Applications/Lulu.app/Contents/MacOS/Lulu')):
        from .core_service import CoreLicenseService
        return CoreLicenseService()
    return build_core_license_service()
