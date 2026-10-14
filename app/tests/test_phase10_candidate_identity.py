"""20260823.4 trusted-test GUI-only diagnostic identity tests."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import runpy
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]


class Phase10CandidateIdentityTests(unittest.TestCase):
    def test_version_and_profile_identity_are_exact_gui_only_v1(self) -> None:
        from src.version import __build__, __channel__, __version__, channel_for_profile

        gate = runpy.run_path(str(APP_ROOT / "packaging/profile_gate.py"))
        profile = gate["profile_payload"](
            "trusted_test",
            version=__version__,
            build=__build__,
            source_commit="1" * 40,
            source_tree="2" * 40,
            product_source_sha256="3" * 64,
        )
        self.assertEqual((__version__, __build__, __channel__), ("0.3.2", "20260906.3", "candidate"))
        self.assertEqual(channel_for_profile("trusted_test"), "trusted-test")
        self.assertEqual(profile["package_surface"], "gui_only/v1")
        self.assertEqual(profile["app_name"], "Lulu Trusted Test")
        self.assertEqual(profile["bundle_id"], "com.lulu.transcribe.trusted-test")
        self.assertEqual(profile["entry"], "run_trusted_test.py")
        manifest = json.loads(
            (APP_ROOT / "packaging/agent_product_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["product_build"], "20260823.4")
        self.assertEqual(manifest["candidate_identity"]["build"], "20260823.4")

    def test_gui_only_candidate_schema_has_no_cli_authority_fields(self) -> None:
        finalizer = runpy.run_path(str(APP_ROOT / "packaging/phase10_finalize_candidate.py"))
        self.assertEqual(finalizer["_BUILD"], "20260823.4")
        self.assertEqual(finalizer["_TRUSTED_PACKAGE_SURFACE"], "gui_only/v1")
        fields = finalizer["_GUI_ONLY_EVIDENCE_FIELDS"]
        diagnostic = {
            "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
            "diagnostic_only": True,
            "promotion_allowed": False,
            "retained_install_allowed": False,
        }
        self.assertEqual(finalizer["_DIAGNOSTIC_IDENTITY"], diagnostic)
        self.assertTrue(set(diagnostic).issubset(fields))
        for forbidden in (
            "adapters", "agent_cli", "canonical_cli_sha256", "cli_cdhash",
            "product_manifest_sha256", "installation_identity_scope",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertIn("gui_executable_sha256", fields)
        self.assertIn("forbidden_surface_absent", fields)

    def test_finalizer_has_no_caller_authority_or_cli_fallback_parameter(self) -> None:
        path = APP_ROOT / "packaging/phase10_finalize_candidate.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        finalize = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "finalize_candidate"
        )
        names = [argument.arg for argument in (*finalize.args.args, *finalize.args.kwonlyargs)]
        self.assertEqual(names, ["bundle", "bundle_role", "output_path", "codesign_runner"])
        for forbidden in ("authority", "manifest", "agent_cli", "canonical_cli", "ticket", "keychain"):
            self.assertNotIn(forbidden, names)

    def test_agent_manifest_skill_and_cli_sources_remain_read_only_but_outside_trusted_spec(self) -> None:
        spec = (APP_ROOT / "packaging/Lulu.spec").read_text(encoding="utf-8")
        self.assertIn('AGENT_CLI_ENABLED = BUILD_PROFILE == "external_candidate"', spec)
        self.assertIn("if AGENT_CLI_ENABLED:", spec)
        self.assertIn("AGENT_PRODUCT_MANIFEST", spec)
        self.assertIn("AGENT_SKILL_SOURCE", spec)
        self.assertIn('if BUILD_PROFILE == "external_candidate":\n    RUNTIME_HOOKS', spec)


if __name__ == "__main__":
    unittest.main()
