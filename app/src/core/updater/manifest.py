from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Mapping

from .constants import (
    ALLOWED_RELEASE_REPOSITORIES,
    KEY_ID,
    MAX_MANIFEST_BYTES,
    MAX_SIG_BASE64_BYTES,
    MAX_URL_BYTES,
    RELEASES_URL,
)
from .models import Asset, UpdateError, UpdateManifest

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_BUILD = re.compile(r"^[0-9]{8}\.[0-9]+$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^Lulu-[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}\.[0-9]+-arm64\.(zip|dmg)$")
_TOP = {"schema", "app", "channel", "latest_version", "latest_build", "min_supported_version", "key_id", "mandatory", "published_at", "notes_md", "assets"}
_ASSET_DMG = {"kind", "filename", "url", "sha256", "size"}
_ASSET_ZIP = _ASSET_DMG | {"tree_manifest_sha256"}


def version_tuple(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise UpdateError("manifest", "更新清单格式异常")
    return tuple(int(part) for part in value.split("."))


def build_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"^[0-9]+(?:\.[0-9]+)*$", value):
        raise UpdateError("manifest", "更新清单格式异常")
    return tuple(int(part) for part in value.split("."))


def minimal_key_id(data: bytes) -> str:
    if len(data) > MAX_MANIFEST_BYTES:
        raise UpdateError("manifest", "更新清单过大")
    try:
        value = json.loads(data)
    except Exception as exc:
        raise UpdateError("manifest", "更新清单格式异常") from exc
    if not isinstance(value, dict):
        raise UpdateError("manifest", "更新清单格式异常")
    for key, expected in (("schema", 1), ("app", "Lulu")):
        if value.get(key) != expected:
            raise UpdateError("manifest", "更新清单格式异常")
    key_id = value.get("key_id")
    if not isinstance(key_id, str) or len(key_id.encode()) > 96:
        raise UpdateError("manifest", "更新清单格式异常")
    return key_id


def decode_signature(value: bytes) -> bytes:
    if len(value) > MAX_SIG_BASE64_BYTES or value.count(b"\n") > 1:
        raise UpdateError("signature", "更新清单无法验证")
    if value.endswith(b"\n"):
        value = value[:-1]
    if not value or b"\r" in value or b"\n" in value:
        raise UpdateError("signature", "更新清单无法验证")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError("signature", "更新清单无法验证") from exc


def verify_signature(data: bytes, encoded_signature: bytes, public_keys: Mapping[str, str]) -> str:
    requested = minimal_key_id(data)
    signature = decode_signature(encoded_signature)
    ordered = ([requested] if requested in public_keys else []) + [key for key in public_keys if key != requested]
    if not ordered:
        raise UpdateError("signature", "更新清单无法验证")
    for key_id in ordered:
        pem = public_keys.get(key_id, "")
        if not pem: continue
        with tempfile.TemporaryDirectory(prefix="lulu-update-verify-") as raw:
            root = Path(raw)
            root.chmod(0o700)
            manifest = root / "version.json"
            sig = root / "version.sig.bin"
            pub = root / "public.pem"
            for path, content in ((manifest, data), (sig, signature), (pub, pem.encode())):
                fd = path.open("xb")
                with fd:
                    fd.write(content)
                path.chmod(0o600)
            try:
                result = subprocess.run(
                    ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig), str(manifest)],
                    capture_output=True, timeout=10, check=False,
                )
            except Exception:
                continue
            output = (result.stdout + result.stderr)[:8192]
            if result.returncode == 0 and b"Verified OK" in output:
                return key_id
    raise UpdateError("signature", "更新清单无法验证")


def _text(value, *, maximum: int, pattern=None) -> str:
    if not isinstance(value, str) or len(value.encode()) > maximum or (pattern and not pattern.fullmatch(value)):
        raise UpdateError("manifest", "更新清单格式异常")
    return value


def _asset(value, *, name: str, version: str, build: str) -> Asset:
    expected_fields = _ASSET_ZIP if name == "macos_arm64" else _ASSET_DMG
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise UpdateError("manifest", "更新清单格式异常")
    kind = "zip" if name == "macos_arm64" else "dmg"
    filename = _text(value["filename"], maximum=160, pattern=_FILENAME)
    expected = f"Lulu-{version}-{build}-arm64.{kind}"
    url = _text(value["url"], maximum=MAX_URL_BYTES)
    expected_urls = {
        f"https://github.com/{repository}/releases/download/v{version}/{expected}"
        for repository in ALLOWED_RELEASE_REPOSITORIES
    }
    if value["kind"] != kind or filename != expected or url not in expected_urls:
        raise UpdateError("manifest", "更新资产地址异常")
    digest = _text(value["sha256"], maximum=64, pattern=_HASH)
    size = value["size"]
    if type(size) is not int or not 1 <= size <= 3221225472:
        raise UpdateError("manifest", "更新清单格式异常")
    tree_sha = _text(value["tree_manifest_sha256"], maximum=64, pattern=_HASH) if name == "macos_arm64" else ""
    return Asset(kind, filename, url, digest, size, tree_sha)


def parse_manifest(data: bytes, *, local_channel: str, verified_key_id: str | None = None) -> UpdateManifest:
    if len(data) > MAX_MANIFEST_BYTES:
        raise UpdateError("manifest", "更新清单过大")
    try:
        value = json.loads(data)
    except Exception as exc:
        raise UpdateError("manifest", "更新清单格式异常") from exc
    if not isinstance(value, dict) or set(value) != _TOP:
        raise UpdateError("manifest", "更新清单格式异常")
    if value["schema"] != 1 or value["app"] != "Lulu" or value["channel"] != local_channel or type(value["key_id"]) is not str or len(value["key_id"].encode()) > 96 or type(value["mandatory"]) is not bool:
        raise UpdateError("manifest", "更新清单格式异常")
    version = _text(value["latest_version"], maximum=32, pattern=_VERSION)
    build = _text(value["latest_build"], maximum=32, pattern=_BUILD)
    minimum = _text(value["min_supported_version"], maximum=32, pattern=_VERSION)
    published = _text(value["published_at"], maximum=40)
    try:
        datetime.fromisoformat(published)
    except ValueError as exc:
        raise UpdateError("manifest", "更新清单格式异常") from exc
    notes = _text(value["notes_md"], maximum=20000)
    if any(len(line.encode()) > 1000 for line in notes.splitlines()) or "<" in notes or ">" in notes:
        raise UpdateError("manifest", "更新说明格式异常")
    assets = value["assets"]
    if not isinstance(assets, dict) or set(assets) != {"macos_arm64", "macos_arm64_dmg"}:
        raise UpdateError("manifest", "更新清单格式异常")
    return UpdateManifest(1, "Lulu", local_channel, version, build, minimum, verified_key_id or value["key_id"], value["mandatory"], published, notes, _asset(assets["macos_arm64"], name="macos_arm64", version=version, build=build), _asset(assets["macos_arm64_dmg"], name="macos_arm64_dmg", version=version, build=build))


def canonical_sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def is_safe_manual_url(url: str) -> bool:
    if url == RELEASES_URL:
        return True
    return any(
        bool(
            re.fullmatch(
                rf"https://github\.com/{re.escape(repository)}/releases/download/"
                r"v[0-9]+\.[0-9]+\.[0-9]+/Lulu-[0-9]+\.[0-9]+\.[0-9]+-"
                r"[0-9]{8}\.[0-9]+-arm64\.dmg",
                url or "",
            )
        )
        for repository in ALLOWED_RELEASE_REPOSITORIES
    )
