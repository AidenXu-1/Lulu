"""Isolated retirement/guide contracts; no real credentials, API or clipboard."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src.core import library, library_gateway
from src.core.agent_entry.entrypoints import CoreCommandDispatcher
from src.core.agent_workspace import documents
from src.core.agent_workspace.processing import validate_options, validate_replacement
from tests import test_agent_workspace as core
from tests import test_agent_workspace_conversations as conversations
from tests import test_agent_workspace_ui as ui
from tests import test_agent_workspace_shell as shell


class RetirementCoreTests(unittest.TestCase):
    setUp = core.AgentWorkspaceTests.setUp
    runner = conversations.ConversationsTests.runner
    wait = conversations.ConversationsTests.wait

    def test_retired_request_cannot_initialize_runtime(self):
        factory = Mock(side_effect=AssertionError('retired request must not start Core'))
        dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=self.root, runtime_factory=factory)
        for operation in ('start', 'show', 'cancel', 'recent', 'status'):
            with self.assertRaisesRegex(RuntimeError, 'COMPONENT_EVIDENCE_REQUIRED'):
                dispatcher({'command':'gui.text', 'arguments':{'operation':operation, 'arguments':{}}})
        factory.assert_not_called()
        with self.assertRaises(ValueError):
            library_gateway.validate_request({'operation':'workspace_draft','args':['copywriting'],'kwargs':{}})

    def test_strict_proofread_preserves_numbers_links_and_original(self):
        original = '会议于 09:30 开始，预算 100 元，详情 https://example.com/a 。'
        valid = original.replace(' 。', '。')
        options = validate_options({'strict':True,'language':'英语','terms':'Lulu'})
        validate_replacement(original, {'action':'replace','markdown':valid}, options)
        for edited in (valid.replace('100','200'), valid.replace('/a','/b'), '这是一篇全新文案。'):
            with self.assertRaises(ValueError):
                validate_replacement(original, {'action':'replace','markdown':edited}, options)
        self.eid = library.save_document(original, '严格校对')
        def generate(*a, **kw):
            return dict(text='校对结果', calls=[dict(name='propose_edits',arguments=dict(explanation='校对',
                edits=[dict(target='current_document',action='replace',markdown=valid.replace('100','200'))]))],usage={})
        runner, chat = self.runner(generate)
        jid = runner.start(chat['id'],'strict1','只校正错字',options=options)['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'failed')
        self.assertEqual(documents.snapshot(self.eid)['text'],original)
        self.assertEqual(self.service.store.proposals(chat['id']),[])

    def test_templates_and_old_files_survive_retirement(self):
        from src.config import AppConfig
        old = self.root / 'library/.drafts/copywriting.json'
        old.parent.mkdir(parents=True, exist_ok=True)
        body = '{"revision":1,"fields":{"text":"旧草稿，保留"}}'
        old.write_text(body)
        cfg = AppConfig()
        cfg.copywriting_user_templates = {'rewrite':[{'id':'mine','name':'旧模板','text':'我的写稿要求'}]}
        with patch.object(AppConfig,'load',return_value=cfg):self.service.migrate()
        self.assertEqual(old.read_text(),body)
        self.assertTrue(any(t['id'].startswith('rewrite:') for t in self.service.templates()))
        self.assertTrue(any(t['text']=='我的写稿要求' for t in self.service.presets()['custom']))

    def test_retired_modules_have_no_importable_source(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        for name in ('ui/copywriting_page.py','ui/content_output_page.py','ui/text_worker.py',
                     'ui/text_history.py','ui/workspace_draft.py','ui/template_manager.py',
                     'core/copywriting.py','core/text_application.py','core/text_client.py'):
            self.assertFalse((root / name).exists(),name)


class GuideUiTests(unittest.TestCase):
    setUpClass = ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    setUp = ui.AgentWorkspaceUiTests.setUp
    tearDown = ui.AgentWorkspaceUiTests.tearDown
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js

    def test_menu_guide_search_copy_and_close_preserve_document(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id') == self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#doc-more")?.onclick)'))
        before = library.document_snapshot(self.eid)
        self.js('document.getElementById("doc-more").click()')
        labels = json.loads(self.js('JSON.stringify([...document.querySelectorAll(".lulu-menu-item")].map(b=>b.textContent))'))
        self.assertEqual(labels[labels.index('查看 Markdown 源码')+1], 'Markdown 语法指南')
        self.assertNotIn('批量处理与严格校对',labels)
        self.assertNotIn('内容输出模板',labels)
        self.js('document.getElementById("markdown-guide").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.assertEqual(self.js('document.activeElement.id'),'guide-search')
        self.assertEqual(self.js('document.querySelectorAll(".guide-entry").length'),15)
        self.js('let search=document.getElementById("guide-search");search.value="表格";search.dispatchEvent(new Event("input"))')
        self.assertEqual(self.js('document.querySelectorAll(".guide-entry:not([hidden])").length'),1)
        with patch('src.ui.agent_workspace.widget.QGuiApplication.clipboard') as clipboard:
            self.js('document.querySelector(".guide-entry:not([hidden]) button").click()')
            self.until(lambda:clipboard.return_value.setText.called)
            self.assertEqual(clipboard.return_value.setText.call_args.args[0],'| 名称 | 状态 |\n| --- | --- |\n| 文稿 | 完成 |')
        self.until(lambda:'已复制表格' in self.js('document.getElementById("guide-feedback").textContent'))
        self.js('search.value="找不到的条目";search.dispatchEvent(new Event("input"))')
        self.assertFalse(self.js('document.getElementById("guide-empty").hidden'))
        self.js('search.value="";search.dispatchEvent(new Event("input"))')
        self.assertFalse(self.js('document.body.hasAttribute("data-theme")'))
        self.assertEqual(self.js('getComputedStyle(document.getElementById("dialog")).backgroundColor'),
                         'rgb(251, 248, 241)')
        self.widget.resize(700,650)
        self.tick()
        self.assertTrue(self.js('(()=>{const r=document.getElementById("dialog").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;})()'))
        self.assertTrue(self.js('(()=>{const b=document.getElementById("dialog-body");return b.scrollHeight>b.clientHeight})()'))
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        QTest.keyClick(self.widget.view.focusProxy(),Qt.Key_Escape)
        self.until(lambda:not self.js('document.getElementById("dialog").open'))
        self.assertEqual(library.document_snapshot(self.eid),before)

    def test_copy_validation_and_retired_native_routes_have_no_effect(self):
        with patch('src.ui.agent_workspace.widget.QGuiApplication.clipboard') as clipboard:
            for args in ({'text':''},{'text':42},{'text':'x'*8001},{'text':'valid','extra':True}):
                with self.assertRaises(ValueError):self.widget.native('copy_text',args)
            clipboard.assert_not_called()
        with patch.object(self.widget,'native') as native, patch.object(self.widget.bridge,'respond') as respond:
            for action in ('processing','output'):
                self.widget.bridge.nativeAction('fixture',action,json.dumps({'document_id':self.eid}))
                self.assertFalse(respond.call_args.args[1])
            native.assert_not_called()


class RetirementShellTests(unittest.TestCase):
    setUpClass = shell.WorkspaceShellTests.__dict__['setUpClass']
    setUp = shell.WorkspaceShellTests.setUp
    tearDown = shell.WorkspaceShellTests.tearDown
    tick = shell.WorkspaceShellTests.tick
    until = shell.WorkspaceShellTests.until
    js = shell.WorkspaceShellTests.js

    def test_retired_pages_resolve_to_persistent_library(self):
        original = self.window.library_page
        for index in (3,7,3,7):
            self.window._switch_page(index)
            self.assertEqual(self.window.stack.currentIndex(),2)
            self.assertIs(self.window.library_page,original)
            self.assertIs(self.window._ensure_page_loaded(index),original)
        self.assertFalse(hasattr(self.window,'copywriting_page'))
        self.assertFalse(hasattr(self.window,'content_output_page'))
        self.assertEqual(documents.snapshot(self.eid)['title'],'好工具，应该让人忘记工具')
