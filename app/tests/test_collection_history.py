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


































class HistoryAtomicFailureTests(HistoryCase):
    def assert_failure_preserves(self, patcher, code: str):
        self.add([item("safe")])
        original = self.path.read_bytes()
        with patcher:
            self.assert_error(code, lambda: self.add([item("new")]))
        self.assertEqual(self.path.read_bytes(), original)









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




class HistorySystemTrashTests(HistoryCase):
    def test_failed_put_back_registration_keeps_history_readable_and_retries(self):
        for phase in ('store','receipt','fsync'):
            with self.subTest(phase=phase):
                initial=self.add([item(phase)])
                key=initial.selected_record_id
                received={}
                def move(path):
                    target=Path(self.temp.name)/path.name
                    path.rename(target);received.update(original=path,trashed=target)
                self.repo.move_records_to_system_trash([key],expected_source_hash=initial.source_hash,move=move)
                received['trashed'].rename(received['original'])
                if phase=='store':
                    injection=patch.object(self.repo,'_store',side_effect=HistoryError('disk_full'))
                elif phase=='receipt':
                    unlink=history_module._unlink
                    def reject_receipt(path):
                        if path.suffix=='.sha256': raise PermissionError('fixture')
                        return unlink(path)
                    injection=patch.object(history_module,'_unlink',side_effect=reject_receipt)
                else:
                    sync=history_module._fsync_directory
                    def reject_sync(path):
                        if path==self.repo.returned_packages: raise OSError('fixture')
                        return sync(path)
                    injection=patch.object(history_module,'_fsync_directory',side_effect=reject_sync)
                with injection:
                    snapshot=self.repo.load()
                self.assertFalse(snapshot.writable)
                self.assertEqual(snapshot.code,'restore_pending')
                self.assertTrue(received['original'].is_file())
                restored=self.repo.load()
                self.assertTrue(restored.writable)
                self.assertIn(key,[row.record_id for row in restored.active_records])

    def test_batch_move_and_finder_put_back_preserve_legacy_and_media(self):
        legacy = self.add([item("old")])
        self.repo.trash_records([legacy.selected_record_id], expected_source_hash=legacy.source_hash)
        first = self.add([item("one")]).selected_record
        second = self.add([item("two")]).selected_record
        current = self.add([item("keep")])
        media = self.path.parent / "saved.mp4"
        media.write_bytes(b"existing media")
        received = {}

        def move(path):
            target = Path(self.temp.name) / path.name
            path.rename(target)
            received.update(original=path, trashed=target)

        moved = self.repo.move_records_to_system_trash(
            [first.record_id, second.record_id], expected_source_hash=current.source_hash, move=move,
        )
        self.assertEqual(moved.active_records, (current.selected_record,))
        self.assertEqual(moved.trashed_records, current.trashed_records)
        self.assertEqual(media.read_bytes(), b"existing media")
        received["trashed"].rename(received["original"])
        reopened = CollectionHistoryRepository(self.path, legacy_path=self.legacy_path)
        restored = reopened.load()
        self.assertEqual(set(row.record_id for row in restored.active_records),
                         {first.record_id, second.record_id, current.selected_record_id})
        self.assertEqual(next(row for row in restored.active_records if row.record_id == first.record_id), first)
        self.assertEqual(restored.source_hash, reopened.load().source_hash)
        moved_again = reopened.move_records_to_system_trash(
            [first.record_id], expected_source_hash=restored.source_hash, move=move,
        )
        self.assertEqual(moved_again, reopened.load())

    def test_rejected_and_concurrent_moves_preserve_the_current_index(self):
        snapshot = self.add()
        key = snapshot.selected_record_id
        before = self.path.read_bytes()

        def reject(_):
            raise PermissionError("fixture")

        self.assert_error("permission_denied", lambda: self.repo.move_records_to_system_trash(
            [key], expected_source_hash=snapshot.source_hash, move=reject,
        ))
        self.assertEqual(before, self.path.read_bytes())

        def external_rename(path):
            self.repo.rename(key, "保留新名称", expected_source_hash=snapshot.source_hash)
            path.rename(Path(self.temp.name) / path.name)

        self.assert_error("concurrent_change", lambda: self.repo.move_records_to_system_trash(
            [key], expected_source_hash=snapshot.source_hash, move=external_rename,
        ))
        self.assertEqual(self.repo.load().selected_record.name, "保留新名称")

if __name__ == "__main__":
    unittest.main()
