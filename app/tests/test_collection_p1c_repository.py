"""P1C schema v2 仓库的纯本地数据安全测试。"""

from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.models import CollectedItem  # noqa: E402
from core.collect import repository as repository_module  # noqa: E402
from core.collect.repository import (  # noqa: E402
    CollectionRepository,
    EMPTY_SOURCE_HASH,
    RepositoryError,
)


def _item(work_id: str, *, title: str = "", cover_status: str = "not_requested") -> CollectedItem:
    return CollectedItem(work_id, title=title, cover_status=cover_status)


def _cross_process_field_cas(
    path_value: str,
    expected: dict[str, object],
    updates: dict[str, object],
    start_event,
    result_path: str = "",
) -> None:
    repo = CollectionRepository(Path(path_value))
    real_store = repo._atomic_store

    def slow_store(payload, previous):
        time.sleep(0.08)
        return real_store(payload, previous)

    repo._atomic_store = slow_store  # type: ignore[method-assign]
    if not start_event.wait(5):
        raise RuntimeError("start timeout")
    try:
        repo.compare_and_swap_fields(
            "douyin:1",
            expected=expected,
            updates=updates,
        )
        result = "ok"
    except RepositoryError as exc:
        result = exc.code
    if result_path:
        Path(result_path).write_text(result, encoding="utf-8")
    elif result != "ok":
        raise RuntimeError(result)


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "collection" / "records.json"
        self.repo = CollectionRepository(self.path)

    def _write_json(self, document: object) -> bytes:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(payload)
        return payload

    def _seed_v2(self, *items: CollectedItem) -> bytes:
        self.repo.upsert(items)
        return self.path.read_bytes()

    def assert_repository_error(self, code: str, action) -> RepositoryError:
        with self.assertRaises(RepositoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class TestRepositoryReadWrite(RepositoryCase):
    def test_missing_file_is_empty_writable_and_not_created(self):
        snapshot = self.repo.load()
        self.assertEqual(snapshot.items, ())
        self.assertTrue(snapshot.writable)
        self.assertEqual(snapshot.code, "empty")
        self.assertEqual(snapshot.source_hash, EMPTY_SOURCE_HASH)
        self.assertFalse(self.path.exists())

    def test_utf8_round_trip_is_deterministic(self):
        first = _item("1", title="兆基的中文标题", cover_status="url_only")
        receipt = self.repo.upsert([first])
        initial = self.path.read_bytes()
        snapshot = self.repo.load()
        second = self.repo.upsert([first], expected_source_hash=snapshot.source_hash)
        self.assertEqual(snapshot.items[0].title, "兆基的中文标题")
        self.assertEqual(snapshot.items[0].cover_status, "url_only")
        self.assertEqual(initial, self.path.read_bytes())
        self.assertEqual(receipt.current_hash, hashlib.sha256(initial).hexdigest())
        self.assertEqual(second.previous_hash, second.current_hash)

    def test_upsert_replaces_in_place_and_appends_in_input_order(self):
        self.repo.upsert([_item("1", title="old"), _item("2")])
        receipt = self.repo.upsert([_item("1", title="new"), _item("3"), _item("4")])
        snapshot = self.repo.load()
        self.assertEqual([item.work_id for item in snapshot.items], ["1", "2", "3", "4"])
        self.assertEqual(snapshot.items[0].title, "new")
        self.assertEqual(receipt.record_count, 4)

    def test_collected_upsert_updates_platform_truth_without_regressing_settled_fields(self):
        current = CollectedItem(
            platform="douyin",
            work_id="1",
            aweme_id="1",
            title="旧标题",
            cover_url="https://old.example/cover.jpg",
            cover_local_path="douyin/1.jpg",
            cover_status="downloaded",
            media_local_path="douyin/1.mp4",
            media_status="downloaded",
            transcript_status="completed",
            feishu_video_status="uploaded",
            feishu_cover_status="uploaded",
            last_error_code="cover:cover_commit_uncertain",
        )
        self.repo.upsert([current])
        recollected = CollectedItem(
            platform="douyin",
            work_id="1",
            aweme_id="1",
            title="新标题",
            like=99,
            cover_url="https://new.example/cover.jpg",
            cover_status="url_only",
            media_status="not_requested",
            transcript_status="not_started",
            last_error_code="",
        )

        self.repo.upsert_collected([recollected])

        saved = self.repo.load().items[0]
        self.assertEqual(saved.title, "新标题")
        self.assertEqual(saved.like, 99)
        self.assertEqual(saved.cover_url, "https://new.example/cover.jpg")
        self.assertEqual(saved.cover_local_path, "douyin/1.jpg")
        self.assertEqual(saved.cover_status, "downloaded")
        self.assertEqual(saved.media_local_path, "douyin/1.mp4")
        self.assertEqual(saved.media_status, "downloaded")
        self.assertEqual(saved.transcript_status, "completed")
        self.assertEqual(saved.feishu_video_status, "uploaded")
        self.assertEqual(saved.feishu_cover_status, "uploaded")
        self.assertEqual(saved.last_error_code, "cover:cover_commit_uncertain")

    def test_duplicate_or_empty_input_is_all_or_nothing(self):
        original = self._seed_v2(_item("1"))
        self.assert_repository_error(
            "invalid_input", lambda: self.repo.upsert([_item("2"), _item("2")])
        )
        self.assert_repository_error(
            "invalid_input", lambda: self.repo.upsert([CollectedItem("")])
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_only_canonical_cover_states_are_persisted(self):
        for status in ("not_requested", "url_only", "downloaded", "failed"):
            self.repo.upsert([_item(status, cover_status=status)])
        original = self.path.read_bytes()
        self.assert_repository_error(
            "invalid_cover_status",
            lambda: self.repo.upsert([_item("pending", cover_status="pending")]),
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_expected_hash_rejects_stale_writer(self):
        first = CollectionRepository(self.path)
        second = CollectionRepository(self.path)
        stale = first.load().source_hash
        second.upsert([_item("2")])
        changed = self.path.read_bytes()
        self.assert_repository_error(
            "concurrent_change",
            lambda: first.upsert([_item("1")], expected_source_hash=stale),
        )
        self.assertEqual(self.path.read_bytes(), changed)

    def test_field_cas_merges_over_unrelated_change_and_rejects_owned_drift(self):
        original = _item("1", title="old")
        self.repo.upsert([original])
        newer = CollectedItem(**{
            **original.to_dict(),
            "title": "newer",
            "transcript_status": "completed",
        })
        self.repo.upsert([newer])

        self.repo.compare_and_swap_fields(
            original.stable_key,
            expected={"media_local_path": "", "media_status": "not_requested"},
            updates={"media_local_path": "douyin/1.m4a", "media_status": "downloaded"},
        )
        saved = self.repo.load().items[0]
        self.assertEqual(saved.title, "newer")
        self.assertEqual(saved.transcript_status, "completed")
        self.assertEqual(saved.media_status, "downloaded")
        self.assert_repository_error(
            "field_conflict",
            lambda: self.repo.compare_and_swap_fields(
                original.stable_key,
                expected={"media_status": "not_requested"},
                updates={"media_status": "failed"},
            ),
        )

    def test_field_cas_remerges_change_injected_after_hash_check(self):
        original = CollectedItem("1", like=1, title="old")
        self.repo.upsert([original])
        real_assert = self.repo._assert_current_hash
        injected = False

        def assert_then_inject(expected):
            nonlocal injected
            real_assert(expected)
            if injected:
                return
            injected = True
            document = json.loads(self.path.read_text(encoding="utf-8"))
            document["records"][0]["like"] = 999
            document["records"][0]["title"] = "newer"
            self.path.write_text(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    separators=(",", ": "),
                ) + "\n",
                encoding="utf-8",
            )

        with patch.object(
            self.repo,
            "_assert_current_hash",
            side_effect=assert_then_inject,
        ):
            self.repo.compare_and_swap_fields(
                original.stable_key,
                expected={
                    "media_local_path": "",
                    "media_status": "not_requested",
                },
                updates={
                    "media_local_path": "douyin/safe.m4a",
                    "media_status": "downloaded",
                },
            )

        saved = self.repo.load().items[0]
        self.assertEqual(saved.like, 999)
        self.assertEqual(saved.title, "newer")
        self.assertEqual(saved.media_status, "downloaded")

    def test_cross_process_different_field_writers_preserve_both_updates(self):
        original = CollectedItem("1", like=7)
        self.repo.upsert([original])
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        media = context.Process(
            target=_cross_process_field_cas,
            args=(
                str(self.path),
                {"media_local_path": "", "media_status": "not_requested"},
                {
                    "media_local_path": "douyin/process-safe.m4a",
                    "media_status": "downloaded",
                },
                start_event,
            ),
        )
        transcript = context.Process(
            target=_cross_process_field_cas,
            args=(
                str(self.path),
                {"transcript_status": "not_started"},
                {"transcript_status": "completed"},
                start_event,
            ),
        )
        media.start()
        transcript.start()
        start_event.set()
        media.join(10)
        transcript.join(10)
        if media.is_alive():
            media.terminate()
            media.join(2)
        if transcript.is_alive():
            transcript.terminate()
            transcript.join(2)

        self.assertEqual(media.exitcode, 0)
        self.assertEqual(transcript.exitcode, 0)
        saved = self.repo.load().items[0]
        self.assertEqual(saved.like, 7)
        self.assertEqual(saved.media_status, "downloaded")
        self.assertEqual(saved.transcript_status, "completed")

    def test_cross_process_same_field_writers_yield_one_conflict(self):
        original = CollectedItem("1", like=11)
        self.repo.upsert([original])
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_a = Path(self.temp.name) / "writer-a.txt"
        result_b = Path(self.temp.name) / "writer-b.txt"
        common = {"media_local_path": "", "media_status": "not_requested"}
        writer_a = context.Process(
            target=_cross_process_field_cas,
            args=(
                str(self.path),
                common,
                {
                    "media_local_path": "douyin/a.m4a",
                    "media_status": "downloaded",
                },
                start_event,
                str(result_a),
            ),
        )
        writer_b = context.Process(
            target=_cross_process_field_cas,
            args=(
                str(self.path),
                common,
                {
                    "media_local_path": "douyin/b.m4a",
                    "media_status": "downloaded",
                },
                start_event,
                str(result_b),
            ),
        )
        writer_a.start()
        writer_b.start()
        start_event.set()
        writer_a.join(10)
        writer_b.join(10)
        for process in (writer_a, writer_b):
            if process.is_alive():
                process.terminate()
                process.join(2)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(
            sorted((
                result_a.read_text(encoding="utf-8"),
                result_b.read_text(encoding="utf-8"),
            )),
            ["field_conflict", "ok"],
        )
        saved = self.repo.load().items[0]
        self.assertEqual(saved.like, 11)
        self.assertIn(saved.media_local_path, {"douyin/a.m4a", "douyin/b.m4a"})

    def test_field_cas_rejects_cross_domain_and_non_cover_shared_error(self):
        original = _item("1")
        self.repo.upsert([original])
        self.assert_repository_error(
            "invalid_field_cas",
            lambda: self.repo.compare_and_swap_fields(
                original.stable_key,
                expected={"media_status": "not_requested", "cover_status": "not_requested"},
                updates={"media_status": "failed", "cover_status": "failed"},
            ),
        )
        self.assert_repository_error(
            "field_conflict",
            lambda: self.repo.compare_and_swap_fields(
                original.stable_key,
                expected={"cover_status": "not_requested", "last_error_code": ""},
                updates={"cover_status": "failed", "last_error_code": "network_error"},
            ),
        )

    def test_xhs_field_states_round_trip_without_schema_change(self):
        legacy = CollectedItem(
            "",
            platform="xiaohongshu",
            work_id="legacy-note",
            like=99,
        )
        marked = CollectedItem(
            "",
            platform="xiaohongshu",
            work_id="marked-note",
            like=0,
            extra_stats={
                "_field_state_create_time": "unknown",
                "_field_state_duration_ms": "unknown",
                "_field_state_like": "known",
                "_field_state_collect": "unknown",
                "_field_state_comment": "unknown",
                "_field_state_share": "unknown",
            },
        )

        self.repo.upsert([legacy, marked])
        document = json.loads(self.path.read_text(encoding="utf-8"))
        snapshot = self.repo.load()

        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(snapshot.items[0].field_state("like"), "unknown")
        self.assertIsNone(snapshot.items[0].known_numeric("like"))
        self.assertEqual(snapshot.items[1].field_state("like"), "known")
        self.assertEqual(snapshot.items[1].known_numeric("like"), 0)


class TestRepositoryMigration(RepositoryCase):
    def test_v1_bare_list_is_byte_exact_backed_up_then_migrated(self):
        source = self._write_json([{"aweme_id": "1", "title": "旧标题"}])
        snapshot = self.repo.load()
        self.assertEqual(snapshot.code, "migrated_v1")
        self.assertEqual(snapshot.items[0].stable_key, "douyin:1")
        self.assertEqual(self.repo.backup_path.read_bytes(), source)
        self.assertTrue(snapshot.migration_receipt.backup_created)
        self.assertEqual(snapshot.migration_receipt.source_hash, hashlib.sha256(source).hexdigest())

    def test_v1_envelope_migrates_and_same_backup_is_reused(self):
        source = self._write_json({"schema_version": 1, "records": [{"aweme_id": "7"}]})
        self.repo.backup_path.write_bytes(source)
        snapshot = self.repo.load()
        self.assertFalse(snapshot.migration_receipt.backup_created)
        self.assertEqual(snapshot.items[0].work_id, "7")

    def test_v1_backup_conflict_preserves_everything(self):
        source = self._write_json([{"aweme_id": "1"}])
        self.repo.backup_path.write_bytes(b"different-old-backup")
        self.assert_repository_error("backup_conflict", self.repo.load)
        self.assertEqual(self.path.read_bytes(), source)
        self.assertEqual(self.repo.backup_path.read_bytes(), b"different-old-backup")

    def test_invalid_v1_is_backed_up_but_source_is_never_replaced(self):
        cases = [
            [{"aweme_id": ""}],
            [{"aweme_id": "1"}, {"aweme_id": "1"}],
            [{"aweme_id": "1", "unknown": True}],
            ["not-a-record"],
        ]
        for index, records in enumerate(cases):
            with self.subTest(index=index):
                path = Path(self.temp.name) / str(index) / "records.json"
                repo = CollectionRepository(path)
                path.parent.mkdir(parents=True)
                source = json.dumps(records).encode()
                path.write_bytes(source)
                self.assert_repository_error("migration_failed", repo.load)
                self.assertEqual(path.read_bytes(), source)
                self.assertEqual(repo.backup_path.read_bytes(), source)

    def test_future_schema_is_metadata_only_read_only(self):
        source = self._write_json({
            "schema_version": 3,
            "records": [{"schema_version": 3, "work_id": "future", "secret": "x"}],
            "future_metadata": {"writer": "newer-lulu"},
        })
        snapshot = self.repo.load()
        self.assertFalse(snapshot.writable)
        self.assertEqual(snapshot.code, "future_schema_read_only")
        self.assertEqual(snapshot.record_count, 1)
        self.assertEqual(snapshot.items, ())
        self.assert_repository_error("future_schema_read_only", lambda: self.repo.upsert([_item("1")]))
        self.assertEqual(self.path.read_bytes(), source)
        self.assertFalse(self.repo.backup_path.exists())

    def test_corrupt_documents_are_not_treated_as_empty_or_overwritten(self):
        cases = [b"{bad", b"[] trailing", b'[]{"x":1}', b'{"schema_version":2}']
        for index, source in enumerate(cases):
            with self.subTest(index=index):
                path = Path(self.temp.name) / f"corrupt-{index}" / "records.json"
                repo = CollectionRepository(path)
                path.parent.mkdir(parents=True)
                path.write_bytes(source)
                self.assert_repository_error("corrupt_repository", repo.load)
                self.assertEqual(path.read_bytes(), source)

    def test_invalid_v2_record_is_corrupt_and_preserved(self):
        source = self._write_json({
            "schema_version": 2,
            "records": [{"schema_version": 2, "aweme_id": "1", "work_id": "1",
                         "cover_status": "pending"}],
        })
        self.assert_repository_error("corrupt_repository", self.repo.load)
        self.assertEqual(self.path.read_bytes(), source)

    def test_v2_envelope_cannot_smuggle_v1_record(self):
        source = self._write_json({
            "schema_version": 2,
            "records": [{"aweme_id": "1"}],
        })
        self.assert_repository_error("corrupt_repository", self.repo.load)
        self.assertEqual(self.path.read_bytes(), source)


class TestRepositoryAtomicFailures(RepositoryCase):
    def _assert_write_failure_preserves(self, patcher, code: str):
        original = self._seed_v2(_item("1", title="safe"))
        with patcher:
            self.assert_repository_error(code, lambda: self.repo.upsert([_item("2")]))
        self.assertEqual(self.path.read_bytes(), original)

    def test_enospc_preserves_main_file(self):
        self._assert_write_failure_preserves(
            patch.object(
                repository_module,
                "_write_temp_bytes",
                side_effect=OSError(errno.ENOSPC, "full"),
            ),
            "disk_full",
        )

    def test_permission_failure_preserves_main_file(self):
        self._assert_write_failure_preserves(
            patch.object(repository_module, "_write_temp_bytes", side_effect=PermissionError()),
            "permission_denied",
        )

    def test_fsync_failure_preserves_main_file(self):
        self._assert_write_failure_preserves(
            patch.object(repository_module, "_fsync_file", side_effect=OSError("fsync")),
            "atomic_write_failed",
        )

    def test_replace_failure_preserves_main_file(self):
        self._assert_write_failure_preserves(
            patch.object(repository_module, "_replace_file", side_effect=OSError("replace")),
            "atomic_write_failed",
        )

    def test_pre_replace_validation_failure_preserves_main_file(self):
        self._assert_write_failure_preserves(
            patch.object(
                repository_module,
                "_validate_v2_bytes",
                side_effect=RepositoryError("post_write_validation_failed"),
            ),
            "post_write_validation_failed",
        )

    def test_post_replace_validation_failure_restores_main_file(self):
        original = self._seed_v2(_item("1", title="safe"))
        real_validate = repository_module._validate_v2_bytes
        calls = 0

        def fail_second(payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RepositoryError("post_write_validation_failed")
            return real_validate(payload)

        with patch.object(repository_module, "_validate_v2_bytes", side_effect=fail_second):
            self.assert_repository_error(
                "post_write_validation_failed", lambda: self.repo.upsert([_item("2")])
            )
        self.assertEqual(self.path.read_bytes(), original)


class TestRepositoryCrashRecovery(RepositoryCase):
    def test_valid_main_wins_and_cleans_orphan_temp_and_rollback(self):
        current = self._seed_v2(_item("current"))
        self.repo.rollback_path.write_bytes(b"damaged rollback")
        orphan = self.path.parent / f"{self.repo._temp_prefix}orphan"
        orphan.write_bytes(b"uncommitted")
        snapshot = self.repo.load()
        self.assertEqual(snapshot.items[0].work_id, "current")
        self.assertEqual(self.path.read_bytes(), current)
        self.assertFalse(self.repo.rollback_path.exists())
        self.assertFalse(orphan.exists())

    def test_invalid_main_is_restored_from_valid_rollback(self):
        valid = self._seed_v2(_item("safe"))
        self.repo.rollback_path.write_bytes(valid)
        self.path.write_bytes(b"damaged")
        snapshot = self.repo.load()
        self.assertEqual(snapshot.items[0].work_id, "safe")
        self.assertEqual(self.path.read_bytes(), valid)
        self.assertFalse(self.repo.rollback_path.exists())

    def test_two_invalid_files_preserve_crash_scene(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"bad-main")
        self.repo.rollback_path.write_bytes(b"bad-rollback")
        self.assert_repository_error("recovery_failed", self.repo.load)
        self.assertEqual(self.path.read_bytes(), b"bad-main")
        self.assertEqual(self.repo.rollback_path.read_bytes(), b"bad-rollback")

    def test_orphan_temp_without_main_is_discarded_not_promoted(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        orphan = self.path.parent / f"{self.repo._temp_prefix}orphan"
        orphan.write_bytes(b"uncommitted")
        snapshot = self.repo.load()
        self.assertEqual(snapshot.code, "empty")
        self.assertFalse(orphan.exists())
        self.assertFalse(self.path.exists())

    def test_symlink_and_directory_targets_are_rejected(self):
        target = Path(self.temp.name) / "target"
        target.mkdir()
        link = Path(self.temp.name) / "link"
        link.symlink_to(target, target_is_directory=True)
        self.assert_repository_error(
            "unsafe_path", lambda: CollectionRepository(link / "records.json")
        )
        directory_target = Path(self.temp.name) / "directory-as-file"
        directory_target.mkdir()
        repo = CollectionRepository(directory_target)
        self.assert_repository_error("read_failed", repo.load)

    def test_existing_plain_child_below_symlink_ancestor_is_rejected_without_write(self):
        managed = Path(self.temp.name) / "managed"
        external = Path(self.temp.name) / "external"
        managed.mkdir()
        (external / "already-exists").mkdir(parents=True)
        sentinel = external / "already-exists" / "sentinel.txt"
        sentinel.write_bytes(b"unchanged")
        before_names = sorted(path.name for path in (external / "already-exists").iterdir())
        (managed / "nested-link").symlink_to(external, target_is_directory=True)
        external_record = external / "already-exists" / "records.json"

        self.assert_repository_error(
            "unsafe_path",
            lambda: CollectionRepository(
                managed / "nested-link" / "already-exists" / "records.json"
            ),
        )
        self.assertFalse(external_record.exists())
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(
            sorted(path.name for path in (external / "already-exists").iterdir()),
            before_names,
        )

    def test_late_symlink_ancestor_swap_is_rechecked_before_write(self):
        managed = Path(self.temp.name) / "managed-late"
        external = Path(self.temp.name) / "external-late"
        managed.mkdir()
        path = managed / "late-parent" / "late-child" / "records.json"
        repo = CollectionRepository(path)

        (external / "late-child").mkdir(parents=True)
        sentinel = external / "late-child" / "sentinel.txt"
        sentinel.write_bytes(b"unchanged")
        before_names = sorted(path.name for path in (external / "late-child").iterdir())
        (managed / "late-parent").symlink_to(external, target_is_directory=True)
        external_record = external / "late-child" / "records.json"

        self.assert_repository_error(
            "unsafe_path", lambda: repo.upsert([_item("must-not-write")])
        )
        self.assertFalse(external_record.exists())
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(
            sorted(path.name for path in (external / "late-child").iterdir()),
            before_names,
        )

    def test_normal_nested_path_without_symlink_still_writes(self):
        path = Path(self.temp.name) / "normal-nested" / "one" / "two" / "records.json"
        repo = CollectionRepository(path)
        receipt = repo.upsert([_item("safe")])
        self.assertTrue(receipt.success)
        self.assertEqual(repo.load().items[0].stable_key, "douyin:safe")
        self.assertTrue(path.is_file())

    def test_symlink_introduced_after_construction_and_sidecars_are_rejected(self):
        late_path = Path(self.temp.name) / "late" / "records.json"
        late_repo = CollectionRepository(late_path)
        external = Path(self.temp.name) / "external.json"
        external.write_bytes(b"external")
        late_path.parent.mkdir(parents=True)
        late_path.symlink_to(external)
        self.assert_repository_error("unsafe_path", late_repo.load)
        self.assertEqual(external.read_bytes(), b"external")

        normal = Path(self.temp.name) / "normal" / "records.json"
        sidecar_repo = CollectionRepository(normal)
        normal.parent.mkdir(parents=True)
        sidecar_repo.backup_path.symlink_to(external)
        self.assert_repository_error("unsafe_path", sidecar_repo.load)
        self.assertEqual(external.read_bytes(), b"external")

    def test_relative_and_root_paths_are_rejected(self):
        self.assert_repository_error("unsafe_path", lambda: CollectionRepository("records.json"))
        self.assert_repository_error("unsafe_path", lambda: CollectionRepository(Path("/")))


if __name__ == "__main__":
    unittest.main()
