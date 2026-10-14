"""Phase 9 Skill Creator structure and bundled-source contract."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


class Phase9SkillStructureTests(unittest.TestCase):
    def test_skill_is_thin_progressively_disclosed_and_bundled_without_install(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        skill_root = app_root / "skills" / "lulu-agent"
        expected_files = {
            "SKILL.md",
            "manifest.json",
            "scripts/verify_lulu_install.py",
            "scripts/verify_lulu_generation9.py",
            "scripts/verify_delivery_tree_v2.py",
            "references/protocol.md",
            "references/workflows.md",
            "references/workspace.md",
            "references/voice.md",
            "references/links.md",
            "references/errors.md",
            "references/trust-anchor.md",
        }
        self.assertTrue(skill_root.is_dir(), "Phase 9 lulu-agent skill is missing")
        actual_files = {
            str(path.relative_to(skill_root))
            for path in skill_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual_files, expected_files)
        self.assertFalse((skill_root / "README.md").exists())
        self.assertFalse((skill_root / "agents").exists())
        self.assertFalse((skill_root / "assets").exists())

        entry = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(entry.splitlines()), 70)
        self.assertTrue(entry.startswith("---\nname: lulu-agent\n"))
        self.assertIn(
            "description: Operate Lulu through its verified packaged Agent CLI",
            entry,
        )
        for reference in ("protocol", "workflows", "errors", "trust-anchor"):
            self.assertIn(f"references/{reference}.md", entry)
        self.assertNotIn("src.core", entry)
        self.assertNotIn("README", entry)

        manifest = json.loads(
            (skill_root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_id"], "lulu.agent-skill-manifest/v1")
        self.assertEqual(manifest["candidate_generation"], 9)
        self.assertEqual(manifest["product_manifest"]["schema_id"], "lulu.agent-component-manifest/v1")
        self.assertEqual(manifest["entry"]["canonical_cli"], "/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli")
        self.assertIs(manifest["entry"]["fallback"], False)
        self.assertIs(manifest["contains_secret_material"], False)

        spec = (app_root / "packaging" / "Lulu.spec").read_text(encoding="utf-8")
        self.assertIn('AGENT_SKILL_SOURCE = os.path.join(APP_DIR, "skills", "lulu-agent")', spec)
        self.assertIn('datas += [(AGENT_SKILL_SOURCE, "skills/lulu-agent")]', spec)
        product_manifest = json.loads(
            (app_root / "packaging" / "agent_product_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            product_manifest["bundled_skill_source"],
            {
                "relative_path": "skills/lulu-agent",
                "manifest_schema_id": "lulu.agent-skill-manifest/v1",
                "skill_version": "1.0.0",
                "automatic_installation": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
