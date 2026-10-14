"""Phase 9 Skill/CLI compatibility must fail closed before proposal work."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
import runpy
import unittest


_SIGNED_MANIFEST_SHA256 = "4" * 64


def _claim(protocol: str = "1.0.0") -> dict[str, object]:
    return {
        "schema_id": "lulu.agent/v1",
        "schema_revision": 1,
        "cli_protocol_version": protocol,
        "catalog_version": "1.0.0",
        "product_version": "0.3.1",
        "product_build": "20260815.3",
        "installation_identity_scope": "shared_lulu_installation",
        "signed_manifest_sha256": _SIGNED_MANIFEST_SHA256,
    }


def _verified_installation() -> dict[str, object]:
    return {
        "schema_id": "lulu.install-verification/v1",
        "verified": True,
        "product_version": "0.3.1",
        "product_build": "20260815.3",
        "installation_identity_scope": "shared_lulu_installation",
        "signed_manifest_sha256": _SIGNED_MANIFEST_SHA256,
    }


class Phase9ProtocolCompatibilityTests(unittest.TestCase):
    def test_semver_schema_unknown_fields_and_install_identity_fail_closed(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        contract_source = app_root / "src" / "agent_cli" / "skill_contract.py"
        self.assertTrue(
            contract_source.is_file(),
            "Phase 9 strict Skill compatibility contract is missing",
        )
        namespace = runpy.run_path(str(contract_source))
        validate = namespace["validate_skill_compatibility"]
        SkillContractError = namespace["SkillContractError"]
        manifest = json.loads(
            (app_root / "skills" / "lulu-agent" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            validate(
                skill_manifest=manifest,
                cli_claim=_claim("1.0.0+frozen"),
                verified_installation=_verified_installation(),
            ),
            {
                "schema_id": "lulu.skill-compatibility/v1",
                "compatible": True,
                "skill_version": "1.0.0",
                "cli_protocol_version": "1.0.0+frozen",
                "catalog_version": "1.0.0",
                "cli_schema_id": "lulu.agent/v1",
                "cli_schema_revision": 1,
                "installation_identity_scope": "shared_lulu_installation",
            },
        )

        hostile_cases: list[tuple[dict[str, object], dict[str, object], dict[str, object], str]] = []
        unknown_manifest = dict(manifest)
        unknown_manifest["future_control"] = True
        hostile_cases.append((unknown_manifest, _claim(), _verified_installation(), "UNKNOWN_FIELD"))

        prerelease = _claim("1.1.0-rc.1")
        hostile_cases.append((manifest, prerelease, _verified_installation(), "PRERELEASE_NOT_PINNED"))

        wrong_schema = _claim()
        wrong_schema["schema_revision"] = 2
        hostile_cases.append((manifest, wrong_schema, _verified_installation(), "SCHEMA_INCOMPATIBLE"))

        unknown_claim = _claim()
        unknown_claim["trusted"] = True
        hostile_cases.append((manifest, unknown_claim, _verified_installation(), "UNKNOWN_FIELD"))

        wrong_catalog = _claim()
        wrong_catalog["catalog_version"] = "1.0.1"
        hostile_cases.append((manifest, wrong_catalog, _verified_installation(), "CATALOG_INCOMPATIBLE"))

        wrong_install = _verified_installation()
        wrong_install["signed_manifest_sha256"] = "5" * 64
        hostile_cases.append((manifest, _claim(), wrong_install, "INSTALL_IDENTITY_MISMATCH"))

        for skill_manifest, cli_claim, installation, expected_code in hostile_cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(SkillContractError) as raised:
                    validate(
                        skill_manifest=skill_manifest,
                        cli_claim=cli_claim,
                        verified_installation=installation,
                    )
                self.assertEqual(raised.exception.code, expected_code)

        pinned = json.loads(json.dumps(manifest))
        pinned["cli_protocol"]["exact_prerelease_pin"] = "1.1.0-rc.1"
        self.assertTrue(
            validate(
                skill_manifest=pinned,
                cli_claim=_claim("1.1.0-rc.1"),
                verified_installation=_verified_installation(),
            )["compatible"]
        )

        from src.agent_cli.main import main

        contract = {
            "skill_manifest": manifest,
            "cli_claim": _claim(),
            "verified_installation": _verified_installation(),
        }
        stdout = StringIO()
        stderr = StringIO()
        exit_code = main(
            [
                "skill",
                "compatibility",
                "check",
                "--contract-json",
                json.dumps(contract, separators=(",", ":")),
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=stderr,
            product_identity={"adapter_id": "agent_cli"},
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        envelope = json.loads(stdout.getvalue())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "skill compatibility check")
        self.assertEqual(envelope["result"]["compatible"], True)


if __name__ == "__main__":
    unittest.main()
