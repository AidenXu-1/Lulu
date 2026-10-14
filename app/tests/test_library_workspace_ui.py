import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QEventLoop, QTimer, QItemSelectionModel
from PySide6.QtWidgets import QApplication
from src.core import library
from src.ui.library_page import LibraryPage, EntryCard
from src.ui import theme as T


class LibraryWorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(library, "LIBRARY_DIR", self.root),
                        patch.object(library, "FOLDERS_FILE", self.root / "folders.json")]
        for p in self.patches:
            p.start()
        self.pages = []

    def tearDown(self):
        for page in self.pages:
            self.wait(page)
            page.close()
            page.deleteLater()
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()
        T.apply_theme("warm")

    def wait(self, page):
        end = time.monotonic() + 5
        while (page._query_worker is not None or page._batch_worker is not None or page._pending_refresh) and time.monotonic() < end:
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        self.assertIsNone(page._query_worker)
        self.assertIsNone(page._batch_worker)

    def page(self):
        page = LibraryPage(on_view=lambda eid: None)
        self.pages.append(page)
        page.resize(620, 760)
        page.show()
        page._set_kind("transcript")
        self.wait(page)
        return page

    def test_bad_records_do_not_break_virtual_list_and_single_click_preview(self):
        eid = library.save_document("正文", "正常文稿")
        (self.root / "bad.json").write_text("[]")
        page = self.page()
        self.assertEqual(page.model.rowCount(), 1)
        self.assertEqual(page.findChildren(EntryCard), [])
        self.assertIn("读取异常", page.status_label.text())
        calls = []
        page._on_view = calls.append
        page._preview_index(page.model.index(0))
        self.assertEqual(calls, [eid])

    def test_select_all_matches_search_and_batch_failure_keeps_only_failed_selected(self):
        ids = [library.save_document("匹配" if n < 2 else "其他", str(n)) for n in range(3)]
        page = self.page()
        page._on_search("匹配")
        self.wait(page)
        page._toggle_select_all()
        self.assertEqual(page._sel_entries, set(ids[:2]))
        def action(eid):
            if eid == ids[0]:
                raise OSError("disk unavailable")
        page._start_batch(action)
        self.wait(page)
        self.assertEqual(page._sel_entries, {ids[0]})
        self.assertEqual({i.data(256)["id"] for i in page.list_view.selectedIndexes()}, {ids[0]})
        self.assertIn("1 项未完成", page.status_label.text())

    def test_filter_and_selection_restore_per_kind(self):
        eid = library.save_document("标题", "原稿")
        library.save_rewrite("输出", "成稿")
        page = self.page()
        page.search_field.setText("原稿")
        page._on_search("原稿")
        self.wait(page)
        page._toggle_select_all()
        page._set_kind("rewrite")
        self.wait(page)
        self.assertEqual(page.search_field.text(), "")
        self.assertEqual(page.model.rows[0]["kind"], "rewrite")
        page._set_kind("transcript")
        self.wait(page)
        self.assertEqual(page.search_field.text(), "原稿")
        self.assertEqual(page._sel_entries, {eid})

    def test_feishu_prepares_off_thread_and_keeps_selection_until_complete_receipt(self):
        import threading
        from types import SimpleNamespace
        from PySide6.QtCore import QObject, Signal
        from src.core.feishu_storage import record_from_transcript

        class Sender(QObject):
            done = Signal(bool, object)

            def isRunning(self):
                return False

        library.save_document("发送正文", "第一份")
        library.save_document("发送正文", "第二份")
        page = self.page()
        page._toggle_select_all()
        selected = set(page._sel_entries)
        sender = Sender(page)
        threads = []

        def prepare(*args, **kwargs):
            threads.append(threading.get_ident())
            return record_from_transcript(*args, **kwargs)

        with patch("src.core.feishu_storage.record_from_transcript", side_effect=prepare), \
                patch("src.ui.feishu_dialog.send_records_to_feishu", return_value=sender) as send:
            page._batch_feishu()
            self.wait(page)
            send.assert_called_once()
            self.assertEqual(len(send.call_args.args[2]), 2)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(identity != threading.get_ident() for identity in threads))
        self.assertEqual(page._sel_entries, selected)
        sender.done.emit(False, SimpleNamespace(complete=False))
        self.assertEqual(page._sel_entries, selected)
        sender.done.emit(True, SimpleNamespace(complete=True))
        self.wait(page)
        self.assertEqual(page._sel_entries, set())

    def test_picker_can_load_before_library_page_and_uses_virtual_rows(self):
        from src.ui.doc_picker import DocPickerDialog
        library.save_document("正文", "文稿")
        dialog = DocPickerDialog([])
        loop = QEventLoop()
        dialog._worker.finished.connect(loop.quit)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        self.assertFalse(dialog._worker.isRunning())
        self.assertEqual(dialog.model.rowCount(), 1)
        dialog.reject()

    def test_picker_searches_text_beyond_visible_preview(self):
        from src.ui.doc_picker import DocPickerDialog
        library.save_document("前半篇" * 200 + "末尾关键词", "普通标题")
        library.save_document("无关正文", "无关标题")
        dialog = DocPickerDialog([])
        for query in ("", "末尾关键词"):
            if query:
                dialog.search.setText(query)
            deadline = time.monotonic() + 4
            expected_rows = 1 if query else 2
            # A worker can finish before its queued result reaches the UI model.
            # Wait for both the task and the visible result instead of racing it.
            while (dialog._worker.isRunning() or dialog._search_timer.isActive() or dialog._pending_query
                   or dialog.model.rowCount() != expected_rows) and time.monotonic() < deadline:
                loop = QEventLoop()
                QTimer.singleShot(10, loop.quit)
                loop.exec()
            self.assertFalse(dialog._worker.isRunning())
            self.assertEqual(dialog.model.rowCount(), expected_rows)
        self.assertEqual(dialog.model.rowCount(), 1)
        self.assertEqual(dialog.model.rows[0]["title"], "普通标题")
        dialog.reject()


    def test_narrow_layout_keeps_controls_and_rows_inside_paper(self):
        library.save_document("正文", "很长的文稿标题" * 12)
        for theme in ("warm",):
            T.apply_theme(theme)
            page = self.page()
            self.assertEqual(page.width(), 620)
            self.assertGreater(page.list_view.height(), 500)
            self.assertLessEqual(page.new_folder_btn.geometry().right(), page.control_stack.width())
