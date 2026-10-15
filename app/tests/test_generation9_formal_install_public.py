from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from src.core.agent_entry.component_manifest import build_component_manifest
from src.core.agent_entry.ownership import (
    verify_ownership_receipt,
    verify_ready_action_receipt,
)


SOURCE_REVISION = "2" * 40


class MemoryKeys:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_text(self, key: str) -> str | None:
        return self.values.get(key)

    def write_new(self, key: str, value: str) -> None:
        if key in self.values:
            raise RuntimeError("existing key")
        self.values[key] = value

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        if key not in self.values and not missing_ok:
            raise RuntimeError("missing key")
        self.values.pop(key, None)


class MemoryService:
    def __init__(self) -> None:
        self.loaded = False
        self.starts = 0
        self.stops = 0

    def is_loaded(self) -> bool:
        return self.loaded

    def start(self, _plist: Path) -> None:
        self.loaded = True
        self.starts += 1

    def stop(self) -> None:
        self.loaded = False
        self.stops += 1


def _fixture_app(
    root: Path,
    *,
    product_build: str = "20260904.1",
    source_revision: str = SOURCE_REVISION,
    skill_text: str = "# Lulu Agent\n",
) -> Path:
    app = root / "Lulu.app"
    resources = app / "Contents/Resources"
    macos = app / "Contents/MacOS"
    launch_agents = app / "Contents/Library/LaunchAgents"
    skill = resources / "skills/lulu-agent"
    for directory in (macos, launch_agents, skill):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("Lulu", "lulu-agent-cli", "lulu-agent-core"):
        path = macos / name
        path.write_bytes(f"fixture:{name}\n".encode())
        path.chmod(0o755)
    (resources / "LuluTransportPolicy.v1.plist").write_bytes(
        plistlib.dumps(
            {
                "Schema": "lulu.agent-xpc-transport/v1",
                "CandidateGeneration": 9,
                "MachService": "com.lulu.transcribe.agent-core",
                "Scope": "current_user_gui_session",
                "NetworkListener": False,
                "SocketFallback": False,
                "PathFallback": False,
            }
        )
    )
    (resources / "build_profile.json").write_text(
        json.dumps(
            {
                "profile": "external_candidate",
                "version": "0.3.2",
                "build": product_build,
                "source_commit": source_revision,
            }
        ),
        encoding="utf-8",
    )
    catalog = resources / "src/agent_cli/capability_catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {"schema_id": "lulu.agent-capability-catalog/v1", "version": "1.0.0"}
        ),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (skill / "manifest.json").write_text(
        json.dumps(
            {
                "schema_id": "lulu.agent-skill-manifest/v1",
                "skill_version": "1.1.0",
                "candidate_generation": 9,
                "product_identity": {
                    "product_version": "0.3.2",
                    "product_build": product_build,
                    "source_revision": source_revision,
                    "installation_profile": "circle_internal",
                },
                "cli_protocol": {"minimum": "1.0.0", "maximum_exclusive": "2.0.0"},
                "catalog_version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    (launch_agents / "com.lulu.transcribe.agent-core.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.lulu.transcribe.agent-core",
                "MachServices": {"com.lulu.transcribe.agent-core": True},
                "ProgramArguments": [
                    "/Applications/Lulu.app/Contents/MacOS/lulu-agent-core"
                ],
            }
        )
    )
    manifest_path = resources / "agent_product_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    manifest = build_component_manifest(
        app_bundle=app,
        manifest_path=manifest_path,
        product_version="0.3.2",
        product_build=product_build,
        source_revision=source_revision,
        candidate_generation=9,
        cli_protocol_min="1.0.0",
        cli_protocol_max="2.0.0",
        catalog_relative_path="Contents/Resources/src/agent_cli/capability_catalog.json",
        capability_closed_set=("doctor", "transcribe", "version"),
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return app


def _verified_dmg_receipt(
    root: Path,
    app: Path,
    *,
    product_build: str = "20260904.1",
) -> tuple[Path, Path]:
    from src.core.agent_entry.component_manifest import verify_component_manifest

    root.mkdir(parents=True, exist_ok=True)
    dmg = root / f"Lulu-0.3.2-{product_build}-arm64.dmg"
    dmg.write_bytes(b"exact-real-dmg-file-bytes\n")
    metadata = dmg.lstat()
    verification = verify_component_manifest(
        app, app / "Contents/Resources/agent_product_manifest.json"
    )
    manifest_raw = (app / "Contents/Resources/agent_product_manifest.json").read_bytes()
    receipt = root / "verified-dmg.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_id": "lulu.generation9-dmg-verification/v1",
                "verified": True,
                "release_dmg_sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
                "release_dmg_size": metadata.st_size,
                "release_dmg_identity": {
                    "path": str(dmg),
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "mode": f"{metadata.st_mode & 0o777:04o}",
                    "owner": metadata.st_uid,
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                },
                "expected_app": str(app),
                "app_component_set_digest": verification["app_component_set_digest"],
                "component_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "product_version": verification["product_version"],
                "product_build": verification["product_build"],
                "source_revision": verification["source_revision"],
                "candidate_generation": 9,
                "mounted_readonly": True,
                "detached": True,
                "contains_secret_material": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    return dmg, receipt


class Generation9FormalInstallPublicTests(unittest.TestCase):
    def _installer(self, root: Path):
        from src.core.agent_entry.formal_install import (
            FormalAgentEntryInstaller,
            FormalInstallPaths,
        )

        app = _fixture_app(root / "candidate")
        dmg, dmg_receipt = _verified_dmg_receipt(root, app)
        paths = FormalInstallPaths(
            app_bundle=app,
            dmg=dmg,
            dmg_receipt=dmg_receipt,
            state_root=root / "home/Library/Application Support/Lulu/AgentEntry",
            launch_agent_target=root / "home/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
            skill_target=root / "home/.agents/skills/lulu-agent",
            launcher_target=root / "home/.local/bin/lulu",
        )
        keys = MemoryKeys()
        service = MemoryService()
        installer = FormalAgentEntryInstaller(
            paths,
            key_store=keys,
            service=service,
            installation_id_factory=lambda: "installation-public-0001",
        )
        return app, dmg, paths, keys, service, installer


if __name__ == "__main__":
    unittest.main()
