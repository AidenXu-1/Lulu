import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.core.collect.contracts import CollectResult
from src.core.collect.cover_workflow import CoverItemOutcome, CoverWorkflowResult
from src.core.collect.models import CollectedItem
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.collect.repository import (
    CollectionRepository,
    RepositoryError,
)
from src.ui.collection_page import CollectionPage, _MaterialDownloadWorker


def _item(work_id: str, **changes) -> CollectedItem:
    values = {
        "platform": "douyin",
        "work_id": work_id,
        "aweme_id": work_id,
        "title": f"作品 {work_id}",
        "cover_url": f"https://example.test/{work_id}.jpg",
        "cover_status": "url_only",
    }
    values.update(changes)
    return CollectedItem(**values)


class _ConflictRepository:
    def __init__(self, current: CollectedItem) -> None:
        self.current = current

    def compare_and_swap_fields(self, *_args, **_kwargs):
        raise RepositoryError("field_conflict")

    def load(self):
        return SimpleNamespace(items=(self.current,))


class _DownloadedCoverWorkflow:
    def __init__(self, after: CollectedItem) -> None:
        self.after = after

    def run(self, result, **_kwargs):
        return CoverWorkflowResult(
            replace(result, items=(self.after,)),
            (
                CoverItemOutcome(
                    self.after.stable_key,
                    "downloaded",
                    relative_path=self.after.cover_local_path,
                ),
            ),
        )


class UserFeedbackStateSettlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_material_cover_worker_turns_uncommitted_file_success_into_failure(self):
        before = _item("1")
        after = replace(
            before,
            cover_local_path="douyin/1.jpg",
            cover_status="downloaded",
        )
        worker = _MaterialDownloadWorker(
            _ConflictRepository(before),
            _DownloadedCoverWorkflow(after),
            [before],
            MaterialPreferenceSnapshot("video", save_cover=True),
            origin="homepage_collect",
        )
        completed = []
        worker.done.connect(completed.append)

        worker.run()

        self.assertEqual(len(completed), 1)
        items, _media, cover, _cancelled = completed[0]
        self.assertEqual(items[0].cover_status, "url_only")
        self.assertEqual(cover.outcomes[0].status, "failed")
        self.assertEqual(
            cover.outcomes[0].reason_code,
            "cover_state_commit_failed",
        )

    def test_material_cover_worker_accepts_same_state_committed_by_other_writer(self):
        before = _item("2")
        after = replace(
            before,
            cover_local_path="douyin/2.jpg",
            cover_status="downloaded",
        )
        worker = _MaterialDownloadWorker(
            _ConflictRepository(after),
            _DownloadedCoverWorkflow(after),
            [before],
            MaterialPreferenceSnapshot("video", save_cover=True),
            origin="homepage_collect",
        )
        completed = []
        worker.done.connect(completed.append)

        worker.run()

        items, _media, cover, _cancelled = completed[0]
        self.assertEqual(items[0].cover_status, "downloaded")
        self.assertEqual(cover.outcomes[0].status, "downloaded")

    def test_busy_material_worker_merges_same_contract_instead_of_dropping(self):
        page = SimpleNamespace()
        page._material_worker = object()
        page._pending_material_jobs = []
        messages = []
        page.status = SimpleNamespace(setText=messages.append)
        snapshot = MaterialPreferenceSnapshot("video", save_cover=True)

        CollectionPage._enqueue_material_job(
            page, [_item("1")], snapshot, origin="homepage_collect"
        )
        CollectionPage._enqueue_material_job(
            page,
            [_item("1", title="较新事实"), _item("2")],
            snapshot,
            origin="homepage_collect",
        )

        self.assertEqual(len(page._pending_material_jobs), 1)
        waiting, waiting_snapshot, origin = page._pending_material_jobs[0]
        self.assertEqual([item.work_id for item in waiting], ["1", "2"])
        self.assertEqual(waiting[0].title, "较新事实")
        self.assertEqual(waiting_snapshot, snapshot)
        self.assertEqual(origin, "homepage_collect")
        self.assertIn("已排队 2 项", messages[-1])

    def test_finished_material_worker_starts_next_queued_batch(self):
        deleted = []
        worker = SimpleNamespace(deleteLater=lambda: deleted.append(True))
        snapshot = MaterialPreferenceSnapshot("video", save_cover=True)
        waiting = (_item("4"),)
        started = []
        page = SimpleNamespace(
            _material_worker=worker,
            _pending_material_jobs=[(waiting, snapshot, "homepage_collect")],
            _shutting_down=False,
            _start_material_job=lambda items, frozen, *, origin: started.append(
                (items, frozen, origin)
            ),
            material_download_btn=SimpleNamespace(setText=lambda _text: None),
            _update_tools_enabled=lambda: None,
        )

        CollectionPage._on_material_download_finished(page, worker)

        self.assertEqual(page._material_worker, None)
        self.assertEqual(page._pending_material_jobs, [])
        self.assertEqual(started, [(waiting, snapshot, "homepage_collect")])
        self.assertEqual(deleted, [True])

    def test_old_transient_records_error_does_not_lock_future_collect(self):
        with tempfile.TemporaryDirectory() as raw:
            repository = CollectionRepository(Path(raw) / "records.json")
            page = SimpleNamespace()
            page._records_repo = repository
            page._records_snapshot = None
            page._records_error_code = "concurrent_change"

            warning, saved = CollectionPage._persist_records_result(
                page,
                CollectResult.complete([_item("3")])
            )

            self.assertTrue(saved)
            self.assertEqual(warning, "")
            self.assertEqual(page._records_error_code, "")
            self.assertEqual(repository.load().items[0].work_id, "3")


if __name__ == "__main__":
    unittest.main()
