from __future__ import annotations

import hashlib
import platform
import secrets


def new_device_salt() -> str:
    return secrets.token_urlsafe(32)


def privacy_preserving_device_hash(salt: str) -> str:
    """Return a stable per-install hash without exposing raw device identifiers.

    Phase 1 intentionally avoids hostname, username, paths, and raw hardware
    serials. The install salt lives in secure storage; reinstall/migration may
    consume a new device slot, matching the product spec.
    """

    material = f"lulu-license-v1|{platform.system()}|{platform.machine()}|{salt}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
