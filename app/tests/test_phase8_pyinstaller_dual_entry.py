"""Phase 8 shared-PYZ dual-entry parity with the final profile gate."""

from __future__ import annotations

from pathlib import Path
import runpy
import unittest


class Phase8PyInstallerDualEntryTests(unittest.TestCase):
    def test_spec_bundles_the_catalog_at_the_explicit_product_resource_path(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        spec = (app_root / "packaging" / "Lulu.spec").read_text(encoding="utf-8")

        self.assertIn(
            'AGENT_CAPABILITY_CATALOG = os.path.join(APP_DIR, "src", "agent_cli", "capability_catalog.json")',
            spec,
        )
        self.assertIn(
            'if not os.path.isfile(AGENT_CAPABILITY_CATALOG):',
            spec,
        )
        self.assertIn(
            'datas += [(AGENT_CAPABILITY_CATALOG, "src/agent_cli")]',
            spec,
        )

    def test_spec_and_final_gate_keep_only_production_secondary_entry(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        spec = (app_root / "packaging" / "Lulu.spec").read_text(encoding="utf-8")
        gate = runpy.run_path(str(app_root / "packaging" / "profile_gate.py"))

        self.assertIn("ANALYSIS_ENTRIES = [ENTRY]", spec)
        self.assertIn("ANALYSIS_ENTRIES.append(AGENT_CLI_ENTRY)", spec)
        self.assertEqual(spec.count("a = Analysis("), 1)
        self.assertEqual(spec.count("pyz = PYZ("), 1)
        self.assertEqual(
            gate["APPROVED_SECONDARY_ENTRY_CONTRACTS"],
            {
                "external_candidate": {
                    "module": "run_agent_cli",
                    "relative_path": "Contents/MacOS/lulu-agent-cli",
                    "contract": "agent_cli_executable",
                },
            },
        )
        self.assertEqual(
            gate["APPROVED_TERTIARY_ENTRY_CONTRACTS"]["external_candidate"],
            {
                "module": "run_agent_core",
                "relative_path": "Contents/MacOS/lulu-agent-core",
                "contract": "agent_core_executable",
            },
        )
        self.assertNotIn("internal_test", gate["APPROVED_SECONDARY_ENTRY_CONTRACTS"])
        self.assertNotIn("trusted_test_safe_e2e", gate["APPROVED_SECONDARY_ENTRY_CONTRACTS"])


if __name__ == "__main__":
    unittest.main()
