from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class Generation9ManagedEffectsTests(unittest.TestCase):
    def test_core_registration_combines_exact_plist_and_current_user_service(self) -> None:
        from src.core.agent_entry.effects import CoreRegistrationEffect

        class Service:
            loaded = False
            starts = 0
            stops = 0

            def is_loaded(self):
                return self.loaded

            def start(self, _plist):
                self.loaded = True
                self.starts += 1

            def stop(self):
                self.loaded = False
                self.stops += 1

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.plist"
            source.write_text("plist\n", encoding="utf-8")
            service = Service()
            effect = CoreRegistrationEffect(
                source=source,
                target=root / "Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
                work_root=root / "state/core",
                service=service,
            )
            self.assertEqual(effect.probe().state, "absent")
            effect.apply()
            self.assertEqual(effect.probe().state, "after")
            self.assertEqual(service.starts, 1)
            effect.rollback()
            self.assertEqual(effect.probe().state, "absent")
            self.assertEqual(service.stops, 1)

    def test_file_and_tree_effects_are_atomic_reversible_and_preserve_third_party(self) -> None:
        from src.core.agent_entry.effects import ManagedPathEffect

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_file = root / "source-launcher"
            source_file.write_bytes(b"managed launcher\n")
            source_file.chmod(0o700)
            target_file = root / "bin/lulu"
            file_effect = ManagedPathEffect(
                name="launcher",
                source=source_file,
                target=target_file,
                work_root=root / "state/launcher",
            )
            self.assertEqual(file_effect.probe().state, "absent")
            file_effect.apply()
            self.assertEqual(file_effect.probe().state, "after")
            self.assertEqual(target_file.read_bytes(), b"managed launcher\n")
            file_effect.rollback()
            self.assertEqual(file_effect.probe().state, "absent")

            source_tree = root / "source-skill"
            source_tree.mkdir()
            (source_tree / "SKILL.md").write_text("managed\n", encoding="utf-8")
            target_tree = root / "skills/lulu-agent"
            target_tree.mkdir(parents=True)
            (target_tree / "foreign.txt").write_text("preserve\n", encoding="utf-8")
            tree_effect = ManagedPathEffect(
                name="skill",
                source=source_tree,
                target=target_tree,
                work_root=root / "state/skill",
            )
            self.assertEqual(tree_effect.probe().state, "third_party")
            with self.assertRaisesRegex(RuntimeError, "OWNERSHIP_CONFLICT"):
                tree_effect.apply()
            self.assertEqual((target_tree / "foreign.txt").read_text(), "preserve\n")

    def test_update_restores_exact_before_object(self) -> None:
        from src.core.agent_entry.effects import ManagedPathEffect, object_digest

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.write_text("new\n", encoding="utf-8")
            target.write_text("old\n", encoding="utf-8")
            old_digest = object_digest(target)
            effect = ManagedPathEffect(
                name="launcher",
                source=source,
                target=target,
                work_root=root / "state",
                expected_before_digest=old_digest,
            )
            self.assertEqual(effect.probe().state, "before")
            effect.apply()
            self.assertEqual(target.read_text(), "new\n")
            effect.rollback()
            self.assertEqual(target.read_text(), "old\n")
            self.assertEqual(object_digest(target), old_digest)


if __name__ == "__main__":
    unittest.main()
