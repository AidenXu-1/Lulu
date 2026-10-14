from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest

from src.core.agent_entry.component_manifest import build_component_manifest


SOURCE_REVISION = "7b15ff66cd214786293d62ffde6ba80276a5d891"
APP_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = APP_ROOT / "skills/lulu-agent"
VERIFIER_SOURCE = SKILL_SOURCE / "scripts/verify_lulu_generation9.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "lulu_generation9_skill_verifier", VERIFIER_SOURCE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("skill verifier module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Generation9SkillVerifierTests(unittest.TestCase):
    def test_packaged_verifier_and_formal_installer_share_exact_launcher_bytes(self) -> None:
        from src.core.agent_entry.formal_install import LAUNCHER_BYTES

        verifier = _load_verifier()
        self.assertEqual(verifier.LAUNCHER_BYTES, LAUNCHER_BYTES)

    def test_finalized_app_uses_canonical_tree_digest(self) -> None:
        verifier = _load_verifier()
        with tempfile.TemporaryDirectory(prefix="lulu generation9 skill verifier ") as directory:
            root = Path(directory)
            app = root / "Lulu.app"
            resources = app / "Contents/Resources"
            macos = app / "Contents/MacOS"
            launch_agents = app / "Contents/Library/LaunchAgents"
            bundled_skill = resources / "skills/lulu-agent"
            for target in (macos, launch_agents, resources):
                target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(SKILL_SOURCE, bundled_skill)
            skill_manifest_path = bundled_skill / "manifest.json"
            skill_manifest = json.loads(
                skill_manifest_path.read_text(encoding="utf-8")
            )
            skill_manifest["product_identity"]["source_revision"] = SOURCE_REVISION
            skill_manifest_path.write_text(
                json.dumps(skill_manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            for name in ("Lulu", "lulu-agent-cli", "lulu-agent-core"):
                executable = macos / name
                executable.write_bytes(f"fixture:{name}\n".encode())
                executable.chmod(0o755)
            (resources / "LuluTransportPolicy.v1.plist").write_bytes(
                plistlib.dumps({
                    "Schema": "lulu.agent-xpc-transport/v1",
                    "CandidateGeneration": 9,
                    "MachService": "com.lulu.transcribe.agent-core",
                    "Scope": "current_user_gui_session",
                    "NetworkListener": False,
                    "SocketFallback": False,
                    "PathFallback": False,
                })
            )
            (resources / "build_profile.json").write_text(
                json.dumps({
                    "profile": "external_candidate",
                    "version": "0.3.2",
                    "build": "20260823.4",
                    "source_commit": SOURCE_REVISION,
                }),
                encoding="utf-8",
            )
            catalog = resources / "src/agent_cli/capability_catalog.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                json.dumps({
                    "schema_id": "lulu.agent-capability-catalog/v1",
                    "version": "1.0.0",
                }),
                encoding="utf-8",
            )
            (launch_agents / "com.lulu.transcribe.agent-core.plist").write_bytes(
                plistlib.dumps({
                    "Label": "com.lulu.transcribe.agent-core",
                    "MachServices": {"com.lulu.transcribe.agent-core": True},
                    "ProgramArguments": [
                        "/Applications/Lulu.app/Contents/MacOS/lulu-agent-core"
                    ],
                })
            )
            manifest_path = resources / "agent_product_manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            manifest = build_component_manifest(
                app_bundle=app,
                manifest_path=manifest_path,
                product_version="0.3.2",
                product_build="20260823.4",
                source_revision=SOURCE_REVISION,
                candidate_generation=9,
                cli_protocol_min="1.0.0",
                cli_protocol_max="2.0.0",
                catalog_relative_path=(
                    "Contents/Resources/src/agent_cli/capability_catalog.json"
                ),
                capability_closed_set=("doctor", "jobs", "transcribe", "version"),
            )
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            launcher = root / "lulu"
            launcher.write_bytes(verifier.LAUNCHER_BYTES)
            launcher.chmod(0o700)
            installed_skill = root / "installed-skill"
            shutil.copytree(bundled_skill, installed_skill)

            original_app = verifier.APP
            verifier.APP = app
            try:
                result = verifier.verify(
                    app,
                    launcher=launcher,
                    installed_skill=installed_skill,
                )
                self.assertTrue(result["verified"])
                self.assertEqual(
                    result["app_component_set_digest"],
                    manifest["app_component_set_digest"],
                )
                self.assertEqual(os.lstat(launcher).st_uid, os.getuid())

                protected_directory = resources / "src"
                protected_directory.chmod(0o700)
                with self.assertRaisesRegex(
                    verifier.VerificationError,
                    "AGENT_ENTRY_COMPONENT_MISMATCH",
                ):
                    verifier.verify(
                        app,
                        launcher=launcher,
                        installed_skill=installed_skill,
                    )
            finally:
                verifier.APP = original_app


if __name__ == "__main__":
    unittest.main()
