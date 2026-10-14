"""Static PyInstaller production secondary and safe primary contract."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


class Phase8DualExecutableSpecTests(unittest.TestCase):
    def test_one_analysis_feeds_profile_primary_and_production_secondary(self) -> None:
        spec_path = Path(__file__).resolve().parents[1] / "packaging" / "Lulu.spec"
        source = spec_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(spec_path))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

        analysis_calls = [
            node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "Analysis"
        ]
        pyz_calls = [
            node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "PYZ"
        ]
        exe_calls = [
            node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "EXE"
        ]

        self.assertEqual(len(analysis_calls), 1, "Phase 8 requires one shared Analysis")
        self.assertEqual(len(pyz_calls), 1, "Phase 8 requires one shared Python archive")
        self.assertEqual(len(exe_calls), 3, "Spec defines the profile primary plus production CLI and Core")
        self.assertIn('AGENT_CLI_ENTRY = os.path.join(APP_DIR, "run_agent_cli.py")', source)
        self.assertIn('AGENT_CLI_ENABLED = BUILD_PROFILE == "external_candidate"', source)
        self.assertIn('TRUSTED_TEST_SAFE_E2E_ENABLED = BUILD_PROFILE == "trusted_test_safe_e2e"', source)
        self.assertIn("ANALYSIS_ENTRIES = [ENTRY]", source)
        self.assertIn("ANALYSIS_ENTRIES.append(AGENT_CLI_ENTRY)", source)
        self.assertIn("ANALYSIS_ENTRIES.append(AGENT_CORE_ENTRY)", source)
        self.assertIn("a = Analysis(\n    ANALYSIS_ENTRIES,", source)
        self.assertIn("gui_exe = EXE(", source)
        self.assertIn("console=TRUSTED_TEST_SAFE_E2E_ENABLED", source)
        self.assertIn("agent_cli_exe = EXE(", source)
        self.assertIn('name="lulu-agent-cli"', source)
        self.assertIn('name="lulu-agent-core"', source)
        self.assertIn("console=True", source)
        self.assertIn("collect_executables.append(agent_cli_exe)", source)
        self.assertIn("collect_executables.append(agent_core_exe)", source)
        self.assertIn("coll = COLLECT(\n    *collect_executables,", source)
        self.assertNotIn("Analysis([AGENT_CLI_ENTRY]", source)


if __name__ == "__main__":
    unittest.main()
