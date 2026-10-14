from __future__ import annotations

import re

_SENSITIVE_KEYS = (
    "license_code",
    "license_id",
    "lease_token",
    "device_id",
    "device_hash",
    "request_fingerprint",
    "fingerprint",
    "authorization",
    "admin_token",
    "worker_secret",
    "d1_binding",
    "cloudflare_secret",
)

_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_RE = re.compile(
    r"(?i)\b(lease|token|secret|license_code|license_id|device_id|device_hash|"
    r"lease_token|request_fingerprint|fingerprint)\b\s*[:=]\s*['\"]?[^,'\"\s}]+"
)
_MOCK_CODE_RE = re.compile(r"\bLULU-[A-Z0-9-]{4,}\b")
_HASH_RE = re.compile(r"\bsha256:[a-f0-9]{24,}\b", re.IGNORECASE)
_WORKER_DETAIL_RE = re.compile(r"(?i)\b(D1|Worker|Cloudflare)\b[^。；;\n]*")


def redact_sensitive(value) -> str:
    text = str(value)
    text = _AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _TOKEN_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _MOCK_CODE_RE.sub("[REDACTED-LICENSE]", text)
    text = _HASH_RE.sub("sha256:[REDACTED]", text)
    text = _WORKER_DETAIL_RE.sub("[授权服务内部错误已隐藏]", text)
    for key in _SENSITIVE_KEYS:
        text = re.sub(
            rf"(?i)([\"']?{re.escape(key)}[\"']?\s*:\s*)[\"'][^\"']+[\"']",
            rf"\1\"[REDACTED]\"",
            text,
        )
    return text
