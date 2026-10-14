"""Phase 8 shared GUI/Agent CLI product-identity contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest


_COMMIT = "1" * 40
_TREE = "2" * 40
_PRODUCT_SOURCE = "3" * 64


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


class Phase8SharedProductIdentityTests(unittest.TestCase):
    def test_gui_and_console_project_one_sealed_product_identity(self) -> None:
        try:
            from src.core.product_identity import project_product_identity
        except ImportError as exc:
            self.fail(f"Phase 8 shared product identity seam is missing: {exc}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "build_profile.json"
            manifest_path = root / "agent_product_manifest.json"
            _write_json(
                profile_path,
                {
                    "schema": 1,
                    "profile": "trusted_test",
                    "version": "0.3.1",
                    "build": "20260815.3",
                    "source_commit": _COMMIT,
                    "source_tree": _TREE,
                    "product_source_sha256": _PRODUCT_SOURCE,
                    "app_name": "Lulu Trusted Test",
                    "bundle_id": "com.lulu.transcribe.trusted-test",
                    "artifact_prefix": "Lulu-Trusted-Test",
                    "upload_target": "none",
                    "entry": "run_trusted_test.py",
                },
            )
            _write_json(
                manifest_path,
                {
                    "schema_id": "lulu.agent-product-manifest/v1",
                    "schema_revision": 1,
                    "product_family": "Lulu",
                    "product_version": "0.3.1",
                    "product_build": "20260815.3",
                    "cli_protocol_version": "1.0.0",
                    "installation_identity_scope": "shared_lulu_installation",
                    "installation_identity": {
                        "scope": "shared_lulu_installation",
                        "status": "unverified_until_real_install",
                        "agent_specific_copy": "forbidden",
                    },
                    "supported_profiles": ["external_candidate", "trusted_test"],
                    "adapters": {
                        "gui": {
                            "executable": "Contents/MacOS/Lulu-Trusted-Test",
                            "mode": "windowed",
                            "stdio": False,
                        },
                        "agent_cli": {
                            "executable": "Contents/MacOS/lulu-agent-cli",
                            "mode": "console",
                            "stdio": True,
                        },
                    },
                    "shared_runtime": {
                        "analysis": "single",
                        "python": "single",
                        "core": "single",
                        "resources": "single",
                        "storage_layout": "lulu.shared-storage/v1",
                    },
                    "trust_evidence": {
                        "bundle_signature": "unverified",
                        "team_id": "unverified",
                        "acl": "unverified",
                        "stdio": "source_contract_only",
                        "package_size": "planning_only",
                    },
                },
            )

            gui = project_product_identity(
                build_profile_path=profile_path,
                manifest_path=manifest_path,
                adapter_id="gui",
            )
            console = project_product_identity(
                build_profile_path=profile_path,
                manifest_path=manifest_path,
                adapter_id="agent_cli",
            )

        shared_expected = {
            "schema_id": "lulu.product-identity/v1",
            "product_family": "Lulu",
            "product_version": "0.3.1",
            "product_build": "20260815.3",
            "profile": "trusted_test",
            "channel": "trusted-test",
            "display_name": "Lulu Trusted Test",
            "bundle_id": "com.lulu.transcribe.trusted-test",
            "source_commit": _COMMIT,
            "source_tree": _TREE,
            "product_source_sha256": _PRODUCT_SOURCE,
            "cli_protocol_version": "1.0.0",
            "installation_identity_scope": "shared_lulu_installation",
            "installation_identity": {
                "scope": "shared_lulu_installation",
                "status": "unverified_until_real_install",
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
                "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            },
        }
        self.assertEqual(gui["shared"], shared_expected)
        self.assertEqual(console["shared"], shared_expected)
        self.assertEqual(gui["identity_digest"], console["identity_digest"])
        self.assertEqual(gui["adapter"]["mode"], "windowed")
        self.assertFalse(gui["adapter"]["stdio"])
        self.assertEqual(console["adapter"]["mode"], "console")
        self.assertTrue(console["adapter"]["stdio"])


if __name__ == "__main__":
    unittest.main()
