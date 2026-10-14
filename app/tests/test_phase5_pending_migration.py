"""Phase 5 one-time pending.json migration tests."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from src.core.job_ledger.contracts import JobLedgerError, JobState
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class FailingAnchorStore(FakeIntegrityAnchorStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def compare_and_set(self, expected, replacement) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated migration high-water crash")
        super().compare_and_set(expected, replacement)


def _legacy_payload() -> dict[str, object]:
    return {
        "schema": 2,
        "tasks": [
            {
                "id": 1,
                "title": "待恢复本地文件",
                "source": "/isolated/input.wav",
                "kind": "file",
                "state": "queued",
                "folder_id": None,
                "stable_key": "",
                "execution_policy": None,
            },
            {
                "id": 2,
                "title": "上次运行链接",
                "source": "https://example.invalid/video/2",
                "kind": "link",
                "state": "running",
                "folder_id": "folder-1",
                "stable_key": "legacy:2",
                "execution_policy": {
                    "origin": "single_link",
                    "mode": "materialize",
                    "stable_key": "legacy:2",
                    "media_destination": "temporary",
                    "media_directory": None,
                    "prefer_subtitles": False,
                    "material_snapshot": {
                        "download_mode": "audio",
                        "save_media": False,
                        "save_cover": False,
                        "version": 1,
                    },
                },
            },
        ],
        "groups": [],
    }


def _write_legacy(
    data_root: Path,
    payload: object,
    *,
    mode: int = 0o600,
) -> tuple[Path, bytes]:
    queue_directory = data_root / "task-queue"
    queue_directory.mkdir(mode=0o700, parents=True)
    os.chmod(queue_directory, 0o700)
    path = queue_directory / "pending.json"
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    path.write_bytes(encoded)
    os.chmod(path, mode)
    return path, encoded


class Phase5PendingMigrationTests(unittest.TestCase):
    def test_pending_migration_rejects_unsafe_execution_policy_before_authority_changes(
        self,
    ) -> None:
        migration_module = importlib.import_module("src.core.job_ledger.migration")
        store_module = importlib.import_module("src.core.job_ledger.store")
        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            data_root.mkdir(mode=0o700)
            payload = _legacy_payload()
            payload["tasks"][0]["execution_policy"] = {
                "api_key": "must-never-persist",
            }
            legacy_path, legacy_bytes = _write_legacy(data_root, payload)
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            stable_head = store.head
            stable_journal = store.journal_path.read_bytes()
            stable_snapshot = store.snapshot_path.read_bytes()
            stable_anchor_mutations = anchor.mutation_count

            with self.assertRaises(JobLedgerError) as rejected:
                migration_module.PendingQueueMigrator(data_root, store).migrate(
                    receipt_id="migration-unsafe-policy",
                    committed_at="2026-08-20T03:05:00Z",
                )

            self.assertEqual(rejected.exception.code, "MIGRATION_INTEGRITY_ERROR")
            self.assertEqual(store.head, stable_head)
            self.assertEqual(store.list_jobs(), ())
            self.assertIsNone(store.migration_receipt)
            self.assertEqual(store.journal_path.read_bytes(), stable_journal)
            self.assertEqual(store.snapshot_path.read_bytes(), stable_snapshot)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
            self.assertEqual(stat.S_IMODE(legacy_path.stat().st_mode), 0o600)

    def test_pending_migration_is_atomic_read_once_and_never_dual_writes(self) -> None:
        try:
            migration_module = importlib.import_module(
                "src.core.job_ledger.migration"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 5 pending migration is unavailable: {exc}")
        store_module = importlib.import_module("src.core.job_ledger.store")

        with TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory).resolve() / "isolated-data"
            data_root.mkdir(mode=0o700)
            legacy_path, legacy_bytes = _write_legacy(
                data_root,
                _legacy_payload(),
            )
            anchor = FakeIntegrityAnchorStore()
            store = store_module.JobLedgerStore(data_root, anchor)
            migrator = migration_module.PendingQueueMigrator(data_root, store)

            receipt = migrator.migrate(
                receipt_id="migration-001",
                committed_at="2026-08-20T03:00:00Z",
            )

            self.assertEqual(
                receipt.imported_job_ids,
                ("legacy-task-1", "legacy-task-2"),
            )
            self.assertEqual(store.migration_receipt, receipt)
            self.assertEqual(store.head["sequence"], 1)
            jobs = store.list_jobs()
            self.assertEqual([job.job_id for job in jobs], ["legacy-task-1", "legacy-task-2"])
            self.assertEqual(
                [job.state for job in jobs],
                [JobState.QUEUED, JobState.RECOVERY_REQUIRED],
            )
            self.assertEqual(
                [event.sequence for event in store.events("legacy-task-1")],
                [1],
            )
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
            self.assertEqual(stat.S_IMODE(legacy_path.stat().st_mode), 0o400)
            reopened = store_module.JobLedgerStore(data_root, anchor)
            self.assertEqual(reopened.migration_receipt, receipt)
            self.assertEqual(reopened.list_jobs(), jobs)
            stable_head = store.head
            stable_anchor_mutations = anchor.mutation_count

            repeated = migrator.migrate(
                receipt_id="migration-001",
                committed_at="2026-08-20T03:00:00Z",
            )
            self.assertEqual(repeated, receipt)
            self.assertEqual(store.head, stable_head)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)

        with self.subTest(fault="invalid_schema"):
            with TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory).resolve() / "isolated-data"
                data_root.mkdir(mode=0o700)
                legacy_path, legacy_bytes = _write_legacy(
                    data_root,
                    {**_legacy_payload(), "attacker": True},
                )
                anchor = FakeIntegrityAnchorStore()
                store = store_module.JobLedgerStore(data_root, anchor)
                stable_head = store.head
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(JobLedgerError) as invalid:
                    migration_module.PendingQueueMigrator(
                        data_root,
                        store,
                    ).migrate(
                        receipt_id="migration-invalid",
                        committed_at="2026-08-20T03:10:00Z",
                    )
                self.assertEqual(invalid.exception.code, "MIGRATION_INTEGRITY_ERROR")
                self.assertEqual(store.head, stable_head)
                self.assertEqual(store.list_jobs(), ())
                self.assertIsNone(store.migration_receipt)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)
                self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
                self.assertEqual(stat.S_IMODE(legacy_path.stat().st_mode), 0o600)

        with self.subTest(fault="high_water_crash"):
            with TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory).resolve() / "isolated-data"
                data_root.mkdir(mode=0o700)
                legacy_path, legacy_bytes = _write_legacy(
                    data_root,
                    _legacy_payload(),
                )
                anchor = FailingAnchorStore()
                store = store_module.JobLedgerStore(data_root, anchor)
                anchor.fail_next = True
                with self.assertRaises(JobLedgerError):
                    migration_module.PendingQueueMigrator(
                        data_root,
                        store,
                    ).migrate(
                        receipt_id="migration-crash",
                        committed_at="2026-08-20T03:20:00Z",
                    )
                self.assertEqual(store.head["sequence"], 0)
                self.assertEqual(store.list_jobs(), ())
                self.assertIsNone(store.migration_receipt)
                self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
                self.assertEqual(stat.S_IMODE(legacy_path.stat().st_mode), 0o600)
                reopened = store_module.JobLedgerStore(data_root, anchor)
                self.assertEqual(reopened.head["sequence"], 0)
                self.assertEqual(reopened.list_jobs(), ())
                self.assertIsNone(reopened.migration_receipt)

        for fault, mode in (("legacy_mode", 0o644),):
            with self.subTest(fault=fault):
                with TemporaryDirectory() as temporary_directory:
                    data_root = Path(temporary_directory).resolve() / "isolated-data"
                    data_root.mkdir(mode=0o700)
                    legacy_path, legacy_bytes = _write_legacy(
                        data_root,
                        _legacy_payload(),
                        mode=mode,
                    )
                    anchor = FakeIntegrityAnchorStore()
                    store = store_module.JobLedgerStore(data_root, anchor)
                    with self.assertRaises(JobLedgerError):
                        migration_module.PendingQueueMigrator(
                            data_root,
                            store,
                        ).migrate(
                            receipt_id="migration-mode",
                            committed_at="2026-08-20T03:30:00Z",
                        )
                    self.assertEqual(store.list_jobs(), ())
                    self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
                    self.assertEqual(stat.S_IMODE(legacy_path.stat().st_mode), mode)

        with self.subTest(fault="legacy_symlink"):
            with TemporaryDirectory() as temporary_directory:
                data_root = Path(temporary_directory).resolve() / "isolated-data"
                data_root.mkdir(mode=0o700)
                queue_directory = data_root / "task-queue"
                queue_directory.mkdir(mode=0o700)
                outside = Path(temporary_directory).resolve() / "outside.json"
                outside.write_text("outside", encoding="utf-8")
                legacy_path = queue_directory / "pending.json"
                legacy_path.symlink_to(outside)
                store = store_module.JobLedgerStore(
                    data_root,
                    FakeIntegrityAnchorStore(),
                )
                with self.assertRaises(JobLedgerError):
                    migration_module.PendingQueueMigrator(
                        data_root,
                        store,
                    ).migrate(
                        receipt_id="migration-symlink",
                        committed_at="2026-08-20T03:40:00Z",
                    )
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
                self.assertEqual(store.list_jobs(), ())


if __name__ == "__main__":
    unittest.main()
