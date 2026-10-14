"""Library behavior contracts after the approved virtual-list redesign.

The former nested-card geometry is intentionally retired. These checks retain
its user outcomes: themes, source metadata, folder selection, export and drag.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QListView
from src.core import library
from src.core.models import Transcript
from src.ui.library_page import LibraryPage, EntryCard, _ENTRY_MIME
from src.ui import theme as T


class GlobalThemeBatch3LibrariesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(library, "LIBRARY_DIR", self.root / "library"),
                        patch.object(library, "FOLDERS_FILE", self.root / "library/folders.json")]
        for p in self.patches:
            p.start()
        self.pages = []
        self.old_theme, self.old_qss = T.ACTIVE.name, self.app.styleSheet()

    def tearDown(self):
        for page in self.pages:
            self.wait(page)
            page.close()
            page.deleteLater()
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()
        T.apply_theme(self.old_theme)
        self.app.setStyleSheet(self.old_qss)

    def wait(self, page):
        deadline = time.monotonic() + 5
        while (page._query_worker or page._batch_worker or page._pending_refresh) and time.monotonic() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        self.assertIsNone(page._query_worker)
        self.assertIsNone(page._batch_worker)

    def page(self, kind="transcript", theme="warm", size=(900, 700), cfg=None):
        T.apply_theme(theme)
        self.app.setStyleSheet(T.QSS)
        page = LibraryPage(cfg, on_view=lambda _: None)
        self.pages.append(page)
        page.resize(*size)
        page._set_kind(kind)
        page.show()
        self.wait(page)
        return page

    def test_three_libraries_share_virtual_structure_and_empty_state(self):
        for kind, title in (("transcript", "文稿库"), ("rewrite", "输出库"), ("voice", "音频库")):
            page = self.page(kind)
            self.assertEqual(page.title_label.text(), title)
            self.assertIsInstance(page.list_view, QListView)
            self.assertTrue(page.list_view.uniformItemSizes())
            self.assertEqual(page.findChildren(EntryCard), [])
            self.assertIn("还没有素材", page.status_label.text())

    def test_paper_and_two_sizes_keep_workspace_edges_inside(self):
        library.save_document("正文", "长标题" * 60)
        for theme in ("warm",):
            for size in ((900, 700), (620, 560)):
                page = self.page(theme=theme, size=size)
                self.assertEqual(page.size().width(), size[0])
                self.assertGreater(page.list_view.height(), 300)
                self.assertEqual(page.list_view.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(page.control_stack.geometry().right(), page.width())
                self.assertEqual(page._consumer.name, theme)

    def test_only_list_scrolls_and_original_source_metadata_remains_available(self):
        for n in range(16):
            library.save(Transcript(text="文稿正文", source="unit", meta={
                "original_url": "https://www.douyin.com/video/735099", "author": {"name": "作者甲"},
                "stats": {"like": 12, "favorite": 8, "comment": 3, "share": 2}}), str(n))
        page = self.page(size=(680, 560))
        top = page.control_stack.y()
        bar = page.list_view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.app.processEvents()
        self.assertEqual(page.control_stack.y(), top)
        index = page.model.index(0)
        self.assertIn("作者甲", index.data(Qt.DisplayRole))
        self.assertIn("赞 12", index.data(Qt.ToolTipRole))
        self.assertEqual(index.data(Qt.UserRole)["source_url"], "https://www.douyin.com/video/735099")

    def test_drag_carries_stable_record_identity_and_move_preserves_body(self):
        eid = library.save_document("不能丢失的正文", "文稿")
        folder = library.create_folder("transcript", "目标")
        page = self.page()
        data = page.model.mimeData([page.model.index(0)])
        self.assertEqual(bytes(data.data(_ENTRY_MIME)).decode(), eid)
        page.folder_filter.entryDropped.emit(eid, folder)
        self.wait(page)
        self.assertEqual(library.document_snapshot(eid)["folder_id"], folder)
        self.assertEqual(library.load_markdown(eid), "不能丢失的正文")

    def test_folder_click_selects_and_clears_all_member_entries(self):
        folder = library.create_folder("transcript", "整组选中")
        entries = {library.save_document(str(n), str(n), folder_id=folder) for n in range(2)}
        library.save_document("不属于此组", "其他")
        page = self.page()
        page.folder_filter.setCurrentIndex(page.folder_filter.findData(folder))
        self.wait(page)
        page._toggle_select_all()
        self.assertEqual(page._sel_entries, entries)
        self.assertEqual(page.sel_count_label.text(), "已选 2 项")
        page._toggle_select_all()
        self.assertEqual(page._sel_entries, set())
        self.assertTrue(all(not button.isEnabled() for button, _ in page._batch_btns))

    def test_obsidian_batch_keeps_failures_selected_and_stays_in_selection_mode(self):
        for n in range(2):
            library.save_document(str(n), str(n))
        cfg = type("Config", (), {"obsidian_dir": str(self.root), "save": lambda _: None})()
        page = self.page(cfg=cfg)
        page._toggle_select_all()
        ordered = list(page._ordered_ids)
        with patch("src.core.obsidian.save_note", side_effect=[None, OSError("disk unavailable")]):
            page._batch_obsidian()
            self.wait(page)
        self.assertTrue(page._selecting)
        self.assertEqual(page._sel_entries, {ordered[1]})
        self.assertIn("1 项未完成", page.status_label.text())

    def test_batch_controls_replace_filters_without_moving_content(self):
        library.save_document("正文", "文稿")
        page = self.page()
        top = page.list_view.y()
        page._toggle_select_all()
        self.app.processEvents()
        self.assertEqual(page.list_view.y(), top)
        self.assertTrue(page.toolbar.isVisible())
        page._set_selecting(False)
        self.app.processEvents()
        self.assertEqual(page.list_view.y(), top)
        self.assertTrue(page.folder_filter.isVisible())
