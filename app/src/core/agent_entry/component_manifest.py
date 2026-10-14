"""Closed-set component manifest generation and verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
from typing import Iterable

from .canonical_app_tree import canonical_app_tree_identity
from .delivery_identity import (
    MANIFEST_SCHEMA as EXTERNAL_SCHEMA, MANIFEST_NAME,
    delivery_app_tree_identity, delivery_app_tree_records, verify_final_signature,
    strict_json, verify_delivery_marker, DELIVERY_MARKER, LINK_POLICY,
)


SCHEMA_ID = "lulu.agent-component-manifest/v1"
REQUIRED_GENERATION = 9
INSTALLATION_PROFILE = "circle_internal"
_SHA256_LENGTH = 64
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_id",
        "product_version",
        "product_build",
        "source_revision",
        "candidate_generation",
        "installation_profile",
        "cli_protocol",
        "catalog",
        "capability_closed_set",
        "skill_source",
        "skill_compatibility",
        "launcher",
        "launch_agent",
        "transport_policy_sha256",
        "build_profile_sha256",
        "components",
        "app_component_set_digest",
        "contains_secret_material",
    }
)


class ComponentManifestError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.state = "update_required"
        self.effect_count = 0


def _fail(reason_code: str) -> ComponentManifestError:
    return ComponentManifestError(reason_code)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular(path: Path, *, reason_code: str) -> tuple[bytes, os.stat_result]:
    try:
        expected = path.lstat()
    except OSError as exc:
        raise _fail(reason_code) from exc
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise _fail(reason_code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino, actual.st_mode) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_mode,
            ):
                raise _fail(reason_code)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
        finally:
            os.close(descriptor)
    except ComponentManifestError:
        raise
    except OSError as exc:
        raise _fail(reason_code) from exc
    return content, actual


def _relative_files(app_bundle: Path, manifest_path: Path) -> list[Path]:
    files: list[Path] = []
    try:
        manifest_relative = manifest_path.relative_to(app_bundle).as_posix()
    except ValueError as exc:
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH") from exc
    for root, directories, names in os.walk(app_bundle, topdown=True, followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            child = root_path / directory
            if child.is_symlink():
                raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
        for name in names:
            path = root_path / name
            relative = path.relative_to(app_bundle).as_posix()
            if relative != manifest_relative:
                files.append(path)
    return sorted(files, key=lambda item: item.relative_to(app_bundle).as_posix())


def _role(relative_path: str) -> str:
    fixed = {
        "Contents/MacOS/Lulu": "gui",
        "Contents/MacOS/lulu-agent-cli": "agent_cli",
        "Contents/MacOS/lulu-agent-core": "agent_core",
        "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist": "launch_agent",
        "Contents/Resources/LuluTransportPolicy.v1.plist": "transport_policy",
        "Contents/Resources/build_profile.json": "build_profile",
        "Contents/Resources/src/agent_cli/capability_catalog.json": "catalog",
    }
    if relative_path.startswith("Contents/Resources/skills/lulu-agent/"):
        return "skill_source"
    return fixed.get(relative_path, "runtime_resource")


def _component_record(app_bundle: Path, path: Path) -> dict[str, object]:
    content, metadata = _read_regular(path, reason_code="AGENT_ENTRY_COMPONENT_MISMATCH")
    relative = path.relative_to(app_bundle).as_posix()
    return {
        "path": relative,
        "role": _role(relative),
        "type": "regular",
        "size": len(content),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "sha256": _sha256_bytes(content),
    }


def _canonical_digest(records: Iterable[dict[str, object]]) -> str:
    canonical = json.dumps(
        list(records), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _tree_digest(root: Path) -> str:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
            continue
        content, metadata = _read_regular(path, reason_code="AGENT_ENTRY_COMPONENT_MISMATCH")
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "size": len(content),
                "sha256": _sha256_bytes(content),
            }
        )
    return _canonical_digest(records)


def _json_regular(path: Path, *, reason_code: str) -> tuple[dict[str, object], bytes]:
    content, _metadata = _read_regular(path, reason_code=reason_code)
    try:
        payload = strict_json(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail(reason_code) from exc
    if type(payload) is not dict:
        raise _fail(reason_code)
    return payload, content


def _catalog_identity(payload: dict[str, object]) -> tuple[object, object]:
    nested = payload.get("catalog")
    source = nested if type(nested) is dict else payload
    return source.get("schema_id"), source.get("catalog_version", source.get("version"))


def _external_path(app: Path, manifest: Path) -> bool:
    if manifest == app / "Contents/Resources/agent_product_manifest.json":
        if os.path.lexists(app / DELIVERY_MARKER) or os.path.lexists(app / LINK_POLICY):
            raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
        return False
    if not manifest.is_absolute() or manifest.name != MANIFEST_NAME or app in manifest.parents:
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    return True


def _records_for(app: Path, manifest: Path) -> list[dict[str, object]]:
    if _external_path(app, manifest):
        verify_delivery_marker(app)
        return [dict(record, role=_role(str(record["path"])))
                for record in delivery_app_tree_records(app)
                if record["type"] != "directory"]
    return [_component_record(app, path) for path in _relative_files(app, manifest)]


def _identity_for(app: Path, manifest: Path) -> dict[str, object]:
    return (delivery_app_tree_identity(app) if _external_path(app, manifest)
            else canonical_app_tree_identity(app.resolve()))


def _verify_required_components(records, catalog_relative_path):
    by_path = {str(item["path"]): item for item in records}
    required = {
        "Contents/MacOS/Lulu",
        "Contents/MacOS/lulu-agent-cli",
        "Contents/MacOS/lulu-agent-core",
        "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
        "Contents/Resources/LuluTransportPolicy.v1.plist",
        "Contents/Resources/build_profile.json",
        catalog_relative_path,
        "Contents/Resources/skills/lulu-agent/SKILL.md",
        "Contents/Resources/skills/lulu-agent/manifest.json",
    }
    if not required.issubset(by_path) or any(by_path[path].get("type") != "regular" for path in required):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")


def build_component_manifest(
    *,
    app_bundle: Path,
    manifest_path: Path,
    product_version: str,
    product_build: str,
    source_revision: str,
    candidate_generation: int,
    cli_protocol_min: str,
    cli_protocol_max: str,
    catalog_relative_path: str,
    capability_closed_set: tuple[str, ...],
) -> dict[str, object]:
    app = Path(app_bundle)
    manifest = Path(manifest_path)
    catalog_path = app / catalog_relative_path
    catalog, catalog_bytes = _json_regular(
        catalog_path, reason_code="AGENT_ENTRY_VERSION_MISMATCH"
    )
    catalog_schema, catalog_version = _catalog_identity(catalog)
    records = _records_for(app, manifest)
    skill_root = app / "Contents/Resources/skills/lulu-agent"
    launch_path = app / "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist"
    policy_path = app / "Contents/Resources/LuluTransportPolicy.v1.plist"
    build_path = app / "Contents/Resources/build_profile.json"
    by_path = {str(item["path"]): item for item in records}
    _verify_required_components(records, catalog_relative_path)
    skill_manifest, _skill_bytes = _json_regular(
        skill_root / "manifest.json", reason_code="AGENT_ENTRY_VERSION_MISMATCH"
    )
    try:
        launch_policy = plistlib.loads(_read_regular(launch_path, reason_code="AGENT_ENTRY_COMPONENT_MISMATCH")[0])
        transport_policy = plistlib.loads(_read_regular(policy_path, reason_code="AGENT_ENTRY_COMPONENT_MISMATCH")[0])
    except Exception as exc:
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH") from exc
    if (
        launch_policy.get("Label") != "com.lulu.transcribe.agent-core"
        or launch_policy.get("MachServices") != {"com.lulu.transcribe.agent-core": True}
        or launch_policy.get("ProgramArguments") != ["/Applications/Lulu.app/Contents/MacOS/lulu-agent-core"]
        or transport_policy.get("Schema") != "lulu.agent-xpc-transport/v1"
        or transport_policy.get("CandidateGeneration") != 9
        or transport_policy.get("MachService") != "com.lulu.transcribe.agent-core"
        or transport_policy.get("Scope") != "current_user_gui_session"
        or any(transport_policy.get(key) is not False for key in ("NetworkListener", "SocketFallback", "PathFallback"))
    ):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    if (
        skill_manifest.get("candidate_generation") != candidate_generation
        or skill_manifest.get("product_identity", {}).get("product_version") != product_version
        or skill_manifest.get("product_identity", {}).get("product_build") != product_build
        or skill_manifest.get("product_identity", {}).get("source_revision") != source_revision
        or skill_manifest.get("cli_protocol", {}).get("minimum") != cli_protocol_min
        or skill_manifest.get("cli_protocol", {}).get("maximum_exclusive") != cli_protocol_max
        or skill_manifest.get("catalog_version") != catalog_version
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    if _external_path(app, manifest):
        verify_final_signature(app)
    return {
        "schema_id": EXTERNAL_SCHEMA if _external_path(app, manifest) else SCHEMA_ID,
        "product_version": product_version,
        "product_build": product_build,
        "source_revision": source_revision,
        "candidate_generation": candidate_generation,
        "installation_profile": INSTALLATION_PROFILE,
        "cli_protocol": {"min": cli_protocol_min, "max": cli_protocol_max},
        "catalog": {
            "path": catalog_relative_path,
            "schema_id": catalog_schema,
            "version": catalog_version,
            "sha256": _sha256_bytes(catalog_bytes),
        },
        "capability_closed_set": sorted(capability_closed_set),
        "skill_source": {
            "path": "Contents/Resources/skills/lulu-agent",
            "tree_sha256": _tree_digest(skill_root),
        },
        "skill_compatibility": {
            "schema_id": skill_manifest.get("schema_id"),
            "skill_version": skill_manifest.get("skill_version"),
            "candidate_generation": skill_manifest.get("candidate_generation"),
            "cli_protocol_min": skill_manifest.get("cli_protocol", {}).get("minimum"),
            "cli_protocol_max_exclusive": skill_manifest.get("cli_protocol", {}).get("maximum_exclusive"),
            "catalog_version": skill_manifest.get("catalog_version"),
        },
        "launcher": {
            "path": "~/.local/bin/lulu",
            "exact_target": "/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli",
        },
        "launch_agent": {
            "label": "com.lulu.transcribe.agent-core",
            "mach_service": "com.lulu.transcribe.agent-core",
            "plist_sha256": str(by_path[launch_path.relative_to(app).as_posix()]["sha256"]),
        },
        "transport_policy_sha256": str(by_path[policy_path.relative_to(app).as_posix()]["sha256"]),
        "build_profile_sha256": str(by_path[build_path.relative_to(app).as_posix()]["sha256"]),
        "components": records,
        "app_component_set_digest": _identity_for(app, manifest)["sha256"],
        "contains_secret_material": False,
    }


def verify_component_manifest(app_bundle: Path, manifest_path: Path) -> dict[str, object]:
    app = Path(app_bundle)
    manifest = Path(manifest_path)
    external = _external_path(app, manifest)
    payload, manifest_bytes = _json_regular(
        manifest, reason_code="AGENT_ENTRY_COMPONENT_MISMATCH"
    )
    if frozenset(payload) != _TOP_LEVEL_FIELDS or payload.get("schema_id") != (EXTERNAL_SCHEMA if external else SCHEMA_ID):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    if (
        payload.get("candidate_generation") != REQUIRED_GENERATION
        or payload.get("installation_profile") != INSTALLATION_PROFILE
        or payload.get("contains_secret_material") is not False
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    source = payload.get("source_revision")
    if (
        type(payload.get("product_version")) is not str
        or not payload.get("product_version")
        or type(payload.get("product_build")) is not str
        or not payload.get("product_build")
        or type(source) is not str
        or len(source) != 40
        or any(ch not in "0123456789abcdef" for ch in source)
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")

    protocol = payload.get("cli_protocol")
    if (
        type(protocol) is not dict
        or protocol != {"min": "1.0.0", "max": "2.0.0"}
        or type(payload.get("capability_closed_set")) is not list
        or not payload.get("capability_closed_set")
        or any(type(item) is not str or not item for item in payload["capability_closed_set"])
        or payload["capability_closed_set"] != sorted(set(payload["capability_closed_set"]))
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")

    catalog = payload.get("catalog")
    if type(catalog) is not dict or frozenset(catalog) != {"path", "schema_id", "version", "sha256"}:
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    if catalog.get("path") != "Contents/Resources/src/agent_cli/capability_catalog.json":
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    catalog_path = app / str(catalog["path"])
    actual_catalog, catalog_bytes = _json_regular(
        catalog_path, reason_code="AGENT_ENTRY_VERSION_MISMATCH"
    )
    actual_schema, actual_version = _catalog_identity(actual_catalog)
    if (
        actual_schema != catalog.get("schema_id")
        or actual_version != catalog.get("version")
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    if _sha256_bytes(catalog_bytes) != catalog.get("sha256"):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")

    expected_records = payload.get("components")
    if type(expected_records) is not list or not expected_records:
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    actual_records = _records_for(app, manifest)
    _verify_required_components(actual_records, str(catalog["path"]))
    if actual_records != expected_records:
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    if (
        _identity_for(app, manifest)["sha256"]
        != payload.get("app_component_set_digest")
    ):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    paths = [record.get("path") for record in expected_records if type(record) is dict]
    if (
        len(paths) != len(expected_records)
        or len(paths) != len(set(paths))
        or any(
            frozenset(record) != (
                {"path", "role", "type", "mode", "raw_target", "resolved_target", "target_type"}
                if external and record.get("type") == "symlink"
                else {"path", "role", "type", "size", "mode", "sha256"}
            ) or record.get("type") not in ({"regular", "symlink"} if external else {"regular"})
            for record in expected_records
        )
    ):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    skill = payload.get("skill_source")
    if (
        type(skill) is not dict
        or frozenset(skill) != {"path", "tree_sha256"}
        or skill.get("path") != "Contents/Resources/skills/lulu-agent"
        or _tree_digest(app / str(skill["path"])) != skill.get("tree_sha256")
    ):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    compatibility = payload.get("skill_compatibility")
    skill_manifest, _skill_bytes = _json_regular(
        app / str(skill.get("path", "")) / "manifest.json",
        reason_code="AGENT_ENTRY_VERSION_MISMATCH",
    )
    skill_protocol = skill_manifest.get("cli_protocol")
    skill_identity = skill_manifest.get("product_identity")
    if type(skill_protocol) is not dict or type(skill_identity) is not dict:
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    expected_compatibility = {
        "schema_id": skill_manifest.get("schema_id"),
        "skill_version": skill_manifest.get("skill_version"),
        "candidate_generation": skill_manifest.get("candidate_generation"),
        "cli_protocol_min": skill_protocol.get("minimum"),
        "cli_protocol_max_exclusive": skill_protocol.get("maximum_exclusive"),
        "catalog_version": skill_manifest.get("catalog_version"),
    }
    if (
        compatibility != expected_compatibility
        or skill_identity
        != {
            "product_version": payload["product_version"],
            "product_build": payload["product_build"],
            "source_revision": payload["source_revision"],
            "installation_profile": "circle_internal",
        }
        or skill_manifest.get("candidate_generation") != REQUIRED_GENERATION
        or skill_protocol != {"minimum": "1.0.0", "maximum_exclusive": "2.0.0"}
        or skill_manifest.get("catalog_version") != catalog.get("version")
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")

    launcher = payload.get("launcher")
    launch_agent = payload.get("launch_agent")
    if (
        launcher
        != {
            "path": "~/.local/bin/lulu",
            "exact_target": "/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli",
        }
        or type(launch_agent) is not dict
        or frozenset(launch_agent) != {"label", "mach_service", "plist_sha256"}
        or launch_agent.get("label") != "com.lulu.transcribe.agent-core"
        or launch_agent.get("mach_service") != "com.lulu.transcribe.agent-core"
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    by_path = {str(record["path"]): record for record in expected_records}
    launch_path = "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist"
    policy_path = "Contents/Resources/LuluTransportPolicy.v1.plist"
    build_path = "Contents/Resources/build_profile.json"
    if (
        launch_path not in by_path
        or policy_path not in by_path
        or build_path not in by_path
        or launch_agent.get("plist_sha256") != by_path[launch_path].get("sha256")
        or payload.get("transport_policy_sha256") != by_path[policy_path].get("sha256")
        or payload.get("build_profile_sha256") != by_path[build_path].get("sha256")
    ):
        raise _fail("AGENT_ENTRY_COMPONENT_MISMATCH")
    build_profile, _build_bytes = _json_regular(
        app / build_path, reason_code="AGENT_ENTRY_VERSION_MISMATCH"
    )
    if (
        build_profile.get("profile") != "external_candidate"
        or build_profile.get("version") != payload["product_version"]
        or build_profile.get("build") != payload["product_build"]
        or build_profile.get("source_commit") != payload["source_revision"]
    ):
        raise _fail("AGENT_ENTRY_VERSION_MISMATCH")
    if external:
        verify_final_signature(app)
    return {
        "state": "ready",
        "reason_code": None,
        "product_version": payload["product_version"],
        "product_build": payload["product_build"],
        "source_revision": payload["source_revision"],
        "candidate_generation": payload["candidate_generation"],
        "component_manifest_sha256": _sha256_bytes(
            manifest_bytes
        ),
        "app_component_set_digest": payload["app_component_set_digest"],
        "extra_paths": [],
        "effect_count": 0,
    }
