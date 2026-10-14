from __future__ import annotations

import ast
from pathlib import Path
import runpy
import subprocess
import sys
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]


class TrustedTestSafeE2EPackagingContractTests(unittest.TestCase):
    def _run_packaging_entry_without_arguments(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(APP_ROOT / "packaging" / name)],
            cwd=APP_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_prepare_file_path_entry_reaches_argument_validation(self) -> None:
        result = self._run_packaging_entry_without_arguments(
            "prepare_trusted_test_safe_e2e.py"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--candidate-app", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_cleanup_file_path_entry_reaches_argument_validation(self) -> None:
        result = self._run_packaging_entry_without_arguments(
            "cleanup_trusted_test_safe_e2e.py"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--authority", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_trusted_test_profile_has_safe_e2e_entry_and_release_does_not(self) -> None:
        spec_path = APP_ROOT / "packaging" / "Lulu.spec"
        tree = ast.parse(spec_path.read_text(encoding="utf-8"), filename=str(spec_path))
        gate = runpy.run_path(str(APP_ROOT / "packaging" / "profile_gate.py"))

        profiles = gate["PROFILES"]
        contracts = gate["APPROVED_SECONDARY_ENTRY_CONTRACTS"]
        self.assertEqual(
            profiles["trusted_test_safe_e2e"],
            {
                "app_name": "Lulu Trusted Test Safe E2E",
                "bundle_id": "com.lulu.transcribe.trusted-test.safe-e2e",
                "artifact_prefix": "Lulu-Trusted-Test-Safe-E2E",
                "upload_target": "none",
                "entry": "run_trusted_test_safe_e2e.py",
                "package_surface": "isolated_local_transcription_e2e/v1",
                "required_modules": (
                    "src.trusted_test_safe_e2e",
                    "src.core.pipeline",
                    "src.core.transcription_job_application",
                ),
            },
        )
        self.assertEqual(profiles["trusted_test"]["package_surface"], "gui_only/v1")
        self.assertNotIn("trusted_test", contracts)
        self.assertNotIn("trusted_test_safe_e2e", contracts)
        self.assertEqual(
            contracts["external_candidate"]["relative_path"],
            "Contents/MacOS/lulu-agent-cli",
        )

        assignments = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("TRUSTED_TEST_SAFE_E2E_ENABLED", assignments)
        enabled = assignments["TRUSTED_TEST_SAFE_E2E_ENABLED"]
        self.assertIsInstance(enabled, ast.Compare)
        self.assertEqual(ast.unparse(enabled), "BUILD_PROFILE == 'trusted_test_safe_e2e'")

        agent_enabled = assignments["AGENT_CLI_ENABLED"]
        self.assertEqual(ast.unparse(agent_enabled), "BUILD_PROFILE == 'external_candidate'")

        release_script = (APP_ROOT / "packaging/build_release.sh").read_text(encoding="utf-8")
        self.assertIn('$1" != "external_candidate"', release_script)
        self.assertNotIn("trusted-test-safe-e2e", release_script)

    def test_safe_profile_rejects_formal_surfaces_and_external_rejects_safe_modules(self) -> None:
        gate = runpy.run_path(str(APP_ROOT / "packaging" / "profile_gate.py"))
        safe_scan = gate["scan_collected_members"](
            "trusted_test_safe_e2e",
            [
                ("src.main", ""),
                ("run_agent_cli", ""),
                ("src.agent_cli.main", ""),
                ("src.core.cli_runtime.factory", ""),
                ("keyring.backends.macOS", ""),
            ],
            ["license_runtime_config.json", "release_features.json", "skills/lulu-agent/SKILL.md"],
        )
        self.assertEqual(safe_scan["forbidden_count"], 8)
        external_scan = gate["scan_collected_members"](
            "external_candidate",
            [
                ("run_trusted_test_safe_e2e", ""),
                ("src.trusted_test_safe_e2e", ""),
            ],
            [],
        )
        self.assertEqual(external_scan["forbidden_count"], 2)


if __name__ == "__main__":
    unittest.main()
