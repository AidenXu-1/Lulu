from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import plistlib
import tempfile
import unittest

from src.core.agent_entry.canonical_app_tree import canonical_app_tree_identity
from src.core.agent_entry.component_manifest import (
    ComponentManifestError,
    build_component_manifest,
    verify_component_manifest,
)


SOURCE_REVISION = "7b15ff66cd214786293d62ffde6ba80276a5d891"

_IDENTITY_VERIFIER_PATH = (
    Path(__file__).resolve().parents[1]
    / "packaging"
    / "verify_generation9_candidate_identity.py"
)


def _load_identity_verifier():
    spec = importlib.util.spec_from_file_location(
        "lulu_verify_generation9_candidate_identity", _IDENTITY_VERIFIER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("identity verifier module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Generation9ComponentManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "Lulu.app"
        resources = self.app / "Contents" / "Resources"
        macos = self.app / "Contents" / "MacOS"
        launch_agents = self.app / "Contents" / "Library" / "LaunchAgents"
        skill = resources / "skills" / "lulu-agent"
        for directory in (macos, launch_agents, skill):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("Lulu", "lulu-agent-cli", "lulu-agent-core"):
            path = macos / name
            path.write_bytes(f"fixture:{name}\n".encode())
            path.chmod(0o755)
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
            }), encoding="utf-8"
        )
        catalog_path = resources / "src/agent_cli/capability_catalog.json"
        catalog_path.parent.mkdir(parents=True)
        catalog_path.write_text(
            json.dumps({"schema_id": "lulu.agent-capability-catalog/v1", "version": "1.0.0"}),
            encoding="utf-8",
        )
        (skill / "SKILL.md").write_text("# Lulu Agent\n", encoding="utf-8")
        (skill / "manifest.json").write_text(
            json.dumps({
                "schema_id": "lulu.agent-skill-manifest/v1",
                "skill_version": "1.1.0",
                "candidate_generation": 9,
                "product_identity": {
                    "product_version": "0.3.2",
                    "product_build": "20260823.4",
                    "source_revision": SOURCE_REVISION,
                    "installation_profile": "circle_internal",
                },
                "cli_protocol": {"minimum": "1.0.0", "maximum_exclusive": "2.0.0"},
                "catalog_version": "1.0.0",
            }), encoding="utf-8"
        )
        (launch_agents / "com.lulu.transcribe.agent-core.plist").write_bytes(
            plistlib.dumps({
                "Label": "com.lulu.transcribe.agent-core",
                "MachServices": {"com.lulu.transcribe.agent-core": True},
                "ProgramArguments": ["/Applications/Lulu.app/Contents/MacOS/lulu-agent-core"],
            })
        )
        self.manifest_path = resources / "agent_product_manifest.json"
        self.manifest_path.write_text("{}\n", encoding="utf-8")
        self.manifest_path.chmod(0o644)
        self.manifest = build_component_manifest(
            app_bundle=self.app,
            manifest_path=self.manifest_path,
            product_version="0.3.2",
            product_build="20260823.4",
            source_revision=SOURCE_REVISION,
            candidate_generation=9,
            cli_protocol_min="1.0.0",
            cli_protocol_max="2.0.0",
            catalog_relative_path="Contents/Resources/src/agent_cli/capability_catalog.json",
            capability_closed_set=("doctor", "jobs", "transcribe", "version"),
        )
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assertReason(self, reason: str) -> None:
        with self.assertRaises(ComponentManifestError) as caught:
            verify_component_manifest(self.app, self.manifest_path)
        self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(caught.exception.effect_count, 0)

    def test_exact_closed_component_set_is_ready(self) -> None:
        report = verify_component_manifest(self.app, self.manifest_path)
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["candidate_generation"], 9)
        self.assertEqual(report["source_revision"], SOURCE_REVISION)
        self.assertEqual(report["effect_count"], 0)
        self.assertEqual(report["extra_paths"], [])
        self.assertEqual(
            report["app_component_set_digest"],
            canonical_app_tree_identity(self.app.resolve())["sha256"],
        )

    def test_candidate_identity_verifier_requires_zero_drift_across_consumers(self) -> None:
        verifier = _load_identity_verifier()
        identity = canonical_app_tree_identity(self.app.resolve())
        profile_receipt = self.root / "build-profile-receipt.json"
        profile_receipt.write_text(
            json.dumps({
                "digests": {"app_tree_sha256": identity["sha256"]},
                "app_tree": identity,
                "app_tree_binding": {
                    "stage": "post_materialization",
                    "materialization_receipt_sha256": "a" * 64,
                    "materialized_count": 1,
                    "first_recomputation": identity["sha256"],
                    "second_recomputation": identity["sha256"],
                },
            }),
            encoding="utf-8",
        )
        output = self.root / "canonical-app-tree-receipt.json"

        receipt = verifier.verify_candidate_identity(
            app=self.app,
            profile_receipt_path=profile_receipt,
        )
        verifier.write_receipt(output, receipt)

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["first_recomputation"], identity)
        self.assertEqual(receipt["second_recomputation"], identity)
        self.assertEqual(
            set(receipt["consumer_bindings"].values()), {identity["sha256"]}
        )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), receipt)

        tampered = json.loads(profile_receipt.read_text(encoding="utf-8"))
        binding = tampered.pop("app_tree_binding")
        profile_receipt.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(verifier.CandidateIdentityError):
            verifier.verify_candidate_identity(
                app=self.app,
                profile_receipt_path=profile_receipt,
            )
        tampered["app_tree_binding"] = binding
        tampered["digests"]["app_tree_sha256"] = "0" * 64
        profile_receipt.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(verifier.CandidateIdentityError):
            verifier.verify_candidate_identity(
                app=self.app,
                profile_receipt_path=profile_receipt,
            )

    def test_missing_extra_byte_mode_and_symlink_fail_before_effect(self) -> None:
        targets = {
            "missing": self.app / "Contents/MacOS/lulu-agent-core",
            "one_byte": self.app / "Contents/MacOS/lulu-agent-cli",
            "mode": self.app / "Contents/MacOS/Lulu",
        }
        targets["missing"].unlink()
        self.assertReason("AGENT_ENTRY_COMPONENT_MISMATCH")
        targets["missing"].write_bytes(b"fixture:lulu-agent-core\n")
        targets["missing"].chmod(0o755)

        with targets["one_byte"].open("ab") as handle:
            handle.write(b"x")
        self.assertReason("AGENT_ENTRY_COMPONENT_MISMATCH")
        targets["one_byte"].write_bytes(b"fixture:lulu-agent-cli\n")
        targets["one_byte"].chmod(0o755)

        targets["mode"].chmod(0o700)
        self.assertReason("AGENT_ENTRY_COMPONENT_MISMATCH")
        targets["mode"].chmod(0o755)

        extra = self.app / "Contents/MacOS/old-core"
        extra.write_bytes(b"old")
        extra.chmod(0o755)
        self.assertReason("AGENT_ENTRY_COMPONENT_MISMATCH")
        extra.unlink()

        original = self.app / "Contents/MacOS/lulu-agent-core"
        original.unlink()
        os.symlink("lulu-agent-cli", original)
        self.assertReason("AGENT_ENTRY_COMPONENT_MISMATCH")

    def test_manifest_or_catalog_generation_drift_fails_closed(self) -> None:
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["candidate_generation"] = 8
        self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertReason("AGENT_ENTRY_VERSION_MISMATCH")

        payload["candidate_generation"] = 9
        self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        catalog = self.app / "Contents/Resources/src/agent_cli/capability_catalog.json"
        catalog.write_text(
            json.dumps({"schema_id": "lulu.agent-capability-catalog/v1", "version": "0.9.0"}),
            encoding="utf-8",
        )
        self.assertReason("AGENT_ENTRY_VERSION_MISMATCH")


if __name__ == "__main__":
    unittest.main()
