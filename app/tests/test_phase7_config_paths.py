"""Phase 7 explicit-root configuration migration path contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import importlib
import inspect
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest


class Phase7ConfigPathTests(unittest.TestCase):
    def test_explicit_roots_resolve_closed_owner_only_paths_without_user_home(self) -> None:
        try:
            contracts = importlib.import_module(
                "src.core.config_migration.contracts"
            )
            paths_module = importlib.import_module(
                "src.core.config_migration.paths"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 7 config path seam is unavailable: {exc}")

        self.assertEqual(
            tuple(decision.value for decision in contracts.ConflictDecision),
            ("use_legacy", "use_canonical", "export_both_and_abort"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.ConfigRoots)),
            ("home_root", "application_support_root", "resource_root"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.ConfigRevision)),
            ("revision", "content_sha256", "semantic_sha256"),
        )

        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            roots = contracts.ConfigRoots(
                home_root=temporary_root / "synthetic-home",
                application_support_root=temporary_root / "synthetic-support",
                resource_root=temporary_root / "synthetic-resources",
            )
            with self.assertRaises(FrozenInstanceError):
                roots.home_root = temporary_root  # type: ignore[misc]

            resolved = paths_module.ConfigPaths.from_roots(roots)
            self.assertEqual(
                resolved.legacy_config,
                roots.home_root / ".本地转录app" / "config.json",
            )
            self.assertEqual(
                resolved.canonical_config,
                roots.application_support_root
                / "Lulu"
                / "config"
                / "config.json",
            )
            self.assertEqual(
                resolved.journal_directory,
                roots.application_support_root / "Lulu" / "config" / "migration",
            )
            self.assertEqual(
                resolved.backup_directory,
                roots.application_support_root / "Lulu" / "config" / "backups",
            )
            resolved.prepare_layout()
            for directory in (
                roots.home_root,
                roots.application_support_root,
                roots.resource_root,
                resolved.canonical_config.parent,
                resolved.journal_directory,
                resolved.backup_directory,
            ):
                self.assertTrue(directory.is_dir())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertFalse(resolved.legacy_config.exists())
            self.assertFalse(resolved.canonical_config.exists())

            invalid = contracts.ConfigRoots(
                home_root=Path("relative-home"),
                application_support_root=roots.application_support_root,
                resource_root=roots.resource_root,
            )
            with self.assertRaises(contracts.ConfigMigrationError) as relative:
                paths_module.ConfigPaths.from_roots(invalid)
            self.assertEqual(
                (relative.exception.code, relative.exception.exit_code),
                ("INTEGRITY_ERROR", 60),
            )

            symlink_target = temporary_root / "symlink-target"
            symlink_target.mkdir(mode=0o700)
            symlink_support = temporary_root / "symlink-support"
            symlink_support.symlink_to(symlink_target, target_is_directory=True)
            symlink_roots = contracts.ConfigRoots(
                home_root=roots.home_root,
                application_support_root=symlink_support,
                resource_root=roots.resource_root,
            )
            with self.assertRaises(contracts.ConfigMigrationError) as symlinked:
                paths_module.ConfigPaths.from_roots(symlink_roots)
            self.assertEqual(symlinked.exception.code, "INTEGRITY_ERROR")
            self.assertFalse((symlink_target / "Lulu").exists())

        production_source = inspect.getsource(contracts) + inspect.getsource(
            paths_module
        )
        for forbidden in (
            "Path.home(",
            "app_data_dir",
            "expanduser(",
            "QApplication",
            "PySide6",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, production_source)


if __name__ == "__main__":
    unittest.main()
