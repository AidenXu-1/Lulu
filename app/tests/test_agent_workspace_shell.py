"""Real Qt shell + local editor, with all user stores and system services isolated."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from tests import test_agent_workspace_ui as ui
from src.config import AppConfig
from src.core import library, models_manager, secrets
from src.ui.main_window import MainWindow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLabel, QFrame


class WorkspaceShellTests(unittest.TestCase):
    setUpClass = ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        cfg=AppConfig();cfg.theme=getattr(self,'theme_name','warm');cfg.prevent_sleep_during_tasks=False
        self.patches=[patch.object(AppConfig,'load',return_value=cfg),patch.object(AppConfig,'save',return_value=True),
            patch.object(library,'LIBRARY_DIR',self.root/'library'),patch.object(library,'FOLDERS_FILE',self.root/'library/folders.json'),
            patch.object(models_manager,'app_data_dir',return_value=self.root),patch.object(secrets,'_backend',side_effect=AssertionError('No real keychain access')),
            patch.object(secrets,'_cache',{}),patch.object(secrets,'_memory',{}),
            patch.object(MainWindow,'_sync_runtime_preferences'),
            patch.object(MainWindow,'_choose_xhs_exit_action',return_value='keep'),patch.object(MainWindow,'_settle_xhs_exit',return_value=True)]
        for p in self.patches:p.start()
        self.eid=library.save_document('每个好工具，都应该把注意力还给内容。\n\n## 顺手地继续创作\n\n选中一段，写下要求，确认后再应用修改。','好工具，应该让人忘记工具')
        self.window=MainWindow(task_queue_store=None,collection_service=MagicMock())
        self.window.resize(1680,960);self.window.show()
        self.window.open_material_entry(self.eid)
        self.widget=self.window.library_page
        self.until(lambda:self.widget.ready)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)

    def tearDown(self):
        self.widget.shutdown();self.window.close();self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.app.processEvents()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_native_shell_routes_document_and_keeps_four_sizes_usable(self):
        self.assertFalse(hasattr(self.window,"rail_right"))
        self.assertFalse(hasattr(self.window, "engine_card"))
        dimensions=[]
        for width,height in [(1680,960),(1480,940),(1200,760),(1728,1000)]:
            self.window.resize(width,height)
            for _ in range(8):self.tick()
            actual=json.loads(self.js('JSON.stringify(["list-pane","detail","agent-column"].map(id=>Math.round(document.getElementById(id).getBoundingClientRect().width)))'))
            dimensions.append(dict(window=[self.window.width(),self.window.height()],navigation=self.window.sidebar_paper.width(),columns=actual,
                                   web=[self.widget.width(),self.widget.height()]))
            self.assertGreaterEqual(actual[1],350)
            self.assertGreaterEqual(actual[2],280)
            self.assertEqual(self.js('document.querySelector("#prompt").getBoundingClientRect().height'),78)
            for material,column in zip(self.window._titlebar_paper_frames(),
                                       (self.window.left_wrap,self.window.center_wrap),strict=True):
                self.assertEqual((material.x(),material.width()),(column.x(),column.width()))
            self.assertFalse(any(label.text() in {'Lulu','把灵感变成作品'} for label in self.window.brand_row.findChildren(QLabel)))
            self.assertIn(self.window.brand_logo_label.width(), (52,60))
            self.js('document.querySelector("#model").click()')
            box=json.loads(self.js('JSON.stringify(["model-menu","agent-column"].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.left,r.right]}))'))
            self.assertGreaterEqual(box[0][0],box[1][0])
            self.assertLessEqual(box[0][1],box[1][1])
            self.js('document.querySelector("#model").click()')
            self.window.grab().save(str(Path(__file__).resolve().parents[2]/f'scratch/v37-native-{width}.png'))
        output = Path(__file__).resolve().parents[2]/'scratch'
        output.mkdir(parents=True, exist_ok=True)
        (output/'v37-native-dimensions.json').write_text(json.dumps(dimensions,indent=2))
        self.window.open_library('voice')
        self.until(lambda:self.js('window.lulu.state.kind')=='voice')
        self.assertTrue(self.js('document.getElementById("agent-column").hidden'))
        self.window.open_material_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)

    def test_native_library_counts_stay_global_while_searching(self):
        label = self.window._library_count_labels['transcript']
        self.until(lambda:label.text() == '1')
        self.assertEqual(self.window._library_count_labels['voice'].text(), '0')
        self.until(lambda:self.js('document.querySelector(".entry-data")?.textContent') == '手动文稿')
        self.js('document.getElementById("search").value="找不到的标题";document.getElementById("search").dispatchEvent(new Event("input"));')
        self.until(lambda:self.js('window.lulu.state.total') == 0)
        self.assertEqual(label.text(), '1')
        self.assertEqual(self.js('window.lulu.state.doc.id'), self.eid)
        library.delete(self.eid)
        self.widget.refresh()
        self.until(lambda:label.text() == '0')

    def test_navigation_and_titlebar_stay_stable_between_workspaces(self):
        before=self.window.sidebar_paper.styleSheet()
        rows=self.window.sidebar_paper.findChildren(QFrame)
        rows=[row for row in rows if row.property('luluControl')=='navRow']
        for row in rows:row.clearFocus()
        for _ in range(4):self.tick()
        heights=[row.height() for row in rows]
        self.window._switch_page(0)
        for row in rows:row.clearFocus()
        for _ in range(4):self.tick()
        self.assertEqual(self.window.sidebar_paper.styleSheet(),before)
        self.assertEqual([row.height() for row in rows],heights)
        self.assertTrue(self.window.window_title_label.property('immersiveTitlebar'))
        self.window.open_material_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)
        self.assertEqual(self.window.sidebar_paper.styleSheet(),before)
        self.assertNotIn('border-bottom',self.window.titlebar_paper_center.styleSheet())

    def test_workbench_chrome_restored_after_each_library_kind(self):
        from src.ui import theme
        from src.ui.voice_page import VoicePage
        from src.core.collect.application import XhsBrowserStatus
        from src.core.synthesis import voices, glossary, style_presets
        for replacement in (
            patch.object(VoicePage,'_refresh_indextts2_status'),
            patch.object(voices,'list_voices',return_value=[]),
            patch.object(glossary,'glossary_path',return_value=self.root/'glossary.json'),
            patch.object(style_presets,'style_presets_path',return_value=self.root/'styles.json'),
        ):
            replacement.start();self.addCleanup(replacement.stop)
        original = theme.consumer_theme('warm')
        def navigation_positions():
            return [(row.x(), row.y(), row.width(), row.height()) for _, _, row, *_ in self.window._page_navs]
        baseline_positions = navigation_positions()
        self.assertFalse(hasattr(self.window, 'engine_card'))
        self.window._collection_service.xhs_browser_status.return_value=XhsBrowserStatus()
        for kind in ('transcript','voice','trash'):
            if kind=='trash':
                self.window._open_recently_deleted()
                self.until(lambda:self.js('window.lulu.state.page')=="trash")
            else:
                self.window.open_library(kind)
            if kind != "trash":
                self.until(lambda:self.js('window.lulu.state.kind')==('voice' if kind=='voice' else 'documents'))
            self.assertEqual(self.window.sidebar_paper.styleSheet(), '')
            for index in (0,5,4):
                self.window._switch_page(index)
                for _ in range(4):self.tick()
                self.assertEqual(navigation_positions(), baseline_positions)
                self.assertTrue(self.window.local_storage_note.isVisible())
                self.assertEqual(self.window.sidebar_paper.styleSheet(), '')
                self.assertTrue(all(frame.styleSheet()=='' for frame in self.window._titlebar_paper_frames()))
                self.assertEqual(self.window.sidebar_paper.property('paperTextureStrength'),original.paper_rail_opacity)
                self.assertEqual(self.window.center_wrap.property('paperTextureStrength'),original.paper_canvas_opacity if index==0 else 0.0)
                self.assertEqual([frame.property('paperTextureStrength') for frame in self.window._titlebar_paper_frames()],
                                 [original.paper_rail_opacity,original.paper_canvas_opacity])
                self.assertEqual(self.window.window_title_label.property('immersiveTitlebar'),index==0)

    def test_idle_preparation_reuses_workspace_without_changing_active_page(self):
        other = MainWindow(task_queue_store=None, collection_service=MagicMock())
        try:
            other.show()
            self.assertEqual(other.stack.currentIndex(), 0)
            self.until(lambda:other.library_page is not None and other.library_page.ready)
            self.assertEqual(other.stack.currentIndex(), 0)
            self.assertIs(other.library_page.surface.currentWidget(), other.library_page.view)
            workspace = other.library_page
            loads=[]
            workspace.view.loadStarted.connect(lambda:loads.append(True))
            other.open_library('transcript')
            for _ in range(8):self.tick()
            other._switch_page(0);other.open_library('voice')
            for _ in range(8):self.tick()
            self.assertIs(other.library_page,workspace)
            self.assertFalse(loads)
            self.assertFalse(hasattr(other,'rail_right'))
            self.assertFalse(hasattr(other,'panel'))
            self.assertFalse(hasattr(other,'update_card'))
        finally:
            if other.library_page:other.library_page.shutdown()
            other.close();other.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
