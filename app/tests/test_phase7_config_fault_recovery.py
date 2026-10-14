"""Phase 7 fault injection and journal-bound configuration recovery."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


LEGACY_CONFIG = b'{"theme":"warm","enable_link_download":false}\n'


class _FaultInjector:
    def __init__(self, point: str, contracts) -> None:
        self.point = point
        self.contracts = contracts
        self.calls: list[str] = []

    def hit(self, point: str) -> None:
        self.calls.append(point)
        if point == self.point:
            raise self.contracts.ConfigMigrationError(
                "INTEGRITY_ERROR",
                exit_code=60,
                reason_code=f"FAULT_{point.upper()}",
            )


class Phase7ConfigFaultRecoveryTests(unittest.TestCase):
    def test_recovery_classifies_unsupported_versions_before_supported_schema_closure(self) -> None:
        contracts = importlib.import_module("src.core.config_migration.contracts")
        recovery_module = importlib.import_module(
            "src.core.config_migration.recovery"
        )
        cases = (
            (
                "future-extra-fields",
                lambda payload: (
                    payload.__setitem__(
                        "schema", "lulu.config-migration-journal/v2"
                    ),
                    payload.__setitem__("future_top_level", {"opaque": True}),
                    payload["journal"].__setitem__("compatibility_version", 2),
                    payload["journal"].__setitem__("future_journal_field", "opaque"),
                ),
                "CONFIG_ROLLBACK_VERSION_INCOMPATIBLE",
            ),
            (
                "old-version",
                lambda payload: (
                    payload.__setitem__(
                        "schema", "lulu.config-migration-journal/v0"
                    ),
                    payload["journal"].__setitem__("compatibility_version", 0),
                ),
                "CONFIG_ROLLBACK_VERSION_INCOMPATIBLE",
            ),
            (
                "malformed-schema",
                lambda payload: payload.__setitem__(
                    "schema", "lulu.config-migration-journal/vnext"
                ),
                "CONFIG_JOURNAL_INVALID",
            ),
            (
                "missing-schema",
                lambda payload: payload.pop("schema"),
                "CONFIG_JOURNAL_INVALID",
            ),
            (
                "missing-compatibility",
                lambda payload: payload["journal"].pop("compatibility_version"),
                "CONFIG_JOURNAL_INVALID",
            ),
            (
                "boolean-compatibility",
                lambda payload: payload["journal"].__setitem__(
                    "compatibility_version", True
                ),
                "CONFIG_JOURNAL_INVALID",
            ),
        )
        for name, mutate, expected_reason in cases:
            with self.subTest(case=name), TemporaryDirectory() as temporary:
                fixture = self._fixture(
                    Path(temporary).resolve(),
                    _FaultInjector("after_journal_prepared", contracts),
                )
                migration_id = f"migration-version-{name}"
                with self.assertRaises(contracts.ConfigMigrationError):
                    fixture["service"].migrate(
                        owner=fixture["owner"],
                        migration_id=migration_id,
                        started_at="2026-08-20T08:00:00Z",
                    )
                journal_path = fixture["paths"].journal_path(migration_id)
                payload = json.loads(journal_path.read_text("utf-8"))
                mutate(payload)
                journal_path.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(journal_path, 0o600)
                recovery = recovery_module.ConfigRecoveryService(
                    fixture["paths"],
                    repository=fixture["repository"],
                    lock_manager=fixture["lock_manager"],
                )
                stable = self._state(fixture["paths"])

                with self.assertRaises(contracts.ConfigMigrationError) as raised:
                    recovery.recover(
                        owner=fixture["recovery_owner"],
                        migration_id=migration_id,
                        recovered_at="2026-08-20T08:01:00Z",
                    )

                self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(raised.exception.reason_code, expected_reason)
                self.assertEqual(self._state(fixture["paths"]), stable)

    def test_faults_preserve_old_truth_and_recovery_requires_bound_backup(self) -> None:
        contracts = importlib.import_module("src.core.config_migration.contracts")
        try:
            recovery_module = importlib.import_module(
                "src.core.config_migration.recovery"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 7 config recovery seam is unavailable: {exc}")

        for point in ("before_backup_write", "before_journal_write"):
            with self.subTest(fault=point), TemporaryDirectory() as temporary:
                fixture = self._fixture(
                    Path(temporary).resolve(),
                    _FaultInjector(point, contracts),
                )
                with self.assertRaises(contracts.ConfigMigrationError):
                    fixture["service"].migrate(
                        owner=fixture["owner"],
                        migration_id=f"migration-{point}",
                        started_at="2026-08-20T08:20:00Z",
                    )
                self.assertEqual(
                    fixture["paths"].legacy_config.read_bytes(),
                    LEGACY_CONFIG,
                )
                self.assertFalse(fixture["paths"].canonical_config.exists())
                self.assertEqual(
                    tuple(fixture["paths"].journal_directory.iterdir()),
                    (),
                )

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary).resolve(),
                _FaultInjector("after_journal_prepared", contracts),
            )
            migration_id = "migration-interrupted"
            with self.assertRaises(contracts.ConfigMigrationError):
                fixture["service"].migrate(
                    owner=fixture["owner"],
                    migration_id=migration_id,
                    started_at="2026-08-20T08:21:00Z",
                )
            journal_path = fixture["paths"].journal_path(migration_id)
            self.assertTrue(journal_path.is_file())
            self.assertFalse(fixture["paths"].canonical_config.exists())
            recovery = recovery_module.ConfigRecoveryService(
                fixture["paths"],
                repository=fixture["repository"],
                lock_manager=fixture["lock_manager"],
            )
            rollback = recovery.recover(
                owner=fixture["recovery_owner"],
                migration_id=migration_id,
                recovered_at="2026-08-20T08:22:00Z",
            )
            self.assertEqual(rollback.migration_id, migration_id)
            self.assertEqual(rollback.restored_fingerprint.content_sha256, (
                "dcf9f1a15402207380ea88bbc1d3679d6f32b57fb1bd3eccb0dcad6f6b6eb3da"
            ))
            self.assertIsNone(rollback.canonical_revision)
            self.assertFalse(fixture["paths"].canonical_config.exists())
            self.assertEqual(
                fixture["paths"].legacy_config.read_bytes(),
                LEGACY_CONFIG,
            )

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary).resolve(),
                _FaultInjector("before_canonical_readback", contracts),
            )
            migration_id = "migration-tampered-backup"
            with self.assertRaises(contracts.ConfigMigrationError):
                fixture["service"].migrate(
                    owner=fixture["owner"],
                    migration_id=migration_id,
                    started_at="2026-08-20T08:23:00Z",
                )
            journal = json.loads(
                fixture["paths"].journal_path(migration_id).read_text("utf-8")
            )
            backup = Path(journal["journal"]["backup_path"])
            backup.write_bytes(b"tampered")
            os.chmod(backup, 0o600)
            recovery = recovery_module.ConfigRecoveryService(
                fixture["paths"],
                repository=fixture["repository"],
                lock_manager=fixture["lock_manager"],
            )
            stable = self._state(fixture["paths"])
            with self.assertRaises(contracts.ConfigMigrationError) as tampered:
                recovery.recover(
                    owner=fixture["recovery_owner"],
                    migration_id=migration_id,
                    recovered_at="2026-08-20T08:24:00Z",
                )
            self.assertEqual(tampered.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(self._state(fixture["paths"]), stable)

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary).resolve(),
                _FaultInjector("before_canonical_readback", contracts),
            )
            migration_id = "migration-readback"
            with self.assertRaises(contracts.ConfigMigrationError):
                fixture["service"].migrate(
                    owner=fixture["owner"],
                    migration_id=migration_id,
                    started_at="2026-08-20T08:25:00Z",
                )
            self.assertEqual(
                fixture["paths"].canonical_config.read_bytes(),
                LEGACY_CONFIG,
            )
            recovery = recovery_module.ConfigRecoveryService(
                fixture["paths"],
                repository=fixture["repository"],
                lock_manager=fixture["lock_manager"],
            )
            rollback = recovery.recover(
                owner=fixture["recovery_owner"],
                migration_id=migration_id,
                recovered_at="2026-08-20T08:26:00Z",
            )
            self.assertIsNone(rollback.canonical_revision)
            self.assertFalse(fixture["paths"].canonical_config.exists())
            self.assertEqual(
                fixture["paths"].legacy_config.read_bytes(),
                LEGACY_CONFIG,
            )

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary).resolve(),
                _FaultInjector("after_journal_prepared", contracts),
            )
            migration_id = "migration-incompatible"
            with self.assertRaises(contracts.ConfigMigrationError):
                fixture["service"].migrate(
                    owner=fixture["owner"],
                    migration_id=migration_id,
                    started_at="2026-08-20T08:27:00Z",
                )
            journal_path = fixture["paths"].journal_path(migration_id)
            journal = json.loads(journal_path.read_text("utf-8"))
            journal["journal"]["compatibility_version"] = 999
            journal_path.write_text(
                json.dumps(
                    journal,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(journal_path, 0o600)
            recovery = recovery_module.ConfigRecoveryService(
                fixture["paths"],
                repository=fixture["repository"],
                lock_manager=fixture["lock_manager"],
            )
            stable = self._state(fixture["paths"])
            with self.assertRaises(contracts.ConfigMigrationError) as incompatible:
                recovery.recover(
                    owner=fixture["recovery_owner"],
                    migration_id=migration_id,
                    recovered_at="2026-08-20T08:28:00Z",
                )
            self.assertEqual(
                incompatible.exception.reason_code,
                "CONFIG_ROLLBACK_VERSION_INCOMPATIBLE",
            )
            self.assertEqual(self._state(fixture["paths"]), stable)

    @staticmethod
    def _fixture(temporary_root: Path, fault_injector) -> dict[str, object]:
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
        roots = contracts.ConfigRoots(
            home_root=temporary_root / "home",
            application_support_root=temporary_root / "support",
            resource_root=temporary_root / "resources",
        )
        config_paths = paths_module.ConfigPaths.from_roots(roots)
        config_paths.prepare_layout()
        config_paths.legacy_config.parent.mkdir(mode=0o700)
        config_paths.legacy_config.write_bytes(LEGACY_CONFIG)
        os.chmod(config_paths.legacy_config, 0o600)
        lock_manager = locks_module.ResourceLockManager(roots.resource_root)
        cas_store = cas_module.ResourceCASStore(roots.resource_root, lock_manager)
        repository = repository_module.ConfigRepository(
            config_paths,
            lock_manager=lock_manager,
            cas_store=cas_store,
            fault_injector=fault_injector,
        )
        service = migration_module.ConfigMigrationService(
            config_paths,
            repository=repository,
            lock_manager=lock_manager,
            fault_injector=fault_injector,
        )
        return {
            "paths": config_paths,
            "lock_manager": lock_manager,
            "repository": repository,
            "service": service,
            "owner": resource_contracts.LockOwner(
                pid=703,
                process_start="fake-start:703",
                installation_id="install-001",
                operation_id="config-migrate-fault",
            ),
            "recovery_owner": resource_contracts.LockOwner(
                pid=704,
                process_start="fake-start:704",
                installation_id="install-001",
                operation_id="config-recovery",
            ),
        }

    @staticmethod
    def _state(config_paths) -> tuple[object, ...]:
        def file_bytes(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        def directory_state(path: Path) -> tuple[tuple[str, bytes], ...]:
            return tuple(sorted((item.name, item.read_bytes()) for item in path.iterdir()))

        return (
            file_bytes(config_paths.legacy_config),
            file_bytes(config_paths.canonical_config),
            directory_state(config_paths.journal_directory),
            directory_state(config_paths.backup_directory),
        )


if __name__ == "__main__":
    unittest.main()
