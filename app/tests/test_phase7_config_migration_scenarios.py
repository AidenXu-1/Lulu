"""Phase 7 complete configuration migration scenario matrix."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest


WARM_SEMANTIC_SHA256 = (
    "372b4abc5f0ad8a7bd56775f547e81006c03db974ba52e21ec37a4d661d9f9fc"
)
WARM_RAW_SHA256 = (
    "dcf9f1a15402207380ea88bbc1d3679d6f32b57fb1bd3eccb0dcad6f6b6eb3da"
)
WARM_CONFIG = b'{"theme":"warm","enable_link_download":false}\n'
WARM_FORMATTED = b'{\n  "enable_link_download": false,\n  "theme": "warm"\n}\n'
DARK_CONFIG = b'{"enable_link_download":false,"theme":"dark"}\n'


class Phase7ConfigMigrationScenarioTests(unittest.TestCase):
    def test_complete_scenario_matrix_is_deterministic_and_never_guesses(self) -> None:
        contracts = importlib.import_module("src.core.config_migration.contracts")
        paths_module = importlib.import_module("src.core.config_migration.paths")
        try:
            repository_module = importlib.import_module(
                "src.core.config_migration.repository"
            )
            migration_module = importlib.import_module(
                "src.core.config_migration.migration"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 7 migration scenario seam is unavailable: {exc}")
        resource_contracts = importlib.import_module(
            "src.core.resource_control.contracts"
        )
        locks_module = importlib.import_module("src.core.resource_control.locks")
        cas_module = importlib.import_module("src.core.resource_control.cas")

        scenarios = (
            ("none", None, None, "no_config"),
            ("legacy_only", WARM_CONFIG, None, "migrated_legacy"),
            ("canonical_only", None, WARM_CONFIG, "canonical_only"),
            ("equal", WARM_CONFIG, WARM_FORMATTED, "semantically_equal"),
            ("conflict", WARM_CONFIG, DARK_CONFIG, None),
            ("legacy_damaged", b"{broken", None, None),
            ("canonical_damaged", WARM_CONFIG, b"[]\n", None),
            ("secret_field", b'{"theme":"warm","api_key":"forbidden"}\n', None, None),
        )
        for name, legacy_payload, canonical_payload, expected_outcome in scenarios:
            with self.subTest(scenario=name), TemporaryDirectory() as temporary:
                temporary_root = Path(temporary).resolve()
                roots = contracts.ConfigRoots(
                    home_root=temporary_root / "home",
                    application_support_root=temporary_root / "support",
                    resource_root=temporary_root / "resources",
                )
                config_paths = paths_module.ConfigPaths.from_roots(roots)
                config_paths.prepare_layout()
                if legacy_payload is not None:
                    config_paths.legacy_config.parent.mkdir(mode=0o700)
                    config_paths.legacy_config.write_bytes(legacy_payload)
                    os.chmod(config_paths.legacy_config, 0o600)
                if canonical_payload is not None:
                    config_paths.canonical_config.write_bytes(canonical_payload)
                    os.chmod(config_paths.canonical_config, 0o600)

                lock_manager = locks_module.ResourceLockManager(roots.resource_root)
                cas_store = cas_module.ResourceCASStore(
                    roots.resource_root,
                    lock_manager,
                )
                repository = repository_module.ConfigRepository(
                    config_paths,
                    lock_manager=lock_manager,
                    cas_store=cas_store,
                )
                service = migration_module.ConfigMigrationService(
                    config_paths,
                    repository=repository,
                    lock_manager=lock_manager,
                )
                owner = resource_contracts.LockOwner(
                    pid=701,
                    process_start="fake-start:701",
                    installation_id="installation-test",
                    operation_id=f"config-migration:{name}",
                )
                before = self._business_state(config_paths, cas_store)

                if expected_outcome is None:
                    with self.assertRaises(contracts.ConfigMigrationError) as rejected:
                        service.migrate(
                            owner=owner,
                            migration_id=f"migration-{name}",
                            started_at="2026-08-20T08:00:00Z",
                        )
                    if name == "conflict":
                        self.assertEqual(
                            (rejected.exception.code, rejected.exception.exit_code),
                            ("CONFIG_MIGRATION_CONFLICT", 21),
                        )
                    else:
                        self.assertEqual(rejected.exception.code, "INTEGRITY_ERROR")
                    self.assertEqual(
                        self._business_state(config_paths, cas_store),
                        before,
                    )
                    continue

                receipt = service.migrate(
                    owner=owner,
                    migration_id=f"migration-{name}",
                    started_at="2026-08-20T08:00:00Z",
                )
                self.assertEqual(receipt.outcome, expected_outcome)
                self.assertTrue(Path(receipt.journal_path).is_file())
                self.assertEqual(
                    stat.S_IMODE(Path(receipt.journal_path).stat().st_mode),
                    0o600,
                )
                if expected_outcome == "no_config":
                    self.assertFalse(config_paths.legacy_config.exists())
                    self.assertFalse(config_paths.canonical_config.exists())
                    self.assertIsNone(receipt.canonical_revision)
                    continue

                self.assertTrue(config_paths.canonical_config.is_file())
                self.assertEqual(
                    stat.S_IMODE(config_paths.canonical_config.stat().st_mode),
                    0o600,
                )
                self.assertIsNotNone(receipt.canonical_revision)
                self.assertEqual(
                    receipt.canonical_revision.semantic_sha256,
                    WARM_SEMANTIC_SHA256,
                )
                if expected_outcome == "migrated_legacy":
                    self.assertEqual(config_paths.canonical_config.read_bytes(), WARM_CONFIG)
                    self.assertEqual(config_paths.legacy_config.read_bytes(), WARM_CONFIG)
                    self.assertEqual(
                        receipt.canonical_revision.content_sha256,
                        WARM_RAW_SHA256,
                    )
                    self.assertIsNotNone(receipt.backup_path)
                    backup = Path(receipt.backup_path)
                    self.assertEqual(backup.read_bytes(), WARM_CONFIG)
                    self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                elif expected_outcome == "semantically_equal":
                    self.assertEqual(
                        config_paths.canonical_config.read_bytes(),
                        WARM_FORMATTED,
                    )

    @staticmethod
    def _business_state(config_paths, cas_store) -> tuple[object, ...]:
        def file_bytes(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        journal = tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in config_paths.journal_directory.iterdir()
            )
        )
        backups = tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in config_paths.backup_directory.iterdir()
            )
        )
        cas_records = tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in cas_store.record_directory.iterdir()
            )
        )
        return (
            file_bytes(config_paths.legacy_config),
            file_bytes(config_paths.canonical_config),
            journal,
            backups,
            cas_records,
        )


if __name__ == "__main__":
    unittest.main()
