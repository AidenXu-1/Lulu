from __future__ import annotations

from pathlib import Path
import unittest


class Phase9ActivatedSkillWorkflowTests(unittest.TestCase):
    def test_skill_verifies_then_invokes_only_the_packaged_local_cli_surface(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        skill = (app_root / "skills" / "lulu-agent" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        workflows = (
            app_root / "skills" / "lulu-agent" / "references" / "workflows.md"
        ).read_text(encoding="utf-8")
        protocol = (
            app_root / "skills" / "lulu-agent" / "references" / "protocol.md"
        ).read_text(encoding="utf-8")
        errors = (
            app_root / "skills" / "lulu-agent" / "references" / "errors.md"
        ).read_text(encoding="utf-8")
        trust_anchor = (
            app_root / "skills" / "lulu-agent" / "references" / "trust-anchor.md"
        ).read_text(encoding="utf-8")
        combined = "\n".join((skill, workflows, protocol, errors, trust_anchor))

        for command in (
            "lulu transcribe prepare <absolute-file> --output json",
            "lulu transcribe run <absolute-file> --output json",
            "lulu jobs list",
            "lulu jobs show",
            "lulu jobs wait",
            "lulu jobs cancel",
            "lulu jobs receipt",
        ):
            self.assertIn(command, combined)
        self.assertIn("verify_lulu_generation9.py", skill)
        self.assertIn("canonical CLI", combined)
        self.assertIn("generation 9", combined)
        self.assertIn("measured current-user Core", combined)
        self.assertNotIn("import src.core", combined)
        self.assertNotIn("accept the final unavailable result", workflows)
        self.assertIn("Lulu Settings > General > Lulu Agent Skill", trust_anchor)
        self.assertNotIn("Agent invocation > Repair access", trust_anchor)


if __name__ == "__main__":
    unittest.main()
