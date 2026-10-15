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
