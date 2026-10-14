"""Phase 7 shared canonical config repository compatibility window."""

from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


WARM_CONFIG = b'{"theme":"warm","enable_link_download":false}\n'
DARK_CONFIG = b'{"enable_link_download":false,"theme":"dark"}\n'
DARK_SEMANTIC_SHA256 = (
    "3e14a424b2a1b1a4e242e60770170e85a3a5ba2578fd747f0539fd1017082326"
)


class Phase7ConfigRepositoryCompatibilityTests(unittest.TestCase):
    def test_gui_and_cli_callers_share_one_revisioned_repository_without_asset_moves(self) -> None:
        contracts = importlib.import_module("src.core.config_migration.contracts")
        paths_module = importlib.import_module("src.core.config_migration.paths")
        repository_module = importlib.import_module(
            "src.core.config_migration.repository"
        )
        migration_module = importlib.import_module(
            "src.core.config_migration.migration"
        )
        resource_contracts = importlib.import_module(
            "src.core.resource_control.contracts"
        )
        locks_module = importlib.import_module("src.core.resource_control.locks")
        cas_module = importlib.import_module("src.core.resource_control.cas")

        with TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            roots = contracts.ConfigRoots(
                home_root=temporary_root / "home",
                application_support_root=temporary_root / "support",
                resource_root=temporary_root / "resources",
            )
            config_paths = paths_module.ConfigPaths.from_roots(roots)
            config_paths.prepare_layout()
            config_paths.legacy_config.parent.mkdir(mode=0o700)
            config_paths.legacy_config.write_bytes(WARM_CONFIG)
            os.chmod(config_paths.legacy_config, 0o600)
            lock_manager = locks_module.ResourceLockManager(roots.resource_root)
            cas_store = cas_module.ResourceCASStore(roots.resource_root, lock_manager)
            repository = repository_module.ConfigRepository(
                config_paths,
                lock_manager=lock_manager,
                cas_store=cas_store,
            )
            migration = migration_module.ConfigMigrationService(
                config_paths,
                repository=repository,
                lock_manager=lock_manager,
            )
            owner = resource_contracts.LockOwner(
                pid=705,
                process_start="fake-start:705",
                installation_id="install-001",
                operation_id="config-repository",
            )
            migrated = migration.migrate(
                owner=owner,
                migration_id="migration-compatibility",
                started_at="2026-08-20T08:30:00Z",
            )
            try:
                gui_snapshot = repository.read_canonical()
                cli_snapshot = repository.read_canonical()
            except AttributeError as exc:
                self.fail(f"Phase 7 shared repository read seam is unavailable: {exc}")
            self.assertEqual(gui_snapshot, cli_snapshot)
            self.assertEqual(gui_snapshot.revision, migrated.canonical_revision)
            self.assertEqual(gui_snapshot.values["theme"], "warm")

            references = repository.shared_data_references()
            self.assertEqual(
                references.transcripts,
                roots.application_support_root / "Lulu" / "library",
            )
            self.assertEqual(
                references.voices,
                roots.application_support_root / "Lulu" / "voices",
            )
            self.assertEqual(
                references.models,
                roots.application_support_root / "Lulu" / "models",
            )
            self.assertEqual(
                references.jobs,
                roots.application_support_root / "Lulu" / "jobs",
            )
            for referenced_path in (
                references.transcripts,
                references.voices,
                references.models,
                references.jobs,
            ):
                self.assertFalse(referenced_path.exists())

            lease = lock_manager.claim(
                resource_contracts.ResourceKind.CONFIG,
                repository_module.CONFIG_RESOURCE_ID,
                owner,
            )
            updated = repository.write_canonical(
                lease,
                expected=gui_snapshot.revision,
                payload=DARK_CONFIG,
            )
            self.assertEqual(updated.revision.revision, 2)
            self.assertEqual(updated.revision.semantic_sha256, DARK_SEMANTIC_SHA256)
            self.assertEqual(repository.read_canonical(), updated)
            stable_config = config_paths.canonical_config.read_bytes()
            stable_cas = cas_store.read(
                resource_contracts.ResourceKind.CONFIG,
                repository_module.CONFIG_RESOURCE_ID,
            )
            with self.assertRaises(contracts.ConfigMigrationError) as stale:
                repository.write_canonical(
                    lease,
                    expected=gui_snapshot.revision,
                    payload=WARM_CONFIG,
                )
            self.assertEqual(
                (stale.exception.code, stale.exception.exit_code),
                ("CONFIG_MIGRATION_CONFLICT", 21),
            )
            self.assertEqual(config_paths.canonical_config.read_bytes(), stable_config)
            self.assertEqual(
                cas_store.read(
                    resource_contracts.ResourceKind.CONFIG,
                    repository_module.CONFIG_RESOURCE_ID,
                ),
                stable_cas,
            )
            with self.assertRaises(contracts.ConfigMigrationError):
                repository.write_canonical(
                    lease,
                    expected=updated.revision,
                    payload=b'{"theme":"warm","client_secret":"forbidden"}\n',
                )
            self.assertEqual(config_paths.canonical_config.read_bytes(), stable_config)
            lock_manager.release(lease)

        production_source = (
            inspect.getsource(repository_module)
            + inspect.getsource(paths_module)
            + inspect.getsource(migration_module)
        )
        for forbidden in (
            "src.config",
            "src.agent_cli",
            "src.ui",
            "Path.home(",
            "app_data_dir",
            "keyring",
            "PySide6",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, production_source)


if __name__ == "__main__":
    unittest.main()
