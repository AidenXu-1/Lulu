"""Self-contained installation metadata, sealed before the App's final signature.

The descriptor contains no digest of its enclosing App. The final component
manifest is reconstructed outside the bundle, using the existing verifier.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .component_manifest import build_component_manifest, _read_regular
from .delivery_identity import MANIFEST_NAME, strict_json, verify_final_signature

DESCRIPTOR = Path("Contents/Resources/standalone-install.v1.json")
SCHEMA = "lulu.standalone-install-descriptor/v1"
LOCAL_SCHEMA = "lulu.standalone-install-descriptor/v2"
_FIELDS = {"schema_id", "product_version", "product_build", "source_revision",
           "candidate_generation", "cli_protocol", "capability_closed_set"}


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _validate(value):
    local = isinstance(value, dict) and value.get('schema_id') == LOCAL_SCHEMA
    if (type(value) is not dict or set(value) != (_FIELDS | {'storage_scheme'} if local else _FIELDS)
            or value.get("schema_id") not in {SCHEMA, LOCAL_SCHEMA} or value.get("candidate_generation") != 9
            or value.get("cli_protocol") != {"min": "1.0.0", "max": "2.0.0"}):
        raise RuntimeError("APP_INSTALL_DESCRIPTOR_INVALID")
    if local and value['storage_scheme'] != 'local-integrity-v1':
        raise RuntimeError('APP_INSTALL_DESCRIPTOR_INVALID')
    for name, pattern in (("product_version", r"[0-9]+\.[0-9]+\.[0-9]+"),
                          ("product_build", r"[0-9]{8}\.[0-9]+"),
                          ("source_revision", r"[a-f0-9]{40}")):
        if type(value[name]) is not str or re.fullmatch(pattern, value[name]) is None:
            raise RuntimeError("APP_INSTALL_DESCRIPTOR_INVALID")
    capabilities = value["capability_closed_set"]
    if (type(capabilities) is not list or not 1 <= len(capabilities) <= 1000
            or any(type(item) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item) is None for item in capabilities)
            or capabilities != sorted(set(capabilities))):
        raise RuntimeError("APP_INSTALL_DESCRIPTOR_INVALID")
    return value


def prepare_install_descriptor(app, *, product_version, product_build, source_revision,
                               capability_closed_set):
    """Packaging only; must precede the final App signature."""
    payload = {
        "schema_id": SCHEMA, "product_version": product_version,
        "product_build": product_build, "source_revision": source_revision,
        "candidate_generation": 9, "cli_protocol": {"min": "1.0.0", "max": "2.0.0"},
        "capability_closed_set": sorted(capability_closed_set),
    }
    # 4.2.1 is the first local-storage release; older signed Apps retain v1.
    if tuple(int(n) for n in product_version.split('.')) >= (4, 2, 1):
        payload.update(schema_id=LOCAL_SCHEMA, storage_scheme='local-integrity-v1')
    payload = _validate(payload)
    path = Path(app)/DESCRIPTOR
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise RuntimeError("PRODUCT_PATH_UNSAFE")
    # Refuse an existing descriptor rather than mutate a previously signed App.
    with path.open("xb") as stream:
        stream.write(_encoded(payload))
    path.chmod(0o644)


def uses_local_storage(app):
    import os
    if not os.path.lexists(Path(app)/DESCRIPTOR):
        return False
    raw, _ = _read_regular(Path(app)/DESCRIPTOR, reason_code='APP_INSTALL_DESCRIPTOR_INVALID')
    return _validate(strict_json(raw))['schema_id'] == LOCAL_SCHEMA


@dataclass(frozen=True)
class AppInstallSource:
    manifest: dict
    manifest_bytes: bytes
    receipt: dict


def inspect_install_source(app: Path) -> AppInstallSource:
    app = Path(app)
    if app.resolve(strict=True) != app or app.name != "Lulu.app":
        raise RuntimeError("PRODUCT_PATH_UNSAFE")
    if not (app/DESCRIPTOR).exists():
        raise RuntimeError("APP_INSTALL_DESCRIPTOR_REQUIRED")
    verify_final_signature(app)
    raw, _ = _read_regular(app/DESCRIPTOR, reason_code="APP_INSTALL_DESCRIPTOR_INVALID")
    if len(raw) > 128 * 1024:
        raise RuntimeError("APP_INSTALL_DESCRIPTOR_INVALID")
    descriptor = _validate(strict_json(raw))
    profile_raw, _ = _read_regular(app/"Contents/Resources/build_profile.json",
                                  reason_code="APP_INSTALL_DESCRIPTOR_INVALID")
    profile = strict_json(profile_raw)
    if (profile.get("schema") != 1 or profile.get("profile") != "external_candidate"
            or any(profile.get(key) != descriptor[field] for key, field in (
                ("version", "product_version"), ("build", "product_build"),
                ("source_commit", "source_revision")))):
        raise RuntimeError("APP_INSTALL_DESCRIPTOR_INVALID")
    manifest = build_component_manifest(
        app_bundle=app, manifest_path=app.parent/MANIFEST_NAME,
        product_version=descriptor["product_version"], product_build=descriptor["product_build"],
        source_revision=descriptor["source_revision"], candidate_generation=9,
        cli_protocol_min=descriptor["cli_protocol"]["min"],
        cli_protocol_max=descriptor["cli_protocol"]["max"],
        catalog_relative_path="Contents/Resources/src/agent_cli/capability_catalog.json",
        capability_closed_set=descriptor["capability_closed_set"],
    )
    encoded = _encoded(manifest)
    receipt = {
        "schema_id": "lulu.verified-app-install-source/v1", "source_kind": "signed_app",
        "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
        "component_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "app_component_set_digest": manifest["app_component_set_digest"],
        "candidate_generation": 9,
        "core_cdhash": core_cdhash(app),
    }
    return AppInstallSource(manifest, encoded, receipt)


def core_cdhash(app):
    result = subprocess.run(
        ['/usr/bin/codesign', '-d', '--verbose=4', str(app/'Contents/MacOS/lulu-agent-core')],
        capture_output=True, text=True, timeout=10, check=False,
    )
    match = re.search(r'^CDHash=([a-f0-9]{40})$', result.stderr, re.M)
    if result.returncode or match is None:
        raise RuntimeError('APP_INSTALL_CORE_IDENTITY_REQUIRED')
    return match[1]
