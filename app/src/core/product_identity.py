"""Read-only product identity shared by the GUI and Agent CLI adapters."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..version import __build__, __version__, channel_for_profile


_SOURCE_ID_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_SOURCE_IDENTITIES = frozenset({("0.3.1", "20260815.3")})
_PROFILE_IDENTITIES = {
    "external_candidate": {
        "app_name": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "artifact_prefix": "Lulu",
        "upload_target": "github-release-candidate",
        "entry": "run_external.py",
    },
    "trusted_test": {
        "app_name": "Lulu Trusted Test",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "artifact_prefix": "Lulu-Trusted-Test",
        "upload_target": "none",
        "entry": "run_trusted_test.py",
    },
}


class ProductIdentityError(RuntimeError):
    """The sealed package identity cannot be trusted."""


def _read_owned_regular_json(path: Path, label: str) -> dict[str, object]:
    if not path.is_absolute():
        raise ProductIdentityError(f"{label}路径必须显式为绝对路径")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductIdentityError(f"{label}缺失") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise ProductIdentityError(f"{label}必须是当前用户只读可信的普通文件")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ProductIdentityError(f"{label}无法解析") from exc
    if type(payload) is not dict:
        raise ProductIdentityError(f"{label}必须是JSON对象")
    return payload


def _validated_profile(
    payload: dict[str, object],
    *,
    product_version: str,
    product_build: str,
) -> tuple[str, dict[str, str]]:
    profile = payload.get("profile")
    expected = _PROFILE_IDENTITIES.get(profile)
    if (
        payload.get("schema") != 1
        or expected is None
        or payload.get("version") != product_version
        or payload.get("build") != product_build
        or any(payload.get(key) != value for key, value in expected.items())
        or not _SOURCE_ID_RE.fullmatch(str(payload.get("source_commit", "")))
        or not _SOURCE_ID_RE.fullmatch(str(payload.get("source_tree", "")))
        or not _SHA256_RE.fullmatch(str(payload.get("product_source_sha256", "")))
    ):
        raise ProductIdentityError("构建身份与产品版本不一致")
    return str(profile), expected


def _validated_manifest(
    payload: dict[str, object],
    profile: str,
) -> tuple[str, str]:
    adapters = payload.get("adapters")
    shared_runtime = payload.get("shared_runtime")
    trust_evidence = payload.get("trust_evidence")
    installation_identity = payload.get("installation_identity")
    candidate_identity = payload.get("candidate_identity")
    product_version = payload.get("product_version")
    product_build = payload.get("product_build")
    candidate_identity_valid = candidate_identity == {
        "version": __version__,
        "build": __build__,
        "channel": "candidate",
        "release_state": "non_release",
        "source_evidence": "source_unverified",
        "artifact_evidence": "unverified_until_dist_finalize",
    }
    legacy_source_identity = candidate_identity is None and (
        product_version,
        product_build,
    ) in _LEGACY_SOURCE_IDENTITIES
    if (
        payload.get("schema_id") != "lulu.agent-product-manifest/v1"
        or payload.get("schema_revision") != 1
        or payload.get("product_family") != "Lulu"
        or not (candidate_identity_valid or legacy_source_identity)
        or payload.get("cli_protocol_version") != "1.0.0"
        or payload.get("installation_identity_scope") != "shared_lulu_installation"
        or profile not in payload.get("supported_profiles", [])
        or type(adapters) is not dict
        or frozenset(adapters) != {"gui", "agent_cli"}
        or type(shared_runtime) is not dict
        or shared_runtime
        != {
            "analysis": "single",
            "python": "single",
            "core": "single",
            "resources": "single",
            "storage_layout": "lulu.shared-storage/v1",
        }
        or type(trust_evidence) is not dict
        or installation_identity
        != {
            "scope": "shared_lulu_installation",
            "status": "unverified_until_real_install",
            "agent_specific_copy": "forbidden",
        }
    ):
        raise ProductIdentityError("Agent产品清单与共享产品身份不一致")
    expected_adapters = {
        "gui": ("windowed", False),
        "agent_cli": ("console", True),
    }
    for adapter_id, (mode, stdio) in expected_adapters.items():
        adapter = adapters.get(adapter_id)
        executable = adapter.get("executable") if type(adapter) is dict else None
        if (
            type(adapter) is not dict
            or adapter.get("mode") != mode
            or adapter.get("stdio") is not stdio
            or type(executable) is not str
            or not executable.startswith("Contents/MacOS/")
            or ".." in Path(executable).parts
            or Path(executable).is_absolute()
        ):
            raise ProductIdentityError("Agent产品适配器定义无效")
    return str(product_version), str(product_build)


def project_product_identity(
    *,
    build_profile_path: Path,
    manifest_path: Path,
    adapter_id: str,
) -> dict[str, object]:
    """Project one adapter from the same sealed, zero-effect product identity."""

    profile_payload = _read_owned_regular_json(Path(build_profile_path), "构建身份")
    manifest_payload = _read_owned_regular_json(Path(manifest_path), "Agent产品清单")
    manifest_profile = profile_payload.get("profile")
    if not isinstance(manifest_profile, str):
        raise ProductIdentityError("构建身份与产品版本不一致")
    product_version, product_build = _validated_manifest(
        manifest_payload,
        manifest_profile,
    )
    profile, expected_profile = _validated_profile(
        profile_payload,
        product_version=product_version,
        product_build=product_build,
    )
    adapters = manifest_payload["adapters"]
    if adapter_id not in adapters:
        raise ProductIdentityError("产品适配器不存在")
    shared = {
        "schema_id": "lulu.product-identity/v1",
        "product_family": "Lulu",
        "product_version": product_version,
        "product_build": product_build,
        "profile": profile,
        "channel": channel_for_profile(profile),
        "display_name": expected_profile["app_name"],
        "bundle_id": expected_profile["bundle_id"],
        "source_commit": profile_payload["source_commit"],
        "source_tree": profile_payload["source_tree"],
        "product_source_sha256": profile_payload["product_source_sha256"],
        "cli_protocol_version": manifest_payload["cli_protocol_version"],
        "installation_identity_scope": manifest_payload["installation_identity_scope"],
        "installation_identity": dict(manifest_payload["installation_identity"]),
        "shared_runtime": dict(manifest_payload["shared_runtime"]),
        "protected_capabilities": {
            "state": "unavailable",
            "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
        },
    }
    canonical = json.dumps(
        shared,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "shared": shared,
        "identity_digest": hashlib.sha256(canonical).hexdigest(),
        "adapter_id": adapter_id,
        "adapter": dict(adapters[adapter_id]),
    }
