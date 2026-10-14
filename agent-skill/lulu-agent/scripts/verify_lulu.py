#!/usr/bin/env python3
"""Read-only closed component verifier for the one generation-9 Lulu App."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import unicodedata


APP = Path("/Applications/Lulu.app")
MANIFEST = Path("Contents/Resources/agent_product_manifest.json")
CLI = Path("Contents/MacOS/lulu-agent-cli")



class VerificationError(RuntimeError):
    pass


def _read(path: Path) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_size) != (current.st_dev, current.st_ino, current.st_mode, current.st_size):
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
        chunks = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        return b"".join(chunks), current
    finally:
        os.close(descriptor)


def _role(relative: str) -> str:
    fixed = {
        "Contents/MacOS/Lulu": "gui",
        "Contents/MacOS/lulu-agent-cli": "agent_cli",
        "Contents/MacOS/lulu-agent-core": "agent_core",
        "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist": "launch_agent",
        "Contents/Resources/LuluTransportPolicy.v1.plist": "transport_policy",
        "Contents/Resources/build_profile.json": "build_profile",
        "Contents/Resources/src/agent_cli/capability_catalog.json": "catalog",
    }
    return "skill_source" if relative.startswith("Contents/Resources/skills/lulu-agent/") else fixed.get(relative, "runtime_resource")


def _digest(records: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _closed_app_records(
    app: Path, manifest_path: Path
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    components = []
    canonical = []
    manifest_seen = False
    for root, directories, names in os.walk(app, topdown=True, followlinks=False):
        base = Path(root)
        for name in directories:
            path = base / name
            relative = path.relative_to(app).as_posix()
            metadata = path.lstat()
            if (
                unicodedata.normalize("NFC", relative) != relative
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
            canonical.append({
                "path": relative,
                "type": "directory",
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            })
        for name in names:
            path = base / name
            relative = path.relative_to(app).as_posix()
            if unicodedata.normalize("NFC", relative) != relative:
                raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
            if path == manifest_path:
                manifest_seen = True
                continue
            raw, metadata = _read(path)
            common = {
                "path": relative,
                "type": "regular",
                "size": len(raw),
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            canonical.append(common)
            components.append({
                "path": relative,
                "role": _role(relative),
                "type": "regular",
                "size": common["size"],
                "mode": common["mode"],
                "sha256": common["sha256"],
            })
    if not manifest_seen:
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    components.sort(key=lambda value: str(value["path"]))
    canonical.sort(key=lambda value: str(value["path"]).encode("utf-8"))
    return components, canonical


def _tree(root: Path) -> str:
    records = []
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        if path.is_symlink():
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
        if path.is_file():
            raw, metadata = _read(path)
            records.append({"path": path.relative_to(root).as_posix(), "mode": f"{stat.S_IMODE(metadata.st_mode):04o}", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return _digest(records)


def _v2_module():
    import importlib.util
    source=Path(__file__).with_name("verify_delivery_tree_v2.py")
    spec=importlib.util.spec_from_file_location("lulu_standalone_delivery_v2",source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _verify_v2(app,state_root):
    tree=_v2_module();tree.verify_delivery_marker(app)
    def private(path):
        raw,meta=_read(path)
        if stat.S_IMODE(meta.st_mode)!=0o600 or meta.st_uid!=os.getuid():
            raise VerificationError("AGENT_ENTRY_RECEIPT_REQUIRED")
        return tree.strict_json(raw),raw
    barrier=state_root/"product-installation.json"
    if os.path.lexists(barrier):
        status,_raw=private(barrier)
        if status.get("schema_id")!="lulu.product-installation/v2" or status.get("phase") not in {"COMMITTED","ROLLED_BACK"}:
            raise VerificationError("PRODUCT_INSTALLATION_IN_PROGRESS")
    current,_raw=private(state_root/"current.json")
    if (set(current)!={"schema_id","transaction_id","action","phase","state","effect_count","binding"}
        or current.get("schema_id")!="lulu.agent-entry-current-action/v2"
        or current.get("phase")!="COMMITTED" or current.get("state")!="ready"):
        raise VerificationError("AGENT_ENTRY_RECEIPT_REQUIRED")
    manifest,raw=private(state_root/"product-materials-v2"/tree.MANIFEST_NAME)
    receipt,_receipt_raw=private(state_root/"product-materials-v2/verified-dmg.json")
    fields={"schema_id","product_version","product_build","source_revision","candidate_generation", "installation_profile", "cli_protocol", "catalog", "capability_closed_set", "skill_source", "skill_compatibility", "launcher", "launch_agent", "transport_policy_sha256", "build_profile_sha256", "components", "app_component_set_digest", "contains_secret_material"}
    if (set(manifest)!=fields or manifest.get("schema_id")!=tree.MANIFEST_SCHEMA
        or manifest.get("candidate_generation")!=9 or manifest.get("installation_profile")!="circle_internal"
        or manifest.get("contains_secret_material") is not False
        or receipt.get("schema_id")!=tree.DMG_SCHEMA or receipt.get("verified") is not True
        or receipt.get("detached") is not True or receipt.get("mounted_readonly") is not True):
        raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    binding=current.get("binding",{})
    for field in ("app_component_set_digest","component_manifest_sha256","candidate_generation","release_dmg_sha256"):
        if binding.get(field)!=receipt.get(field):raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    if hashlib.sha256(raw).hexdigest()!=binding.get("component_manifest_sha256"):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    return _verify_v2_components(app, manifest, raw, binding, tree)


def _verify_v2_components(app, manifest, raw, binding, tree):
    records=tree.delivery_app_tree_records(app)
    components=[dict(r,role=_role(r["path"])) for r in records if r["type"]!="directory"]
    required={"Contents/MacOS/Lulu","Contents/MacOS/lulu-agent-cli","Contents/MacOS/lulu-agent-core",
        "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
        "Contents/Resources/LuluTransportPolicy.v1.plist","Contents/Resources/build_profile.json",
        "Contents/Resources/src/agent_cli/capability_catalog.json",
        "Contents/Resources/skills/lulu-agent/SKILL.md","Contents/Resources/skills/lulu-agent/manifest.json"}
    by_path={r["path"]:r for r in components}
    if (components!=manifest.get("components") or not required.issubset(by_path)
        or any(by_path[p]["type"]!="regular" for p in required)
        or _digest(records)!=manifest.get("app_component_set_digest")
        or manifest.get("app_component_set_digest")!=binding.get("app_component_set_digest")):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    profile=tree.strict_json(_read(app/"Contents/Resources/build_profile.json")[0])
    if (profile.get("profile")!="external_candidate" or profile.get("version")!=manifest["product_version"]
        or profile.get("build")!=manifest["product_build"] or profile.get("source_commit")!=manifest["source_revision"]):
        raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    if manifest.get("cli_protocol")!={"min":"1.0.0","max":"2.0.0"}:
        raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    tree.verify_final_signature(app)
    return manifest,raw


def _verify_standalone(app, state_root=None):
    """Read-only public projection; signed Core still authenticates HMAC ownership."""
    import plistlib
    import re
    import subprocess

    tree = _v2_module()
    tree.verify_delivery_marker(app)
    observed = {}

    def read(path, *, private=False):
        if path.resolve() != path:
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
        raw, metadata = _read(path)
        if private and (stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid()):
            raise VerificationError("AGENT_ENTRY_RECEIPT_REQUIRED")
        observed[path] = raw
        return raw

    descriptor_raw = read(app/"Contents/Resources/standalone-install.v1.json")
    descriptor = tree.strict_json(descriptor_raw)
    fields = {"schema_id", "product_version", "product_build", "source_revision",
              "candidate_generation", "cli_protocol", "capability_closed_set"}
    local = type(descriptor) is dict and descriptor.get("schema_id") == "lulu.standalone-install-descriptor/v2"
    if (type(descriptor) is not dict or set(descriptor) != (fields | {"storage_scheme"} if local else fields)
            or descriptor["schema_id"] not in {"lulu.standalone-install-descriptor/v1", "lulu.standalone-install-descriptor/v2"}
            or (local and descriptor["storage_scheme"] != "local-integrity-v1")
            or type(descriptor["candidate_generation"]) is not int or descriptor["candidate_generation"] != 9
            or descriptor["cli_protocol"] != {"min": "1.0.0", "max": "2.0.0"}):
        raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    for name, pattern in (("product_version", r"[0-9]+\.[0-9]+\.[0-9]+"),
                          ("product_build", r"[0-9]{8}\.[0-9]+"), ("source_revision", r"[a-f0-9]{40}")):
        if type(descriptor[name]) is not str or re.fullmatch(pattern, descriptor[name]) is None:
            raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    capabilities = descriptor["capability_closed_set"]
    if (type(capabilities) is not list or not 1 <= len(capabilities) <= 1000
            or any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) is None for value in capabilities)
            or capabilities != sorted(set(capabilities))):
        raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
    token = hashlib.sha256(descriptor_raw + b"\0" + os.fsencode(app)).hexdigest()
    namespace = "local-app-generations" if local else "app-generations"
    root = (Path(state_root) if state_root is not None else
            Path.home()/"Library/Application Support/Lulu/AgentEntry"/namespace/token)
    if root.name != token or root.resolve() != root:
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    owner = tree.strict_json(read(root/"ownership.json", private=True))
    owner_fields = {"schema_id", "source_kind", "source_app_sha256", "descriptor_sha256",
        "component_manifest_sha256", "app_component_set_digest", "installation_id",
        "installation_id_digest", "candidate_generation", "app_path", "core_cdhash", "hmac_sha256"}
    if (type(owner) is not dict or set(owner) != owner_fields
            or owner["schema_id"] != "lulu.agent-entry-ownership/v4"
            or owner["source_kind"] != "signed_app" or type(owner["candidate_generation"]) is not int
            or owner["candidate_generation"] != 9 or owner["app_path"] != str(app)
            or owner["source_app_sha256"] != owner["app_component_set_digest"]
            or owner["descriptor_sha256"] != hashlib.sha256(descriptor_raw).hexdigest()):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    for name in ("source_app_sha256", "descriptor_sha256", "component_manifest_sha256",
                 "app_component_set_digest", "installation_id_digest", "hmac_sha256", "core_cdhash"):
        width = 40 if name == "core_cdhash" else 64
        if type(owner[name]) is not str or re.fullmatch(r"[a-f0-9]{%d}" % width, owner[name]) is None:
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    if (type(owner["installation_id"]) is not str or len(owner["installation_id"]) < 8
            or hashlib.sha256(owner["installation_id"].encode()).hexdigest() != owner["installation_id_digest"]):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    binding = {name: owner[name] for name in ("source_kind", "source_app_sha256", "component_manifest_sha256",
        "app_component_set_digest", "installation_id_digest", "candidate_generation")}
    current = tree.strict_json(read(root/"current.json", private=True))
    if current != {"schema_id": "lulu.agent-entry-current-action/v3", "phase": "COMMITTED",
                   "state": "ready", "binding": binding}:
        raise VerificationError("AGENT_ENTRY_RECEIPT_REQUIRED")
    source = tree.strict_json(read(root/"product-materials-v2/verified-app.json", private=True))
    expected = {"schema_id": "lulu.verified-app-install-source/v1", "source_kind": "signed_app",
        **{name: owner[name] for name in ("descriptor_sha256", "component_manifest_sha256",
               "app_component_set_digest", "candidate_generation", "core_cdhash")}}
    if source != expected:
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    raw = read(root/"product-materials-v2"/tree.MANIFEST_NAME, private=True)
    manifest = tree.strict_json(raw)
    manifest_fields = {"schema_id", "product_version", "product_build", "source_revision", "candidate_generation",
        "installation_profile", "cli_protocol", "catalog", "capability_closed_set", "skill_source",
        "skill_compatibility", "launcher", "launch_agent", "transport_policy_sha256", "build_profile_sha256",
        "components", "app_component_set_digest", "contains_secret_material"}
    if (type(manifest) is not dict or set(manifest) != manifest_fields
            or manifest["schema_id"] != tree.MANIFEST_SCHEMA or manifest["installation_profile"] != "circle_internal"
            or manifest["contains_secret_material"] is not False
            or any(manifest[name] != descriptor[name] for name in fields - {"schema_id"})
            or hashlib.sha256(raw).hexdigest() != owner["component_manifest_sha256"]):
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    label = 'com.lulu.transcribe.agent-core.app-' + token
    registration = plistlib.loads(read(root/"core.plist", private=True))
    if registration != {"Label": label, "ProgramArguments": [str(app/"Contents/MacOS/lulu-agent-core")],
                        "MachServices": {label: True}, "ProcessType": "Interactive"}:
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    _verify_v2_components(app, manifest, raw, binding, tree)
    signature = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(app/'Contents/MacOS/lulu-agent-core')],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
    matches = re.findall(rb'^CDHash=([a-f0-9]{40})$', signature.stdout + signature.stderr, re.MULTILINE)
    if signature.returncode != 0 or matches != [owner['core_cdhash'].encode()]:
        raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    # A concurrent preparation/replacement cannot mix two sets of receipts.
    for path, initial in observed.items():
        if path.resolve() != path or _read(path)[0] != initial:
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    return manifest, raw


def verify(app: Path = APP, *, state_root: Path | None = None) -> dict[str, object]:
    if app != APP or app.is_symlink():
        raise VerificationError("APP_PATH_NOT_ALLOWED")
    v2 = os.path.lexists(app/"Contents/Resources/component_delivery.v2.json") or os.path.lexists(app/"Contents/Resources/agent_links.v2.json")
    standalone = os.path.lexists(app/"Contents/Resources/standalone-install.v1.json")
    if standalone:
        manifest,raw_manifest = _verify_standalone(app, state_root)
    elif v2:
        manifest,raw_manifest=_verify_v2(app,Path(state_root) if state_root is not None else Path.home()/"Library/Application Support/Lulu/AgentEntry")
    else:
        manifest_path = app / MANIFEST
        raw_manifest, _ = _read(manifest_path)
        try:
            manifest = json.loads(raw_manifest)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH") from exc
        if type(manifest) is not dict or manifest.get("schema_id") != "lulu.agent-component-manifest/v1" or manifest.get("candidate_generation") != 9 or manifest.get("installation_profile") != "circle_internal" or manifest.get("contains_secret_material") is not False:
            raise VerificationError("AGENT_ENTRY_VERSION_MISMATCH")
        records, canonical_records = _closed_app_records(app, manifest_path)
        if (
            records != manifest.get("components")
            or _digest(canonical_records) != manifest.get("app_component_set_digest")
        ):
            raise VerificationError("AGENT_ENTRY_COMPONENT_MISMATCH")
    # The optional public Skill and shortcut are not product installation materials.
    # The signed bundled resources were verified above; Core verifies its own receipt.
    return {"schema_id": "lulu.install-verification/v2", "verified": True, "canonical_app": str(APP), "canonical_cli": str(APP / CLI), "component_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(), "app_component_set_digest": manifest["app_component_set_digest"], "candidate_generation": 9, "product_version": manifest["product_version"], "product_build": manifest["product_build"], "cli_protocol_version": manifest["cli_protocol"]["min"]}


def main(argv: list[str]) -> int:
    try:
        if argv:
            raise VerificationError("INVALID_ARGUMENT")
        result, code = verify(), 0
    except (OSError, RuntimeError, ValueError) as exc:
        result, code = {"schema_id": "lulu.install-verification/v2", "verified": False, "reason_code": str(exc) or "AGENT_ENTRY_COMPONENT_MISMATCH"}, 60
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
