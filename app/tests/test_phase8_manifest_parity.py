"""Phase 8 product-manifest, source identity, and availability parity tests."""

from __future__ import annotations

from collections import Counter
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from src.agent_cli.catalog import load_capability_catalog
from src.version import __build__, __version__


class Phase8ManifestParityTests(unittest.TestCase):
    def test_manifest_freezes_two_adapters_shared_assets_and_unverified_gates(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        manifest_path = app_root / "packaging" / "agent_product_manifest.json"
        self.assertTrue(manifest_path.is_file(), "Phase 8 product manifest is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_id"], "lulu.agent-product-manifest/v1")
        self.assertEqual(manifest["schema_revision"], 1)
        self.assertEqual(manifest["product_family"], "Lulu")
        self.assertEqual(manifest["product_version"], __version__)
        self.assertEqual(manifest["product_build"], __build__)
        self.assertEqual(manifest["cli_protocol_version"], "1.0.0")
        self.assertEqual(
            manifest["source_identity"],
            {
                "authority": "sealed_build_profile",
                "required_fields": [
                    "source_commit",
                    "source_tree",
                    "product_source_sha256",
                ],
            },
        )
        self.assertEqual(
            manifest["adapters"],
            {
                "gui": {
                    "executable": "Contents/MacOS/{artifact_prefix}",
                    "mode": "windowed",
                    "stdio": False,
                },
                "agent_cli": {
                    "executable": "Contents/MacOS/lulu-agent-cli",
                    "mode": "console",
                    "stdio": True,
                },
            },
        )
        self.assertEqual(
            manifest["shared_runtime"],
            {
                "analysis": "single",
                "python": "single",
                "core": "single",
                "resources": "single",
                "storage_layout": "lulu.shared-storage/v1",
            },
        )
        self.assertEqual(
            manifest["installation_identity"],
            {
                "scope": "shared_lulu_installation",
                "status": "unverified_until_real_install",
                "agent_specific_copy": "forbidden",
            },
        )
        self.assertEqual(
            manifest["skill_compatibility"],
            {
                "skill_version": "1.0.0",
                "accepted_cli_protocol": ">=1.0.0,<2.0.0",
                "catalog_version": "1.0.0",
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "installation": "not_performed_by_phase8_source",
            },
        )
        self.assertEqual(manifest["size_baselines"]["evidence_status"], "planning_only")
        self.assertEqual(
            manifest["size_baselines"]["values"],
            {
                "base_product_package": "approximately 1.1 GB",
                "base_plus_local_transcription": "approximately 3.5 GB total",
                "skill_payload": "less than 1 MB",
                "optional_local_voice_models": "approximately +12 GB",
            },
        )
        self.assertEqual(
            manifest["trust_evidence"],
            {
                "bundle_signature": "unverified",
                "designated_requirement": "unverified",
                "team_id": "unverified",
                "acl": "unverified",
                "stdio": "source_contract_only",
                "package_size": "planning_only",
                "real_install": "unverified",
            },
        )

        catalog = load_capability_catalog()["catalog"]["capabilities"]
        counts = Counter(item["current_cli_state"] for item in catalog)
        self.assertEqual(counts, Counter({"unavailable": 135, "gui_only": 8}))
        self.assertEqual(
            manifest["capability_projection"],
            {
                "unavailable": 135,
                "gui_only": 8,
                "available": 0,
                "degraded": 0,
                "protected_state": "unavailable",
                "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            },
        )
        spec = (app_root / "packaging" / "Lulu.spec").read_text(encoding="utf-8")
        self.assertIn('AGENT_PRODUCT_MANIFEST = os.path.join(SPECPATH, "agent_product_manifest.json")', spec)
        self.assertIn("datas += [(AGENT_PRODUCT_MANIFEST, \".\")]", spec)

    def test_product_version_status_and_doctor_share_the_same_manifest_identity(self) -> None:
        from src.agent_cli.product_entry import main

        app_root = Path(__file__).resolve().parents[1]
        manifest_source = app_root / "packaging" / "agent_product_manifest.json"
        self.assertTrue(
            manifest_source.is_file(),
            "Phase 8 product manifest is missing from the console identity path",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = {
                "schema": 1,
                "profile": "external_candidate",
                "version": __version__,
                "build": __build__,
                "source_commit": "1" * 40,
                "source_tree": "2" * 40,
                "product_source_sha256": "3" * 64,
                "app_name": "Lulu",
                "bundle_id": "com.lulu.transcribe",
                "artifact_prefix": "Lulu",
                "upload_target": "github-release-candidate",
                "entry": "run_external.py",
            }
            (root / "build_profile.json").write_text(
                json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (root / "agent_product_manifest.json").write_bytes(
                manifest_source.read_bytes()
            )
            os.chmod(root / "build_profile.json", 0o600)
            os.chmod(root / "agent_product_manifest.json", 0o600)
            projections = []
            for argv in (
                ["version", "--output", "json"],
                ["status", "--output", "json"],
                ["doctor", "--output", "json"],
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    argv,
                    resource_root=root,
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr.getvalue(), "")
                payload = json.loads(stdout.getvalue())
                projections.append(payload["result"]["product_identity"])
                self.assertEqual(payload["result"]["formal_cli_state"], "unavailable")

        self.assertEqual(projections[0], projections[1])
        self.assertEqual(projections[1], projections[2])
        self.assertEqual(projections[0]["adapter_id"], "agent_cli")
        self.assertEqual(
            projections[0]["shared"].get("installation_identity"),
            {
                "scope": "shared_lulu_installation",
                "status": "unverified_until_real_install",
                "agent_specific_copy": "forbidden",
            },
        )
        self.assertEqual(
            projections[0]["shared"]["protected_capabilities"]["state"],
            "unavailable",
        )


if __name__ == "__main__":
    unittest.main()
