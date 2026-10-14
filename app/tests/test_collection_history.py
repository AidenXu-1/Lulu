"""H1 跨重启批次历史仓库：纯本地、故障注入、无外部服务。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core.collect import history as history_module
from src.core.collect.history import CollectionHistoryRepository, HistoryError
from src.core.collect.models import CollectedItem


def item(
    work_id: str,
    *,
    platform: str = "douyin",
    author: str = "同一作者",
    like: int = 0,
    unknown_like: bool = False,
) -> CollectedItem:
    extra = {"_field_state_like": "unknown"} if unknown_like else {}
    return CollectedItem(
        work_id if platform == "douyin" else "",
        platform=platform,
        work_id=work_id,
        author_name=author,
        author_id="author-1",
        title=f"作品 {work_id}",
        like=like,
        work_url=f"https://example.test/{platform}/{work_id}",
        extra_stats=extra,
        content_kind="unknown" if platform == "xiaohongshu" else "video",
    )


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root / "collection" / "history.json"
        self.legacy_path = root / "collections" / "snapshots.json"
        self.repo = CollectionHistoryRepository(self.path, legacy_path=self.legacy_path)

    def add(
        self,
        rows=None,
        *,
        platform="douyin",
        completion="complete",
        reason_code="",
        confirmed_empty=False,
        now=None,
    ):
        return self.repo.add_record(
            rows if rows is not None else [item("1", platform=platform)],
            platform=platform,
            homepage_url=f"https://example.test/{platform}/author",
            source_method="profile_public" if platform == "xiaohongshu" else "anonymous_latest",
            completion=completion,
            reason_code=reason_code,
            confirmed_empty=confirmed_empty,
            now=now,
        )

    def assert_error(self, code: str, action):
        with self.assertRaises(HistoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def write_json(self, value: object) -> bytes:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        self.path.write_bytes(payload)
        return payload


class HistoryReadWriteTests(HistoryCase):
    def test_same_author_each_collection_is_independent_across_restart(self):
        first = self.add(
            [item("1"), item("2")], now=datetime(2026, 7, 23, 1, tzinfo=timezone.utc)
        )
        first_id = first.selected_record_id
        second = self.add(
            [item("1"), item("3")], now=datetime(2026, 7, 23, 2, tzinfo=timezone.utc)
        )

        loaded = CollectionHistoryRepository(
            self.path, legacy_path=self.legacy_path
        ).load()
        self.assertEqual(len(loaded.active_records), 2)
        self.assertNotEqual(first_id, second.selected_record_id)
        self.assertEqual(loaded.selected_record_id, second.selected_record_id)
        self.assertEqual(
            [row.record_id for row in loaded.active_records],
            [second.selected_record_id, first_id],
        )

    def test_cross_platform_unknown_and_item_schema_round_trip(self):
        snapshot = self.add(
            [item("xhs-1", platform="xiaohongshu", like=77, unknown_like=True)],
            platform="xiaohongshu",
        )
        record = snapshot.selected_record
        self.assertEqual(record.platform, "xiaohongshu")
        self.assertEqual(record.items[0].stable_key, "xiaohongshu:xhs-1")
        self.assertEqual(record.items[0].field_state("like"), "unknown")
        self.assertIsNone(record.items[0].known_numeric("like"))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["active_records"][0]["items"][0]["schema_version"], 2)

    def test_partial_cancelled_and_confirmed_empty_have_honest_records(self):
        partial = self.add(
            [item("partial")], completion="partial", reason_code="rate_limited"
        )
        self.assertEqual(partial.selected_record.completion, "partial")
        cancelled = self.add(
            [item("cancelled")], completion="cancelled", reason_code="cancelled"
        )
        self.assertEqual(cancelled.selected_record.completion, "cancelled")
        empty = self.add([], confirmed_empty=True)
        self.assertTrue(empty.selected_record.confirmed_empty)
        self.assertEqual(empty.selected_record.items, ())
        self.assert_error(
            "invalid_record",
            lambda: self.add([], completion="partial", reason_code="network_error"),
        )

    def test_mixed_platform_or_duplicate_stable_key_fails_without_write(self):
        before = self.add([item("safe")])
        payload = self.path.read_bytes()
        self.assert_error(
            "invalid_record",
            lambda: self.add([item("1"), item("x", platform="xiaohongshu")]),
        )
        self.assert_error(
            "duplicate_item", lambda: self.add([item("same"), item("same")])
        )
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertEqual(self.repo.load().selected_record_id, before.selected_record_id)

    def test_select_rename_trash_restore_and_purge_are_durable(self):
        first = self.add([item("1")])
        first_id = first.selected_record_id
        second = self.add([item("2")])
        second_id = second.selected_record_id

        selected = self.repo.select(first_id)
        self.assertEqual(selected.selected_record_id, first_id)
        renamed = self.repo.rename(first_id, "研究样本 A")
        self.assertEqual(renamed.selected_record.name, "研究样本 A")
        trashed = self.repo.trash_records([first_id, second_id])
        self.assertEqual(trashed.active_records, ())
        self.assertEqual({row.record_id for row in trashed.trashed_records}, {first_id, second_id})
        restored = self.repo.restore_records([first_id])
        self.assertEqual(restored.selected_record_id, first_id)
        self.assertEqual(restored.selected_record.name, "研究样本 A")
        purged = self.repo.purge_records([second_id])
        self.assertEqual([row.record_id for row in purged.trashed_records], [])

    def test_source_hash_rejects_stale_writer(self):
        first = CollectionHistoryRepository(self.path, legacy_path=self.legacy_path)
        second = CollectionHistoryRepository(self.path, legacy_path=self.legacy_path)
        stale = first.load().source_hash
        second.add_record(
            [item("2")],
            platform="douyin",
            homepage_url="https://example.test/douyin/author",
            source_method="anonymous_latest",
            completion="complete",
        )
        changed = self.path.read_bytes()
        self.assert_error(
            "concurrent_change",
            lambda: first.add_record(
                [item("1")],
                platform="douyin",
                homepage_url="https://example.test/douyin/author",
                source_method="anonymous_latest",
                completion="complete",
                expected_source_hash=stale,
            ),
        )
        self.assertEqual(self.path.read_bytes(), changed)

    def test_item_trash_move_restore_purge_and_round_trip(self):
        snapshot = self.add([
            item("one", like=1),
            item("two", like=2, unknown_like=True),
            item("three", like=3),
        ], completion="partial", reason_code="rate_limited")
        record_id = snapshot.selected_record_id
        other = self.add([item("other")]).selected_record_id
        snapshot = self.repo.select(record_id)

        moved = self.repo.trash_items(
            record_id,
            ["douyin:one", "douyin:two"],
            expected_source_hash=snapshot.source_hash,
        )
        record = moved.selected_record
        self.assertEqual([row.stable_key for row in record.items], ["douyin:three"])
        self.assertEqual(
            [row.stable_key for row in record.item_trash],
            ["douyin:one", "douyin:two"],
        )
        self.assertEqual(record.item_trash[1].field_state("like"), "unknown")
        self.assertEqual((record.completion, record.reason_code), ("partial", "rate_limited"))
        self.assertEqual(
            next(row for row in moved.active_records if row.record_id == other).items[0].work_id,
            "other",
        )

        restored = self.repo.restore_items(
            record_id,
            ["douyin:one"],
            expected_source_hash=moved.source_hash,
        )
        self.assertEqual(
            [row.stable_key for row in restored.selected_record.items],
            ["douyin:three", "douyin:one"],
        )
        purged = self.repo.purge_items(
            record_id,
            ["douyin:two"],
            expected_source_hash=restored.source_hash,
        )
        restarted = CollectionHistoryRepository(
            self.path, legacy_path=self.legacy_path
        ).load()
        self.assertEqual(purged.source_hash, restarted.source_hash)
        self.assertEqual(restarted.selected_record.item_trash, ())

    def test_move_last_item_keeps_record_truth_and_empty_after_purge_is_durable(self):
        before = self.add(
            [item("last")],
            completion="partial",
            reason_code="cancelled",
        )
        record_id = before.selected_record_id
        moved = self.repo.trash_items(record_id, ["douyin:last"])
        record = moved.selected_record
        self.assertEqual(record.items, ())
        self.assertEqual(len(record.item_trash), 1)
        self.assertFalse(record.confirmed_empty)
        self.assertEqual((record.completion, record.reason_code), ("partial", "cancelled"))

        purged = self.repo.purge_items(record_id, ["douyin:last"])
        self.assertEqual(purged.selected_record.items, ())
        self.assertEqual(purged.selected_record.item_trash, ())
        self.assertFalse(purged.selected_record.confirmed_empty)
        self.assertEqual(self.repo.load().selected_record_id, record_id)

    def test_item_operations_require_exact_keys_and_duplicate_restore_fails_whole(self):
        snapshot = self.add([item("one"), item("two")])
        record_id = snapshot.selected_record_id
        original = self.path.read_bytes()
        self.assert_error(
            "item_not_found",
            lambda: self.repo.trash_items(record_id, ["douyin:missing"]),
        )
        self.assertEqual(self.path.read_bytes(), original)

        moved = self.repo.trash_items(record_id, ["douyin:one"])
        unsafe = object.__new__(type(moved.selected_record))
        for field_name in moved.selected_record.__dataclass_fields__:
            object.__setattr__(
                unsafe,
                field_name,
                getattr(moved.selected_record, field_name),
            )
        object.__setattr__(
            unsafe,
            "items",
            moved.selected_record.items + (moved.selected_record.item_trash[0],),
        )
        injected = replace(moved, active_records=(unsafe,))
        before = self.path.read_bytes()
        with patch.object(self.repo, "load", return_value=injected):
            self.assert_error(
                "duplicate_item",
                lambda: self.repo.restore_items(record_id, ["douyin:one"]),
            )
        self.assertEqual(self.path.read_bytes(), before)


class HistoryReadOnlyAndRecoveryTests(HistoryCase):
    def test_future_schema_is_read_only_and_never_overwritten(self):
        source = self.write_json({
            "schema_version": 2,
            "selected_record_id": "",
            "active_records": [],
            "trashed_records": [],
            "legacy_import_receipts": [],
            "future_metadata": {"writer": "newer-lulu"},
        })
        snapshot = self.repo.load()
        self.assertFalse(snapshot.writable)
        self.assertEqual(snapshot.code, "future_schema_read_only")
        self.assert_error("future_schema_read_only", lambda: self.add([item("1")]))
        self.assert_error(
            "future_schema_read_only",
            lambda: self.repo.trash_items("missing", ["douyin:1"]),
        )
        self.assertEqual(self.path.read_bytes(), source)

    def test_corrupt_record_keeps_valid_rows_visible_but_disables_writes(self):
        valid = self.add([item("safe")]).selected_record.to_dict()
        source = self.write_json({
            "schema_version": 1,
            "selected_record_id": valid["record_id"],
            "active_records": [valid, {"record_id": "broken"}],
            "trashed_records": [],
            "legacy_import_receipts": [],
        })
        snapshot = self.repo.load()
        self.assertFalse(snapshot.writable)
        self.assertEqual(snapshot.code, "corrupt_repository")
        self.assertEqual([row.record_id for row in snapshot.active_records], [valid["record_id"]])
        self.assert_error("corrupt_repository", lambda: self.repo.rename(valid["record_id"], "x"))
        self.assertEqual(self.path.read_bytes(), source)

    def test_invalid_json_is_read_only_not_empty_writable(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"{broken")
        snapshot = self.repo.load()
        self.assertFalse(snapshot.writable)
        self.assertEqual(snapshot.code, "corrupt_repository")
        self.assert_error("corrupt_repository", lambda: self.add([item("x")]))

    def test_invalid_main_restores_valid_rollback(self):
        valid = self.add([item("safe")])
        good = self.path.read_bytes()
        self.repo.rollback_path.write_bytes(good)
        self.path.write_bytes(b"broken-main")
        loaded = self.repo.load()
        self.assertEqual(loaded.selected_record_id, valid.selected_record_id)
        self.assertEqual(self.path.read_bytes(), good)
        self.assertFalse(self.repo.rollback_path.exists())

    def test_two_invalid_files_preserve_scene(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"bad-main")
        self.repo.rollback_path.write_bytes(b"bad-rollback")
        self.assert_error("recovery_failed", self.repo.load)
        self.assertEqual(self.path.read_bytes(), b"bad-main")
        self.assertEqual(self.repo.rollback_path.read_bytes(), b"bad-rollback")


class HistoryAtomicFailureTests(HistoryCase):
    def assert_failure_preserves(self, patcher, code: str):
        self.add([item("safe")])
        original = self.path.read_bytes()
        with patcher:
            self.assert_error(code, lambda: self.add([item("new")]))
        self.assertEqual(self.path.read_bytes(), original)

    def test_disk_full_and_permission_fail_closed(self):
        self.assert_failure_preserves(
            patch.object(
                history_module,
                "_write_temp_bytes",
                side_effect=OSError(errno.ENOSPC, "full"),
            ),
            "disk_full",
        )
        self.assert_failure_preserves(
            patch.object(history_module, "_write_temp_bytes", side_effect=PermissionError()),
            "permission_denied",
        )

    def test_replace_failure_preserves_main(self):
        self.assert_failure_preserves(
            patch.object(history_module, "_replace_file", side_effect=OSError("replace")),
            "atomic_write_failed",
        )

    def test_post_replace_validation_failure_restores_main(self):
        self.add([item("safe")])
        original = self.path.read_bytes()
        real_validate = history_module._validate_history_bytes
        calls = 0

        def fail_post_replace(payload):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise HistoryError("post_write_validation_failed")
            return real_validate(payload)

        with patch.object(
            history_module, "_validate_history_bytes", side_effect=fail_post_replace
        ):
            self.assert_error("post_write_validation_failed", lambda: self.add([item("new")]))
        self.assertEqual(self.path.read_bytes(), original)

    def test_direct_and_ancestor_symlinks_are_rejected_without_write(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        direct = Path(self.temp.name) / "direct.json"
        direct.symlink_to(external / "outside.json")
        self.assert_error(
            "unsafe_path",
            lambda: CollectionHistoryRepository(direct, legacy_path=self.legacy_path),
        )

        managed = Path(self.temp.name) / "managed"
        managed.mkdir()
        link = managed / "linked"
        link.symlink_to(external, target_is_directory=True)
        target = external / "history.json"
        self.assert_error(
            "unsafe_path",
            lambda: CollectionHistoryRepository(
                link / "history.json", legacy_path=self.legacy_path
            ),
        )
        self.assertFalse(target.exists())

    def test_ancestor_swap_after_construction_is_rechecked(self):
        root = Path(self.temp.name) / "late"
        path = root / "parent" / "history.json"
        legacy = root / "legacy" / "snapshots.json"
        repo = CollectionHistoryRepository(path, legacy_path=legacy)
        external = Path(self.temp.name) / "external-late"
        external.mkdir()
        root.mkdir()
        (root / "parent").symlink_to(external, target_is_directory=True)
        self.assert_error(
            "unsafe_path",
            lambda: repo.add_record(
                [item("x")],
                platform="douyin",
                homepage_url="https://example.test/author",
                source_method="anonymous_latest",
                completion="complete",
            ),
        )
        self.assertFalse((external / "history.json").exists())

    def test_item_move_disk_permission_post_validation_and_stale_hash_preserve_all(self):
        snapshot = self.add([item("safe"), item("stay")])
        record_id = snapshot.selected_record_id
        original = self.path.read_bytes()
        for patcher, code in (
            (
                patch.object(
                    history_module,
                    "_write_temp_bytes",
                    side_effect=OSError(errno.ENOSPC, "full"),
                ),
                "disk_full",
            ),
            (
                patch.object(
                    history_module,
                    "_write_temp_bytes",
                    side_effect=PermissionError(),
                ),
                "permission_denied",
            ),
        ):
            with self.subTest(code=code), patcher:
                self.assert_error(
                    code,
                    lambda: self.repo.trash_items(record_id, ["douyin:safe"]),
                )
            self.assertEqual(self.path.read_bytes(), original)

        stale = snapshot.source_hash
        changed = self.repo.rename(record_id, "changed")
        changed_bytes = self.path.read_bytes()
        self.assert_error(
            "concurrent_change",
            lambda: self.repo.trash_items(
                record_id,
                ["douyin:safe"],
                expected_source_hash=stale,
            ),
        )
        self.assertEqual(self.path.read_bytes(), changed_bytes)
        self.assertEqual(changed.selected_record.items[1].stable_key, "douyin:stay")

    def test_item_restore_and_purge_write_failures_preserve_main_and_trash(self):
        snapshot = self.add([item("restore"), item("stay")])
        record_id = snapshot.selected_record_id
        moved = self.repo.trash_items(record_id, ["douyin:restore"])
        original = self.path.read_bytes()
        for operation in (
            lambda: self.repo.restore_items(record_id, ["douyin:restore"]),
            lambda: self.repo.purge_items(record_id, ["douyin:restore"]),
        ):
            with self.subTest(operation=operation), patch.object(
                history_module,
                "_write_temp_bytes",
                side_effect=OSError(errno.ENOSPC, "full"),
            ):
                self.assert_error("disk_full", operation)
            self.assertEqual(self.path.read_bytes(), original)
            loaded = self.repo.load()
            self.assertEqual(loaded.source_hash, moved.source_hash)
            self.assertEqual(
                [row.stable_key for row in loaded.selected_record.item_trash],
                ["douyin:restore"],
            )


class HistoryLegacyMigrationTests(HistoryCase):
    def legacy_document(self, *, bad=False):
        old_item = {
            "aweme_id": "7",
            "author_name": "旧作者",
            "title": "旧作品",
            "create_time": 1_700_000_000,
            "duration_ms": 1000,
            "like": 3,
            "collect": 2,
            "comment": 1,
            "share": 0,
            "play_url": "https://media.example/7.mp4",
            "source_url": "https://www.douyin.com/video/7",
            "aweme_type": 0,
            "author_fans": 999,
            "author_total_like": 888,
        }
        if bad:
            old_item["unknown_field"] = True
        record = {
            "id": "legacy-1",
            "name": "旧记录",
            "author_name": "旧作者",
            "created_at": "2026-07-15T10:00:00",
            "method": "authenticated",
            "items": [old_item],
            "deleted_items": [],
            "source_profile_url": "https://www.douyin.com/user/legacy",
            "partial": True,
            "author_fans": 999,
            "author_total_like": 888,
            "trashed_at": "",
        }
        return {
            "schema_version": 1,
            "selected_id": "legacy-1",
            "active": [record],
            "trash": [],
        }

    def write_legacy(self, document) -> bytes:
        self.legacy_path.parent.mkdir(parents=True)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
        self.legacy_path.write_bytes(payload)
        return payload

    def test_known_legacy_import_is_byte_backed_up_and_idempotent(self):
        source = self.write_legacy(self.legacy_document())
        first = self.repo.load()
        second = self.repo.load()
        record = first.selected_record

        self.assertEqual(self.repo.legacy_backup_path.read_bytes(), source)
        self.assertEqual(self.legacy_path.read_bytes(), source)
        self.assertEqual(len(first.active_records), 1)
        self.assertEqual(len(second.active_records), 1)
        self.assertEqual(record.platform, "douyin")
        self.assertEqual(record.completion, "partial")
        self.assertEqual(record.items[0].work_url, "https://www.douyin.com/video/7")
        self.assertEqual(record.items[0].stable_key, "douyin:7")
        data = record.items[0].to_dict()
        self.assertNotIn("author_fans", data)
        self.assertNotIn("author_total_like", data)
        receipt = first.legacy_import_receipts[0]
        self.assertEqual(receipt.source_hash, hashlib.sha256(source).hexdigest())
        self.assertEqual(receipt.imported_count, 1)

    def test_any_bad_legacy_record_fails_whole_import_without_history(self):
        source = self.write_legacy(self.legacy_document(bad=True))
        self.assert_error("migration_failed", self.repo.load)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.legacy_path.read_bytes(), source)
        self.assertFalse(self.repo.legacy_backup_path.exists())


if __name__ == "__main__":
    unittest.main()
