"""The user-facing routes and recovery flows replacing the retired detail panel."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_agent_workspace_ui as ui
from tests import test_agent_workspace as core_tests
from src.core import library
from src.core.agent_workspace import documents


class RecoveryTests(unittest.TestCase):
    setUp = core_tests.AgentWorkspaceTests.setUp
    tearDown = core_tests.AgentWorkspaceTests.tearDown
    request = core_tests.AgentWorkspaceTests.request
    def test_history_labels_preview_and_restore_preserve_current(self):
        before = documents.snapshot(self.eid)
        documents.save(**self.request('新的正文'))
        rows = self.service.history(self.eid)
        self.assertTrue(rows)
        row = rows[0]
        self.assertIsInstance(row['revision'], int)
        self.assertRegex(row['saved_at'], r'^\d{4}-\d{2}-\d{2}T')
        self.assertEqual(self.service.history(self.eid, row['name'])['text'], before['text'])
        current = documents.snapshot(self.eid)
        self.service.restore(self.eid, row['name'], current['revision'], current['content_hash'], 'restore-new')
        self.assertEqual(documents.snapshot(self.eid)['text'], before['text'])
        self.assertIn('新的正文', [self.service.history(self.eid, r['name'])['text'] for r in self.service.history(self.eid)])

    def test_tampered_history_and_stale_restore_cannot_overwrite(self):
        documents.save(**self.request('新的正文'))
        row = self.service.history(self.eid)[0]
        current = documents.snapshot(self.eid)
        documents.save(**self.request('继续编辑', 'save2'))
        with self.assertRaises(library.RevisionConflict):
            self.service.restore(self.eid, row['name'], current['revision'], current['content_hash'], 'stale-restore')
        path = library.LIBRARY_DIR/'.versions'/self.eid/row['name']
        path.write_text('篡改', encoding='utf-8')
        with self.assertRaises(ValueError): self.service.history(self.eid, row['name'])
        with self.assertRaises(ValueError): self.service.history(self.eid, '../outside.md')
        self.assertEqual(documents.snapshot(self.eid)['text'], '继续编辑')

    def test_deleted_preview_is_read_only_then_restore(self):
        original = documents.snapshot(self.eid)['text']
        self.service.delete(self.eid)
        row = self.service.trash()[0]
        self.assertEqual(self.service.trash(row['token'])['text'], original)
        self.assertIsNone(library._load_raw(self.eid))
        result = self.service.restore_deleted(row['token'])
        self.assertEqual(result['document_id'], self.eid)
        self.assertEqual(documents.snapshot(self.eid)['text'], original)
        self.assertEqual(self.service.trash(), [])


class RefinementUiTests(unittest.TestCase):
    setUpClass = ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    setUp = ui.AgentWorkspaceUiTests.setUp
    tearDown = ui.AgentWorkspaceUiTests.tearDown
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js

    def open_document(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)

    def screenshot(self,name):
        output = Path(__file__).resolve().parents[2]/'scratch/workspace-refinement-20260923'
        output.mkdir(exist_ok=True)
        self.widget.grab().save(str(output/(name+'.png')))

    def test_history_human_labels_preview_confirm_restore(self):
        library.save_markdown(self.eid,'新的正文',expected_hash=documents.snapshot(self.eid)['content_hash'])
        self.open_document()
        self.js('document.getElementById("history").click()')
        self.until(lambda:bool(self.js('!!document.querySelector("[data-version][aria-selected=true]")')))
        self.until(lambda:self.js('document.getElementById("version-restore")?.disabled') is False)
        self.assertNotRegex(self.js('document.getElementById("dialog-body").textContent'), r'[a-f0-9]{64}')
        self.assertIn('备份于',self.js('document.getElementById("version-label").textContent'))
        self.assertIn('一个好工具',self.js('document.getElementById("version-text").textContent'))
        self.screenshot('history')
        self.js('document.getElementById("version-restore").click()')
        self.assertEqual(documents.snapshot(self.eid)['text'],'新的正文')
        self.js('document.getElementById("version-restore").click()')
        self.until(lambda:'一个好工具' in documents.snapshot(self.eid)['text'])

    def test_trash_is_page_with_preview_restore_and_persistent_editor(self):
        self.open_document()
        self.js('window.originalEditor=document.querySelector(".ProseMirror")')
        other=library.save_document('等待找回的正文','一篇被删除的文稿')
        library.delete(other)
        self.widget._set_kind('trash')
        self.until(lambda:self.js('document.getElementById("trash-text")?.textContent')=='等待找回的正文')
        self.assertFalse(self.js('document.getElementById("dialog").open'))
        self.assertTrue(self.js('document.getElementById("workspace").hidden'))
        self.screenshot('trash')
        self.js('document.getElementById("trash-restore").click()')
        self.until(lambda:library._load_raw(other) is not None)
        self.widget._set_kind('transcript')
        self.until(lambda:self.js('window.lulu.state.page')=='library')
        self.assertTrue(self.js('window.originalEditor===document.querySelector(".ProseMirror")'))

    def test_trash_restore_remains_available_when_preview_fails(self):
        from src.core.agent_workspace.service import WorkspaceService
        original = WorkspaceService.trash
        deleted = library.save_document("找回正文", "预览暂不可用")
        library.delete(deleted)

        def fail_preview(service, token=None):
            if token is not None:
                raise OSError("隔离测试中的预览读取失败")
            return original(service, token)

        with patch.object(WorkspaceService, "trash", new=fail_preview):
            self.widget._set_kind("trash")
            self.until(lambda: self.js('document.getElementById("trash-preview-empty-title")?.textContent') == "预览没有加载出来")
            self.assertFalse(self.js('document.getElementById("trash-restore").disabled'))
            self.js('document.getElementById("trash-restore").click()')
            self.until(lambda: library._load_raw(deleted) is not None)

    def test_trash_batch_restore_keeps_failed_item_selected_for_retry(self):
        from src.core.agent_workspace.service import WorkspaceService
        first = library.save_document("正文 A", "批量找回 A")
        second = library.save_document("正文 B", "批量找回 B")
        library.delete(first)
        library.delete(second)
        failed_token = next(row["token"] for row in library.list_deleted() if row["id"] == first)
        original = WorkspaceService.restore_deleted

        def fail_one(service, token):
            if token == failed_token:
                raise library.RevisionConflict("原位置已有资料，未覆盖。")
            return original(service, token)

        self.widget._set_kind("trash")
        self.until(lambda: self.js('document.querySelectorAll("[data-trash]").length') == 2)
        self.js('document.getElementById("trash-select-mode").click()')
        self.js('document.getElementById("trash-select-visible").click()')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'), 2)
        with patch.object(WorkspaceService, "restore_deleted", new=fail_one):
            self.js('document.getElementById("trash-restore").click()')
            self.until(lambda: self.js('document.querySelectorAll("[data-trash]").length') == 1)
            self.assertEqual(self.js('document.querySelector("[data-trash]")?.dataset.trash'), failed_token)
            self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'), 1)
            self.assertIn("仍在最近删除", self.js('document.getElementById("trash-status").textContent'))
            self.assertIn("1 项", self.js('document.getElementById("trash-restore").textContent'))
        self.js('document.getElementById("trash-restore").click()')
        self.until(lambda: library._load_raw(first) is not None)

    def test_trash_search_filters_names_and_reports_no_matches(self):
        deleted = library.save_document("内容", "唯一可找回标题")
        library.delete(deleted)
        self.widget._set_kind("trash")
        self.until(lambda: self.js('Boolean(document.getElementById("trash-search"))'))
        self.js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"f",metaKey:true,bubbles:true}))')
        self.assertEqual(self.js('document.activeElement?.id'), "trash-search")
        self.js('''(()=>{const input=document.getElementById("trash-search");input.value="没有这项";input.dispatchEvent(new Event("input"));})()''')
        self.until(lambda: self.js('document.getElementById("trash-no-results")?.hidden') is False)
        self.assertTrue(self.js('document.getElementById("trash-restore").disabled'))
        self.assertIn("没有找到符合条件的资料", self.js('document.getElementById("trash-no-results").textContent'))

    def test_trash_empty_state_explains_how_items_get_here(self):
        self.widget._set_kind("trash")
        self.until(lambda: self.js('document.querySelector(".trash-empty-state h2")?.textContent') == "最近删除为空")
        self.assertIn("删除文稿或音频后，可以在这里找回", self.js('document.querySelector(".trash-empty-state").textContent'))

    def test_cloud_connections_are_direct_page_and_keep_draft(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.open_document()
        self.js('window.originalEditor=document.querySelector(".ProseMirror");document.getElementById("prompt").value="还没有发出的提问"')
        original=self.widget.main
        main=SimpleNamespace(_open_workspace_models=Mock())
        self.widget.main=main
        try:
            self.widget.native('models',{})
            main._open_workspace_models.assert_called_once()
            self.assertFalse(self.js('document.getElementById("dialog").open'))
            self.assertTrue(self.js('window.originalEditor===document.querySelector(".ProseMirror")'))
            self.assertEqual(self.js('document.getElementById("prompt").value'),'还没有发出的提问')
            self.assertFalse(self.js('Boolean(document.getElementById("connection-model"))'))
        finally:self.widget.main=original

    def test_deleted_current_document_keeps_unsaved_input_on_switch(self):
        self.open_document()
        other = library.save_document('另一篇正文', '另一篇')
        self.js('document.querySelector(".ProseMirror").focus();document.execCommand("insertText",false,"未保存的输入")')
        library.delete(self.eid)
        self.widget.open_entry(other)
        self.until(lambda:bool(self.js('document.getElementById("toast").textContent')))
        self.assertEqual(self.js('window.lulu.state.doc.id'),self.eid)
        self.assertIn('未保存的输入',self.js('document.querySelector(".ProseMirror").textContent'))
        self.assertIsNone(library._load_raw(self.eid))
        # Restore original identity to permit fixture shutdown to flush the draft.
        library.restore_deleted(library.list_deleted()[0]['token'])

    def test_shutdown_stops_new_requests_before_destroying_background_reader(self):
        self.open_document()
        self.assertTrue(self.widget.shutdown())
        with patch.object(self.widget.pool, "start") as started:
            self.widget.bridge.request("after-close", "list", "{}")
            self.widget.bridge.nativeAction("after-close-native", "models", "{}")
            self.widget.bridge.ready()
            started.assert_not_called()
        self.assertTrue(self.widget.closing)

    def test_compact_paragraph_spacing_keeps_empty_paragraphs(self):
        self.open_document()
        metrics=json.loads(self.js('JSON.stringify((()=>{const p=document.querySelector(".ProseMirror p"),s=getComputedStyle(p);return {line:parseFloat(s.lineHeight),size:parseFloat(s.fontSize),gap:parseFloat(s.marginBottom)}})())'))
        self.assertLessEqual(metrics['line']/metrics['size'],1.71)
        self.assertLessEqual(metrics['gap'],metrics['size']*.51)
        self.screenshot('document-rhythm')


class CollectionWidthTests(unittest.TestCase):
    from tests import test_collection_p3a_ui as collection_ui
    setUpClass = collection_ui.CollectionP3AUiTests.__dict__['setUpClass']
    setUp = collection_ui.CollectionP3AUiTests.setUp
    tearDown = collection_ui.CollectionP3AUiTests.tearDown

    def test_columns_fill_space_and_keep_title_readable(self):
        self.page.show()
        for width in (1680, 920, 1500):
            self.page.resize(width, 960)
            for _ in range(5): self.app.processEvents()
            table = self.page.table
            total = sum(table.columnWidth(i) for i in range(table.columnCount()))
            self.assertGreaterEqual(table.columnWidth(2), 240)
            if width > 1200: self.assertEqual(total, table.viewport().width())
            else: self.assertGreaterEqual(total, table.viewport().width())
        self.page.table.setColumnWidth(1,200)
        self.app.processEvents()
        self.assertEqual(sum(self.page.table.columnWidth(i) for i in range(12)),self.page.table.viewport().width())

class FirstDisplayTests(unittest.TestCase):
    def test_select_construction_leaves_first_polish_to_qt(self):
        from PySide6.QtWidgets import QApplication
        from src.ui.consumer_select import ConsumerSelect, _OptionRow
        app = QApplication.instance() or QApplication([])
        with patch.object(_OptionRow, 'style', side_effect=AssertionError('Unshown rows do not need style refresh')):
            select=ConsumerSelect((('a','选项一'),('b','选项二')), value='a', accessible_name='测试选项')
        select.setCurrentIndex(1)
        select.show(); app.processEvents()
        self.assertEqual(select.currentData(), 'b')
        select.close();select.deleteLater();app.processEvents()

if __name__ == '__main__': unittest.main()
