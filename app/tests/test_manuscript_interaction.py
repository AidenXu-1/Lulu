"""Isolated manuscript interactions: temporary library, no credentials or external services."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_agent_workspace_ui as ui
from src.core import library


class ManuscriptInteractionTests(unittest.TestCase):
    setUpClass = classmethod(ui.AgentWorkspaceUiTests.setUpClass.__func__)
    setUp = ui.AgentWorkspaceUiTests.setUp
    tearDown = ui.AgentWorkspaceUiTests.tearDown
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js

    def test_cover_and_bare_source_url_do_not_block_timestamp_reference(self):
        original = '# 测试稿\n\n![封面](file:///fixture/cover.jpg)\n\n> 原作品：https://example.test/post?a=1&b=2\n\n## 逐字稿\n\n**[00:16]** 第一段重复原文。\n\n**[00:17]** 第二段原文。\n\n**[00:19]** 第一段重复原文。\n'
        eid = library.save_document(original, '测试稿')
        self.widget.open_entry(eid)
        self.until(lambda: self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const p=[...el.querySelectorAll('p')].find(p=>p.textContent.includes('[00:19]'));
            const r=document.createRange();r.selectNodeContents(p);const s=window.getSelection();s.removeAllRanges();s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));})()''')
        self.until(lambda: self.js('Boolean(window.lulu.state.selection)'))
        self.js('document.querySelector("#ask-selection").click()')
        self.until(lambda: self.js('window.lulu.state.refs.length') == 1)
        reference = json.loads(self.js('JSON.stringify(window.lulu.state.refs[0])'))
        self.assertEqual(reference['markdown'], '**[00:19]** 第一段重复原文。')
        self.assertEqual(library.document_snapshot(eid)['text'], original)
        self.assertEqual(self.js('document.querySelectorAll(".ProseMirror img:not(.ProseMirror-separator)").length'), 1)

    def test_command_toggle_shift_range_and_filter_reset(self):
        ids = [library.save_document('正文', f'顺序 {n}') for n in range(5)]
        self.widget.refresh()
        self.until(lambda: self.js('window.lulu.state.rows.length') == 6)
        rows = json.loads(self.js('JSON.stringify(window.lulu.state.rows.map(r=>r.id))'))
        def click(index, **mods):
            selector = '[data-entry="' + rows[index] + '"]'
            self.js('document.querySelector(' + json.dumps(selector) + ').dispatchEvent(new MouseEvent("click",' + json.dumps(dict(bubbles=True, **mods)) + '))')
        def selected():
            return set(json.loads(self.js('JSON.stringify([...window.lulu.state.selected])')))
        click(1)
        self.until(lambda: self.js('window.lulu.state.doc?.id') == rows[1])
        click(3, metaKey=True)
        self.assertEqual(selected(), {rows[1], rows[3]})
        self.assertEqual(self.js('document.querySelectorAll("#list [aria-selected=true]").length'), 2)
        click(3, metaKey=True)
        self.assertEqual(selected(), {rows[1]})
        click(5, shiftKey=True)
        self.assertEqual(selected(), set(rows[3:6]))
        click(1, shiftKey=True, metaKey=True)
        self.assertEqual(selected(), set(rows[1:6]))
        self.js('document.querySelector("#sort").value="title";document.querySelector("#sort").dispatchEvent(new Event("change"))')
        self.until(lambda: not self.js('window.lulu.state.batch'))
        self.assertEqual(selected(), set())

    def test_batch_export_preserves_latest_edits_duplicates_and_partial_failure(self):
        from src.ui.agent_workspace.batch import deliver
        out = self.root / 'output'; out.mkdir()
        one = library.save_document('第一份最新正文', '同名')
        two = library.save_document('第二份正文', '同名')
        (out / '同名.md').write_text('原有文件')
        result = deliver('batch_export', document_ids=[one, 'missing-document', two], folder=out)
        self.assertEqual([r['ok'] for r in result['results']], [True, False, True])
        self.assertEqual((out / '同名.md').read_text(), '原有文件')
        self.assertEqual((out / '同名 (2).md').read_text(), '第一份最新正文')
        self.assertEqual((out / '同名 (3).md').read_text(), '第二份正文')
        result = deliver('batch_obsidian', document_ids=[one], folder=out)
        self.assertTrue(result['results'][0]['ok'])
        self.assertIn('第一份最新正文', (out / result['results'][0]['filename']).read_text())
        result = deliver('batch_feishu', document_ids=[one, two])
        self.assertEqual([r['title'] for r in result['records']], ['同名', '同名'])
        self.assertTrue(all(r['ok'] for r in result['results']))

    def test_batch_export_bridge_saves_selected_documents_once(self):
        out = self.root / 'output'; out.mkdir()
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector("#doc-title").textContent="当前编辑标题";document.querySelector("#doc-title").dispatchEvent(new Event("input"))')
        self.js('document.querySelector("#select-mode").click()')
        self.js('document.querySelector("[data-entry]").click()')
        with patch('src.ui.agent_workspace.widget.QFileDialog.getExistingDirectory', return_value=str(out)):
            self.js('document.querySelector("#batch-export").click()')
            self.until(lambda: (out / '当前编辑标题.md').exists())
            self.until(lambda: self.js('document.querySelector("#dialog").open'))
        self.assertIn('已完成 1 篇', self.js('document.querySelector("#dialog-body").innerText'))
        self.assertEqual(len(list(out.iterdir())), 1)

    def test_visible_knowledge_buttons_deliver_selection_and_more_opens_move(self):
        from types import SimpleNamespace
        out = self.root / 'obsidian'; out.mkdir()
        other = library.save_document('第二份正文', '第二份')
        self.widget.main = SimpleNamespace(_ensure_obsidian_dir=lambda: out, _toast=lambda message: None)
        self.widget.refresh()
        self.until(lambda: self.js('window.lulu.state.rows.length') == 2)
        self.js('document.querySelector("#select-mode").click()')
        self.assertTrue(self.js('document.querySelector("#batch-obsidian").disabled'))
        self.js('document.querySelectorAll("[data-entry]").forEach(row=>row.click())')
        self.js('document.querySelector("#batch-obsidian").click()')
        self.until(lambda: self.js('document.querySelector("#dialog").open'))
        self.assertEqual(len(list(out.glob('*.md'))), 2)
        self.assertIn('已完成 2 篇', self.js('document.querySelector("#dialog-body").innerText'))
        self.js('document.querySelector("#dialog").close()')
        with patch('src.ui.feishu_dialog.send_records_to_feishu', return_value=None) as send:
            self.js('document.querySelector("#batch-feishu").click()')
            self.until(lambda: send.called)
            self.until(lambda: not self.js('document.querySelector("#batch-feishu").disabled'))
            self.assertEqual({r.title for r in send.call_args.args[2]}, {'第二份', '把 AI 放进每天的创作流程'})
            send.assert_called_once()
        self.js('document.querySelector("#batch-more").click()')
        self.until(lambda: self.js('Boolean(document.querySelector(".lulu-menu"))'))
        self.js('document.querySelector(".lulu-menu button").click()')
        self.until(lambda: self.js('document.querySelector(".lulu-menu")?.getAttribute("aria-label")') == '移动到文件夹')

    def test_link_uses_default_browser_and_chinese_menu_has_no_web_commands(self):
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest as Request
        from src.ui.agent_workspace.context_menu import open_external
        eid = library.save_document('[来源](https://example.test/article)', '链接稿')
        self.widget.open_entry(eid)
        self.until(lambda: self.js('Boolean(document.querySelector(".ProseMirror a"))'))
        with patch('src.ui.agent_workspace.context_menu.QDesktopServices.openUrl', return_value=True) as opened:
            self.js('document.querySelector(".ProseMirror a").click()')
            self.until(lambda: opened.called)
            self.assertEqual(opened.call_args.args[0].toString(), 'https://example.test/article')
            for url in ('javascript:alert(1)', 'file:///tmp/private', 'data:text/html,test', 'https://user:pass@example.test'):
                with self.assertRaises(ValueError): open_external(url)
            self.assertEqual(opened.call_count, 1)
        flags = Request.CanCopy | Request.CanCut | Request.CanPaste | Request.CanSelectAll
        menu = self.widget.view.make_document_menu('https://example.test/article', True, True, flags, {'editor':True,'undo':False,'redo':True})
        labels = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertEqual(labels, ['用默认浏览器打开链接','复制链接','撤销','重做','剪切','复制','粘贴','粘贴为纯文本','全选'])
        self.assertFalse(next(a for a in menu.actions() if a.text() == '撤销').isEnabled())
        self.assertTrue(next(a for a in menu.actions() if a.text() == '重做').isEnabled())
        menu.deleteLater()
        empty = self.widget.view.make_document_menu('', False, False, Request.EditFlag(0), {})
        self.assertEqual(empty.actions(), [])
        empty.deleteLater()

    def test_editor_menu_undo_redo_preserves_document_history(self):
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector(".ProseMirror"))'))
        before = library.document_snapshot(self.eid)['text']
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();const r=document.createRange();r.selectNodeContents(el);r.collapse(false);const s=window.getSelection();s.removeAllRanges();s.addRange(r);document.execCommand('insertText',false,'测试新增');})()''')
        self.until(lambda: self.js('window.lulu.editStatus().undo'))
        self.js('window.lulu.editAction("undo")')
        self.until(lambda: self.js('window.lulu.editStatus().redo'))
        self.assertNotIn('测试新增', self.js('document.querySelector(".ProseMirror").textContent'))
        self.js('window.lulu.editAction("redo")')
        self.assertIn('测试新增', self.js('document.querySelector(".ProseMirror").textContent'))

    def test_batch_selection_keeps_other_row_titles_when_current_document_saves(self):
        other=library.save_document('其他内容','另一篇独立标题')
        self.widget.refresh()
        self.until(lambda:self.js('window.lulu.state.rows.length')==2)
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector('+json.dumps('[data-entry="'+other+'"]')+').dispatchEvent(new MouseEvent("click",{bubbles:true,metaKey:true}))')
        self.js('document.querySelector("#doc-title").textContent="只修改当前标题";document.querySelector("#doc-title").dispatchEvent(new Event("input"))')
        self.until(lambda:library.document_snapshot(self.eid)['title']=='只修改当前标题')
        self.assertEqual(self.js('document.querySelector('+json.dumps('[data-entry="'+other+'"] strong')+').textContent'),'另一篇独立标题')

    def test_feishu_batch_waits_for_receipt_and_blocks_close_without_resending(self):
        from types import SimpleNamespace
        from PySide6.QtCore import QObject, Signal
        class Worker(QObject):
            done=Signal(bool,object)
            finished=Signal()
            def isRunning(self):return True
        worker=Worker();responses=[]
        self.widget.main=SimpleNamespace(_toast=lambda value:None)
        self.widget.bridge.reply.connect(lambda rid,raw:responses.append(json.loads(raw)) if rid=='feishu-batch-test' else None)
        with patch('src.ui.feishu_dialog.send_records_to_feishu',return_value=worker) as send:
            self.widget.bridge.nativeAction('feishu-batch-test','batch_feishu',json.dumps({'document_ids':[self.eid]}))
            self.until(lambda:self.widget.batch_feishu_worker is worker)
            self.assertFalse(responses)
            self.assertFalse(self.widget.shutdown(1))
            self.assertFalse(self.widget.closing)
            records=send.call_args.args[2]
            self.assertEqual(len(records),1)
            self.assertEqual(records[0].title,'把 AI 放进每天的创作流程')
            worker.done.emit(True,SimpleNamespace(complete=False))
            worker.finished.emit()
            self.assertFalse(responses[0]['value']['external_complete'])
            send.assert_called_once()
            self.assertIsNone(self.widget.batch_request)
            self.assertIsNone(self.widget.batch_feishu_worker)

    def test_context_menu_actions_copy_paste_select_all_and_undo(self):
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest as Request
        clipboard=QGuiApplication.clipboard();previous=clipboard.text()
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        try:
            self.js('document.querySelector("#prompt").focus()')
            flags=Request.CanCopy|Request.CanCut|Request.CanPaste|Request.CanSelectAll|Request.CanUndo|Request.CanRedo
            menu=self.widget.view.make_document_menu('',True,False,flags,{})
            actions={a.text():a for a in menu.actions()}
            clipboard.setText('中文粘贴样例')
            actions['粘贴'].trigger()
            self.until(lambda:self.js('document.querySelector("#prompt").value')=='中文粘贴样例')
            actions['全选'].trigger();actions['复制'].trigger()
            self.until(lambda:clipboard.text()=='中文粘贴样例')
            actions['剪切'].trigger()
            self.until(lambda:self.js('document.querySelector("#prompt").value')=='')
            actions['撤销'].trigger()
            self.until(lambda:self.js('document.querySelector("#prompt").value')=='中文粘贴样例')
            actions['重做'].trigger()
            self.until(lambda:self.js('document.querySelector("#prompt").value')=='')
            clipboard.setText('纯文本')
            actions['粘贴为纯文本'].trigger()
            self.until(lambda:self.js('document.querySelector("#prompt").value')=='纯文本')
            menu.deleteLater()
        finally:clipboard.setText(previous)
