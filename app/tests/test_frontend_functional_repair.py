"""Regression cases from the September frontend investigations; isolated data only."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core import library
from src.core.collect.models import CollectedItem
from src.core.models import Segment, Transcript
from src.core.task import TaskGroupCandidate, TaskState
from src.ui.main_window import MainWindow


class FrontendFunctionalRepairTests(unittest.TestCase):
    def wait_library(self, page):
        from PySide6.QtCore import QEventLoop, QTimer
        for _ in range(500):
            if not (page._query_worker or page._batch_worker or page._pending_refresh):
                return
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        self.fail("library work did not finish")
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(patch.object(library, "LIBRARY_DIR", self.root / "library"))
        self.stack.enter_context(patch.object(library, "FOLDERS_FILE", self.root / "library/folders.json"))
        self.stack.enter_context(patch.object(AppConfig, "load", return_value=AppConfig()))
        self.stack.enter_context(patch.object(AppConfig, "save", return_value=True))
        self.stack.enter_context(patch("src.core.models_manager.app_data_dir", return_value=self.root / "data"))
        self.stack.enter_context(patch("src.core.models_manager.is_ready", return_value=False))
        for name in ("_prepare_library", "_sync_runtime_preferences", "_notify_task_completed", "_notify_task_failed"):
            self.stack.enter_context(patch.object(MainWindow, name))
        self.window = MainWindow(task_queue_store=None)
        self.window._switch_page = lambda _index: None
        self.window._toast = lambda _message: None

    def tearDown(self):
        if self.window.current:
            self.window.current[1].request_cancel()
            self.window.current[1].wait(3000)
        self.window._runtime_preferences_timer.stop()
        self.app.processEvents()
        self.window.extraction_workspace.shutdown()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.stack.close()

    def group(self, size):
        self.window._collect_transcribe_group([
            TaskGroupCandidate(f"douyin:{i}", f"合成作品 {i}", f"https://example.invalid/{i}")
            for i in range(size)
        ], "隔离批次")
        return self.window.task_groups[-1]

    def wait_idle(self):
        deadline = time.monotonic() + 15
        while self.window.current is not None and time.monotonic() < deadline:
            QTest.qWait(2)
        self.assertIsNone(self.window.current)


    def test_batch_export_preserves_same_titles_and_existing_file(self):
        from src.ui import library_page
        ids = [library.save_document(text, "同名") for text in ("第一份正文", "第二份正文")]
        out = self.root / "export"
        out.mkdir()
        (out / "同名.txt").write_text("原有文件", encoding="utf-8")
        page = library_page.LibraryPage(self.window.cfg)
        page.refresh()
        self.wait_library(page)
        page._sel_entries = set(ids)
        with patch.object(library_page.QFileDialog, "getExistingDirectory", return_value=str(out)), patch.object(library_page.dialogs, "alert"):
            page._batch_export()
            self.wait_library(page)
        self.assertEqual({p.read_text().strip() for p in out.iterdir()}, {"原有文件", "第一份正文", "第二份正文"})
        page.deleteLater()

    def test_edited_timestamps_never_export_old_words(self):
        from src.core.output.writer import render
        from src.core.collect.export import format_transcript_for_export
        tr = Transcript(text="识别的旧句子", segments=[Segment(text="识别的旧句子", start=0, end=1)])
        eid = library.save(tr, "改稿")
        library.save_markdown(eid, "用户修订后的新句子")
        loaded = library.load(eid)
        self.assertIn("用户修订后的新句子", render(loaded, "md"))
        self.assertNotIn("识别的旧句子", format_transcript_for_export(loaded))
        with self.assertRaises(ValueError):
            render(loaded, "srt")

    def test_homepage_export_uses_latest_transcript_for_same_work(self):
        import json
        work_id = "7100000000000000001"
        source = f"https://www.douyin.com/video/{work_id}"
        old = library.save(Transcript(text="之前识别的旧稿", segments=[], source=source), "旧转录")
        new = library.save(Transcript(text="再次识别后的新稿", segments=[], source=source), "新转录")
        for eid, created_at in ((old, "2026-09-06T12:00:00"), (new, "2026-09-07T12:00:00")):
            path = library.LIBRARY_DIR / f"{eid}.json"
            metadata = json.loads(path.read_text())
            metadata["created_at"] = created_at
            path.write_text(json.dumps(metadata), encoding="utf-8")
        page = self.window._ensure_page_loaded(5)
        exported = page._transcript_text_map()
        self.assertEqual(exported[work_id], "再次识别后的新稿")
        self.assertEqual(exported[f"douyin:{work_id}"], "再次识别后的新稿")

    def test_external_edit_also_invalidates_original_timestamps(self):
        tr = Transcript(text="旧文", segments=[Segment(text="旧文", start=0, end=1)])
        eid = library.save(tr, "外部修改")
        library.markdown_path(eid).write_text("外部新稿", encoding="utf-8")
        self.assertFalse(library.load(eid).has_timestamps)

    def test_search_reuses_unchanged_body_and_detects_external_edits(self):
        from src.ui.library_page import LibraryPage
        eid = library.save_document("可搜索的正文", "文稿标题")
        page = LibraryPage(self.window.cfg)
        page.refresh()
        page._on_search("正文")
        self.wait_library(page)
        first_model = page.model
        with patch.object(library, "_load_raw", wraps=library._load_raw) as load:
            page._on_search("可搜索")
            self.wait_library(page)
            self.assertIs(page.model, first_model)
            load.assert_not_called()
            library.markdown_path(eid).write_text("外部刚刚改成的新词", encoding="utf-8")
            page._on_search("可搜索")
            self.wait_library(page)
            self.assertNotIn(eid, page._ordered_ids)
            page._on_search("新词")
            self.wait_library(page)
            self.assertIn(eid, page._ordered_ids)
            self.assertGreater(load.call_count, 0)
        page.deleteLater()

    def test_search_tracks_relocated_markdown_and_later_external_edit(self):
        import json
        from src.ui.library_page import LibraryPage
        eid = library.save_document("旧位置正文", "文稿标题")
        page = LibraryPage(self.window.cfg)
        page.refresh()
        page._on_search("旧位置")
        self.wait_library(page)
        old_path = library.markdown_path(eid)
        moved_path = old_path.with_name("relocated.md")
        old_path.rename(moved_path)
        metadata_path = library.LIBRARY_DIR / f"{eid}.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["markdown_path"] = str(moved_path)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        page._on_search("旧位置")
        self.wait_library(page)
        moved_path.write_text("迁移后再修改的正文", encoding="utf-8")
        page._on_search("再修改")
        self.wait_library(page)
        self.assertIn(eid, page._ordered_ids)
        self.assertEqual(Path(page._entries_by_id[eid]["markdown_path"]), moved_path)
        page.deleteLater()


    def test_empty_transcription_is_not_reported_as_success(self):
        self.group(24)
        task = self.window.tasks[0]
        task.state = TaskState.RUNNING
        self.window._on_done(task.id, Transcript(text=" \n", segments=[]))
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIsNone(task.library_id)
        self.assertIn("未识别到有效语音", task.error)
        self.assertEqual(library.list_entries(), [])

    def test_collapsed_real_threads_save_all_results_and_preserve_failure(self):
        group = self.group(24)
        self.assertEqual(len(self.window.extraction_workspace.rows()), 24)
        def transcribe(source, **kwargs):
            if source.endswith("/7"):
                raise RuntimeError("FIXTURE_DOWNLOAD_TIMEOUT")
            return Transcript(text=f"成功正文 {source}", segments=[], engine="fixture")
        with patch("src.ui.worker.transcribe_source", side_effect=transcribe):
            self.window._start_task_group(group.id)
            self.wait_idle()
        done = [t for t in self.window.tasks if t.state == TaskState.DONE]
        self.assertEqual(len(done), 23)
        self.assertEqual(self.window.tasks[7].error, "FIXTURE_DOWNLOAD_TIMEOUT")
        self.assertEqual(len(self.window.extraction_workspace.rows()), 24)
        for task in done:
            self.assertIn("成功正文", library.load_markdown(task.library_id))

    def test_collapsed_result_can_retry_saving_without_retranscription(self):
        self.group(24)
        task = self.window.tasks[0]
        task.state = TaskState.RUNNING
        result = Transcript(text="只识别一次", segments=[], engine="fixture")
        with patch.object(library, "save", side_effect=OSError("fixture disk failure")):
            self.window._on_done(task.id, result)
        self.assertIs(task.transcript, result)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertTrue(self.window._retry_result_save(task.id))
        self.assertEqual(task.state, TaskState.DONE)
        self.assertIn("只识别一次", library.load_markdown(task.library_id))

    def test_cancel_request_wins_over_late_success(self):
        self.group(24)
        task = self.window.tasks[0]
        task.state = TaskState.RUNNING
        self.window._cancelling_task_ids.add(task.id)
        self.window._on_done(task.id, Transcript(text="迟到的结果", segments=[]))
        self.assertIsNone(task.transcript)
        self.assertIsNone(task.library_id)
        self.assertIn(task.id, self.window._cancelling_task_ids)

    def test_select_all_does_not_emit_user_item_changes(self):
        page = self.window._ensure_page_loaded(5)
        page._items = [CollectedItem(str(i + 7100000000000000000), title=f"作品 {i}") for i in range(100)]
        page._render()
        changes = []
        page.table.itemChanged.connect(lambda item: changes.append(item.column()))
        page._toggle_select_all()
        self.assertEqual(page._selected_keys, {item.stable_key for item in page._items})
        self.assertEqual(changes, [])
        page._toggle_select_all()
        self.assertEqual(page._selected_keys, set())
        self.assertEqual(changes, [])

    def test_visible_select_all_preserves_hidden_selection_and_shift_range(self):
        page = self.window._ensure_page_loaded(5)
        page._items = [CollectedItem(str(i + 7100000000000000000), title=f"作品 {i}") for i in range(5)]
        page._render()
        hidden = page._items[-1].stable_key
        page._selected_keys = {hidden}
        page._view = page._items[:4]
        page._fill_table()
        page._toggle_select_all()
        page._toggle_select_all()
        self.assertEqual(page._selected_keys, {hidden})
        page._reduce_selection(page._view[0].stable_key)
        page._reduce_selection(page._view[2].stable_key, modifiers=Qt.ShiftModifier)
        self.assertEqual(page._selected_keys, {hidden} | {v.stable_key for v in page._view[:3]})
