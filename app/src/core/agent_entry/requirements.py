"""Load CLI/Core expectations only from one verified generation-9 App manifest."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .verification_cache import verify_runtime_component_manifest as verify_component_manifest
from .installed_materials import component_manifest_path
from .handshake import PeerRequirement
from ...version import channel_for_profile


def load_peer_requirements(
    app_bundle: Path,
    *,
    audit_session_id: int,
) -> tuple[PeerRequirement, PeerRequirement, dict[str, object]]:
    app = Path(app_bundle)
    manifest_path = component_manifest_path(app)
    verification = verify_component_manifest(app, manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = payload["catalog"]
    common = {
        "user_id": os.getuid(),
        "audit_session_id": audit_session_id,
        "product_version": payload["product_version"],
        "product_build": payload["product_build"],
        "source_revision": payload["source_revision"],
        "generation": payload["candidate_generation"],
        "manifest_sha256": verification["component_manifest_sha256"],
        "catalog_sha256": catalog["sha256"],
        "protocol_version": payload["cli_protocol"]["min"],
    }
    cli = PeerRequirement.from_file(
        role="agent_cli",
        path=app / "Contents/MacOS/lulu-agent-cli",
        **common,
    )
    core = PeerRequirement.from_file(
        role="agent_core",
        path=app / "Contents/MacOS/lulu-agent-core",
        **common,
    )
    return cli, core, verification


def load_gui_peer_requirements(
    app_bundle: Path,
    *,
    audit_session_id: int,
) -> tuple[PeerRequirement, PeerRequirement, dict[str, object]]:
    """Load the sealed GUI and Core identities from the same verified App."""
    app = Path(app_bundle)
    _cli, core, verification = load_peer_requirements(
        app,
        audit_session_id=audit_session_id,
    )
    payload = json.loads(
        component_manifest_path(app).read_text(
            encoding="utf-8"
        )
    )
    catalog = payload["catalog"]
    gui = PeerRequirement.from_file(
        role="gui",
        path=app / "Contents/MacOS/Lulu",
        user_id=os.getuid(),
        audit_session_id=audit_session_id,
        product_version=payload["product_version"],
        product_build=payload["product_build"],
        source_revision=payload["source_revision"],
        generation=payload["candidate_generation"],
        manifest_sha256=verification["component_manifest_sha256"],
        catalog_sha256=catalog["sha256"],
        protocol_version=payload["cli_protocol"]["min"],
    )
    return gui, core, verification


def project_component_identity(
    *,
    resource_root: Path,
    adapter_id: str,
) -> dict[str, object]:
    root = Path(resource_root)
    profile = json.loads((root / "build_profile.json").read_text(encoding="utf-8"))
    manifest_path = component_manifest_path(root.parent.parent)
    verify_component_manifest(root.parent.parent, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        profile.get("schema") != 1
        or profile.get("profile") != "external_candidate"
        or manifest.get("schema_id") not in {"lulu.agent-component-manifest/v1", "lulu.agent-component-manifest/v2"}
        or manifest.get("candidate_generation") != 9
        or manifest.get("product_version") != profile.get("version")
        or manifest.get("product_build") != profile.get("build")
        or manifest.get("source_revision") != profile.get("source_commit")
        or adapter_id not in {"agent_cli", "agent_core"}
    ):
        raise RuntimeError("AGENT_ENTRY_VERSION_MISMATCH")
    shared = {
        "schema_id": "lulu.product-identity/v1",
        "product_family": "Lulu",
        "product_version": manifest["product_version"],
        "product_build": manifest["product_build"],
        "profile": "external_candidate",
        "channel": channel_for_profile("external_candidate"),
        "display_name": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "source_commit": profile["source_commit"],
        "source_tree": profile["source_tree"],
        "product_source_sha256": profile["product_source_sha256"],
        "cli_protocol_version": manifest["cli_protocol"]["min"],
        "installation_identity_scope": "shared_lulu_installation",
        "installation_identity": {
            "scope": "shared_lulu_installation",
            "status": "measured_component_receipt_required",
            "agent_specific_copy": "forbidden",
        },
        "shared_runtime": {
            "analysis": "single",
            "python": "single",
            "core": "single",
            "resources": "single",
            "storage_layout": "lulu.shared-storage/v1",
        },
        "protected_capabilities": {
            "state": "unavailable",
            "reason_code": "AGENT_ENTRY_RECEIPT_REQUIRED",
        },
    }
    canonical = json.dumps(shared, sort_keys=True, separators=(",", ":")).encode("utf-8")
    executable = (
        "Contents/MacOS/lulu-agent-cli"
        if adapter_id == "agent_cli"
        else "Contents/MacOS/lulu-agent-core"
    )
    return {
        "shared": shared,
        "identity_digest": hashlib.sha256(canonical).hexdigest(),
        "adapter_id": adapter_id,
        "adapter": {"executable": executable, "mode": "console", "stdio": adapter_id == "agent_cli"},
    }
