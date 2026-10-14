"""Isolated Qt/WebEngine tests. No main window, credentials, installed Core or network."""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX','1')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')
from PySide6.QtCore import QEventLoop, QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from src.config import AppConfig
from src.core import library
from src.core.agent_workspace import client
from src.ui.agent_workspace.widget import AgentWorkspace


class AgentWorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patches=[patch.object(AppConfig,'load',return_value=AppConfig()),patch.object(library,'LIBRARY_DIR',self.root/'library'),patch.object(library,'FOLDERS_FILE',self.root/'library/folders.json')]
        self.patches.append(patch('src.core.agent_workspace.skills.skill_root',return_value=self.root.resolve()/'skills'))
        for p in self.patches:p.start()
        self.eid=library.save_document('我们每天都在收集内容：一段访谈、一条视频。\n\n## 先把注意力，还给内容\n\n一个好工具，应该在需要的时候出现，事情完成后退到一旁。\n\n## 让 AI 接住下一步\n\n选中一段文字，让 AI 改得更自然。','把 AI 放进每天的创作流程')
        self.widget=AgentWorkspace(dispatch=client.dispatch)
        self.widget.resize(1448,917)
        self.widget.show()
        self.until(lambda:self.widget.ready)

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close();self.widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.app.processEvents()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def tick(self):
        loop=QEventLoop();QTimer.singleShot(15,loop.quit);loop.exec()

    def until(self,fn,seconds=12):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if fn():return
            self.tick()
        self.fail('Qt workspace did not reach expected state')

    def js(self,script):
        result=[]
        self.widget.page.runJavaScript(script,lambda v:result.append(v))
        self.until(lambda:bool(result))
        return result[0]

    def use_fake_dialogue(self, generate=None):
        from src.core.agent_workspace import providers
        from src.core.agent_workspace.application import ConversationApplication
        from src.core.agent_workspace.service import WorkspaceService, handle
        from src.core.agent_workspace.store import now
        from src.core.job_ledger.service import JobControlService
        from src.core.job_ledger.store import JobLedgerStore
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
        service=WorkspaceService()
        connection=providers.save_connection(dict(provider='deepseek',mode='api',base='https://api.deepseek.com',protocol='openai',
            model='fixture',window=32768,max_output=4096,threshold=.8,credential_id='workspace-key-fixture',label='隔离模型'),service.store)
        def approve(*a,**k):
            cid=self.dialogue_cid
            p=next(p for p in service.store.proposals(cid) if p['state']=='pending')
            return dict(text='',usage={},calls=[dict(name='resolve_proposal',arguments=dict(
                proposal_id=p['id'],decision='apply',operation_ids=[o['id'] for o in p['operations']]))])
        runner=ConversationApplication(JobControlService(JobLedgerStore(self.root.resolve()/'core',FakeIntegrityAnchorStore())),
            'fixture-install',now,generate=generate or approve)
        original=self.widget.dispatch
        runtime=SimpleNamespace(workspace_application=lambda:runner)
        self.widget.dispatch=lambda operation,**arguments: handle(operation,arguments,runtime=runtime) if operation in {'send','runs','run_status','cancel_run'} else original(operation,**arguments)
        return service,connection,runner

    def send_prompt(self, text):
        self.js('document.querySelector("#prompt").value='+json.dumps(text)+';document.querySelector("#prompt").dispatchEvent(new Event("input"));document.querySelector("#prompt").dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));')

    def test_content_agent_direct_edit_undo_mode_and_cli_entry(self):
        from tests.test_agent_workspace_tools import reply
        from src.core.agent_workspace import documents
        replies=iter([reply('edit_document',dict(explanation='压缩长句，保留原意。',edits=[dict(target='current_document',action='append',
            markdown='## 创作复盘\n\n1. 先明确你想表达的观点。\n2. 用具体例子让读者理解。\n3. 留下一步可以尝试的行动。')]),'edit1'),
            dict(text='已经把三条复盘要点加入文稿末尾，原文保留。你可以查看修改内容或撤销这次修改。',calls=[],usage={})])
        service,connection,runner=self.use_fake_dialogue(lambda *a,**k:next(replies))
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        cid=self.js('window.lulu.state.chat.id');service.select_connection(cid,connection['id'])
        before=documents.snapshot(self.eid)['text']
        capture=os.environ.get('LULU_PRODUCT_CAPTURE')
        if capture:
            Path(capture).mkdir(parents=True,exist_ok=True);self.widget.grab().save(str(Path(capture)/'welcome.png'))
        self.send_prompt('在这篇稿末尾加三条简短的创作复盘要点，直接保存。')
        self.until(lambda:self.js('document.querySelector(".ProseMirror").textContent.includes("留下一步")'))
        self.assertEqual(self.js('document.querySelectorAll("[data-apply],.proposal-question").length'),0)
        self.until(lambda:self.js('Boolean(document.querySelector("[data-undo-edit]:not(:disabled)"))'))
        if capture:self.widget.grab().save(str(Path(capture)/'direct-edit.png'))
        self.js('document.querySelector("[data-undo-edit]").click()')
        self.until(lambda:documents.snapshot(self.eid)['text']==before)
        self.js('document.querySelector("#agent-mode").click()')
        self.assertTrue(self.js('window.lulu.state.options.read_only'))
        self.js('document.querySelector("#prompt-menu").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#skill-settings"))'))
        self.assertFalse(self.js('Boolean(document.querySelector("#agent-tools"))'))

    def test_selected_scope_survives_direct_edit_and_followup(self):
        from tests.test_agent_workspace_tools import reply
        from src.core.agent_workspace import documents
        calls=[]
        def generate(config, system, messages, **kwargs):
            calls.append(messages)
            if len(calls) in (1,3):
                ref=self.js_ref_id
                return reply('edit_document',dict(explanation='调整选段',edits=[dict(target=ref,action='replace',
                    markdown='选段第一次修改。' if len(calls)==1 else '选段第二次修改。')]),'edit'+str(len(calls)))
            return dict(text='已修改所选片段。',calls=[],usage={})
        service,connection,runner=self.use_fake_dialogue(generate)
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        cid=self.js('window.lulu.state.chat.id');service.select_connection(cid,connection['id'])
        doc=documents.snapshot(self.eid)
        start=doc['text'].index('我们每天');end=doc['text'].index('。',start)+1
        reference=service.reference(self.eid,doc['text'],len(doc['text'][:start].encode()),len(doc['text'][:end].encode()),doc['revision'],doc['content_hash'])
        reference['label_number']=1;self.js_ref_id=reference['id']
        self.js('window.lulu.state.refs='+json.dumps([reference])+';')
        self.send_prompt('改好这段')
        self.until(lambda:self.js('window.lulu.state.doc.text.includes("选段第一次修改。")'))
        self.until(lambda:self.js('window.lulu.state.refs[0].content_hash===window.lulu.state.doc.content_hash'))
        self.assertIn('片段一',self.js('document.querySelector("#reference-chips").textContent'))
        self.send_prompt('再自然一点')
        self.until(lambda:self.js('window.lulu.state.doc.text.includes("选段第二次修改。")'))
        after=documents.snapshot(self.eid)['text']
        self.assertEqual(after,doc['text'][:start]+'选段第二次修改。'+doc['text'][end:])
        self.assertEqual(len(calls),4)

    def test_legacy_export_title_is_single_visible_and_preserved_in_saved_markdown(self):
        original = '# 访谈录音-96kbps.mp3\n\n## 逐字稿\n\n第一段原文。\n\n第二段原文。\n'
        eid = library.save_document(original, '访谈录音', source='/local/访谈录音-96kbps.mp3')
        raw = library._load_raw(eid)
        raw['engine'] = 'local_whisper_mlx'
        library._write_raw(raw)
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.assertEqual(self.js('document.querySelector("#doc-title").textContent'), '访谈录音')
        self.assertEqual(self.js('document.querySelectorAll(".ProseMirror h1").length'), 0)
        self.assertEqual(library.document_snapshot(eid)['text'], original)
        self.assertNotIn('local_whisper', self.js('document.querySelector("#detail").innerText'))
        self.assertNotIn('/local/', self.js('document.querySelector("#list").innerText'))
        self.js('''(()=>{document.querySelector('.ProseMirror').focus();
            const node=document.querySelector('.ProseMirror p').firstChild;
            const range=document.createRange();range.selectNodeContents(node);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            document.dispatchEvent(new Event('selectionchange'));})()''')
        self.until(lambda:self.js('Boolean(window.lulu.state.selection)'))
        selection = json.loads(self.js('JSON.stringify(window.lulu.state.selection)'))
        self.assertTrue(selection['canonical'].startswith('# 访谈录音-96kbps.mp3\n\n'))
        selected = selection['canonical'].encode()[selection['start']:selection['end']].decode()
        self.assertEqual(selected, '第一段原文。')
        self.js('document.querySelector("#ask-selection").click()')
        self.until(lambda:self.js('window.lulu.state.refs.length')==1)
        self.assertEqual(self.js('document.querySelector("#prompt").value'), '片段一：')
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            document.execCommand('insertText',false,'新增');})()''')
        self.until(lambda:library.document_snapshot(eid)['text'].rstrip().endswith('新增'))
        self.assertTrue(library.document_snapshot(eid)['text'].startswith('# 访谈录音-96kbps.mp3\n\n'))
        self.js('document.querySelector("#doc-title").textContent="访谈成稿";document.querySelector("#doc-title").dispatchEvent(new Event("input"))')
        self.until(lambda:library.document_snapshot(eid)['title']=='访谈成稿')
        self.assertTrue(library.document_snapshot(eid)['text'].startswith('# 访谈成稿\n\n'))
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)
        self.widget.open_entry(eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==eid)
        self.assertEqual(self.js('document.querySelectorAll(".ProseMirror h1").length'), 0)

    def test_distinct_body_heading_is_not_treated_as_exported_title(self):
        eid = library.save_document('# 一个独立的章节\n\n保留这部分内容。', '文稿名称')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.assertEqual(self.js('document.querySelector(".ProseMirror h1").textContent'), '一个独立的章节')

    def test_analysis_lists_survive_render_edit_and_reopen(self):
        text = '## 文案结构拆解\n\n核心公式：情况加原因\n\n### 逐段拆解\n\n1. 开场钩子完整内容\n2. 行业案例完整内容\n\n### 可复用要点\n\n- 先给公式\n- 再举案例\n  - 嵌套说明\n\n- [x] 已核对\n- [ ] 待核对\n'
        eid = library.save_document(text, '列表完整性')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        for part in ['开场钩子完整内容', '行业案例完整内容', '先给公式', '嵌套说明', '已核对', '待核对']:
            self.assertIn(part, self.js('document.querySelector(".ProseMirror").innerText'))
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
            const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);
            document.execCommand('insertText',false,'补充内容');})()''')
        self.until(lambda:'补充内容' in library.document_snapshot(eid)['text'])
        for part in ['开场钩子完整内容', '行业案例完整内容', '先给公式', '嵌套说明', '[x]', '[ ]']:
            self.assertIn(part, library.document_snapshot(eid)['text'])
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)
        self.widget.open_entry(eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==eid)
        self.assertIn('行业案例完整内容', self.js('document.querySelector(".ProseMirror").innerText'))

    def test_dialogue_previews_complete_markdown_then_reply_commits_and_threads_restore(self):
        from src.core.agent_workspace import documents
        service,connection,runner=self.use_fake_dialogue()
        approval=runner.generate
        markdown='## 文案结构拆解\n\n1. **开场钩子**：保留完整分析。\n2. **行业案例**：不能丢掉列表。\n\n- 第一个可复用要点\n- 第二个可复用要点'
        def generate(config,system,messages,**kwargs):
            if messages[-1]['content']=='可以':return approval(config,system,messages,**kwargs)
            return dict(text='已根据原文整理结构。',usage={},calls=[dict(name='propose_edits',arguments=dict(
                explanation='准备追加到文稿末尾。',edits=[dict(target='current_document',action='append',markdown=markdown)]))])
        runner.generate=generate
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.dialogue_cid=self.js('window.lulu.state.chat.id')
        service.select_connection(self.dialogue_cid,connection['id'])
        before=documents.snapshot(self.eid)['text']
        self.send_prompt('分析结构')
        self.until(lambda:self.js('Boolean(document.querySelector(".conversation-proposal"))'))
        self.assertEqual(documents.snapshot(self.eid)['text'],before)
        self.assertEqual(self.js('document.querySelectorAll(".ai-result li").length'),4)
        self.assertEqual(self.js('document.querySelectorAll("[data-apply],[data-apply-all]").length'),0)
        self.assertEqual(self.js('document.querySelectorAll(".proposal-question").length'),0)
        self.until(lambda:self.js('document.querySelector("#send").getAttribute("aria-label").startsWith("发送")'))
        self.send_prompt('可以')
        self.until(lambda:self.js('document.querySelector(".ProseMirror").textContent.includes("第二个可复用要点")'))
        self.assertEqual(documents.snapshot(self.eid)['text'],before+'\n\n'+markdown)
        self.assertIn('已按你的回复写入',self.js('document.querySelector("#chat-scroll").textContent'))
        self.js('document.querySelector("#prompt").value="未发送的草稿";document.querySelector("#prompt").dispatchEvent(new Event("input"));document.querySelector("#new-conversation").click()')
        self.until(lambda:self.js('window.lulu.state.chat.id')!=self.dialogue_cid)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'')
        self.assertEqual(self.js('window.lulu.state.messages.length'),0)
        self.js('document.querySelector("#conversations").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#conversation-search"))'))
        self.js('document.querySelector("#conversation-search").value="分析结构";document.querySelector("#conversation-search").dispatchEvent(new Event("input"))')
        self.assertEqual(self.js('document.querySelectorAll("[data-chat]").length'),1)
        self.js('document.querySelector("[data-chat]").click()')
        self.until(lambda:self.js('window.lulu.state.chat.id')==self.dialogue_cid)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'未发送的草稿')
        self.assertIn('已按你的回复写入',self.js('document.querySelector("#chat-scroll").textContent'))

    def test_composer_ime_shift_enter_and_skills_settings(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        for options in [{'shiftKey':True},{'isComposing':True},{'keyCode':229}]:
            self.js('document.querySelector("#prompt").value="测试";document.querySelector("#prompt").dispatchEvent(new KeyboardEvent("keydown",'+json.dumps(dict(key='Enter',bubbles=True,**options))+'))')
            self.assertEqual(self.js('window.lulu.state.messages.length'),0)
        self.js('document.querySelector("#prompt-menu").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#skill-settings"))'))
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.widget.main = SimpleNamespace(_switch_page=Mock(), settings_page=SimpleNamespace(set_category=Mock()))
        self.js('document.querySelector("#skill-settings").click()')
        self.until(lambda:self.widget.main._switch_page.called)
        self.widget.main._switch_page.assert_called_once_with(6)
        self.widget.main.settings_page.set_category.assert_called_once_with('skills')
        self.widget.main = None

    def test_text_only_model_label_and_late_switch_stay_with_original_conversation(self):
        import threading
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import providers
        service = WorkspaceService()
        connection = providers.save_connection(dict(provider='custom', mode='api', base='https://example.test/v1',
            protocol='openai', model='text-model', window=8192, max_output=1024, threshold=.8,
            credential_id='workspace-key-test', label='文字模型'), service.store)
        rows = service.store.preference('connections')
        rows[0]['tools_supported'] = False
        service.store.preference('connections', rows)
        second = library.save_document('另一篇正文', '另一篇')
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("#model"))'))
        first_id = self.js('window.lulu.state.chat.id')
        second_chat = service.new_conversation(second, 'model-switch-second')
        self.js('document.getElementById("model").click()')
        self.until(lambda: self.js('document.getElementById("model-menu").innerText.includes("仅文字")'))
        started, release = threading.Event(), threading.Event()
        dispatch = self.widget.dispatch
        def delayed(operation, **arguments):
            if operation == 'select_connection':
                started.set()
                if not release.wait(5):
                    raise RuntimeError('test did not release model selection')
            return dispatch(operation, **arguments)
        self.widget.dispatch = delayed
        try:
            self.js("document.querySelector("+json.dumps('[data-connection="'+connection['id']+'"]')+").click()")
            self.until(started.is_set)
            self.widget.open_entry(second)
            self.until(lambda: self.js('window.lulu.state.doc?.id') == second)
            release.set()
            self.until(lambda: service.store.get(first_id)['connection_id'] == connection['id'])
            for _ in range(4): self.tick()
            self.assertEqual(self.js('window.lulu.state.chat.id'), second_chat['id'])
            self.assertEqual(service.store.get(second_chat['id'])['connection_id'], second_chat['connection_id'])
            self.widget.open_entry(self.eid)
            self.until(lambda: self.js('document.getElementById("model")?.title.includes("采集")'))
        finally:
            release.set()
            self.widget.dispatch = dispatch

    def test_load_edit_autosave_and_document_switch(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        self.assertEqual(self.js('window.lulu.state.doc.id'),self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#model"))'))
        self.js('document.querySelector("#model").click()')
        self.until(lambda: self.js('document.querySelector("#model-menu").innerText.includes("还没有配置模型")'))
        self.js('document.querySelector("#model").click()')
        self.js('document.querySelector("#doc-title").textContent="修改后的标题";document.querySelector("#doc-title").dispatchEvent(new Event("input",{bubbles:true}));')
        self.until(lambda:library.document_snapshot(self.eid)['title']=='修改后的标题')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc === null'))
        self.assertTrue(self.js('document.querySelector("#detail").innerText.includes("选择一篇")'))

    def test_enabled_skills_insert_explicit_choice_without_sending(self):
        from src.core.agent_workspace.skills import catalog
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        for entry in catalog()['entries']:
            self.js('document.querySelector("#prompt").value="我的要求";document.querySelector("#prompt-menu").click();')
            selector='[data-skill="'+entry['name']+'"]'
            self.until(lambda:self.js('Boolean(document.querySelector('+json.dumps(selector)+'))'))
            self.assertFalse(self.js('Boolean(document.querySelector("#add-preset,#manage-presets,#builtin-templates,#processing-options"))'))
            self.js('document.querySelector('+json.dumps(selector)+').click();')
            self.assertEqual(self.js('document.querySelector("#prompt").value'),'/' + entry['name'] + ' 我的要求')
            self.assertEqual(self.js('window.lulu.state.messages.length'),0)

    def test_completed_skill_draft_has_working_settings_handoff(self):
        from unittest.mock import Mock
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import tools
        service=WorkspaceService()
        chat=service.new_conversation(self.eid,'skill-handoff-chat')
        call=dict(name='create_skill_draft',id='draft-call',arguments=dict(name='custom-flow',description='用户方法',instructions='保留事实。'))
        result=tools.execute(call,dict(options={}))
        service.store.append(chat['id'],'tool',result['message'],dict(job_id='workspace-job-draft',calls=[call],results=[result]),message_id='draft-message')
        skills_page=SimpleNamespace(request_pending=Mock())
        self.widget.main=SimpleNamespace(_switch_page=Mock(),settings_page=SimpleNamespace(set_category=Mock(),skills_page=skills_page))
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("[data-handoff-message]"))'))
        self.js('document.querySelector("[data-handoff-message]").click()')
        self.until(lambda:skills_page.request_pending.called)
        self.widget.main._switch_page.assert_called_once_with(6)
        self.widget.main.settings_page.set_category.assert_called_once_with('skills')
        self.assertEqual(self.js('window.lulu.state.chat.id'),chat['id'])
        self.widget.main=None

    def test_frozen_tool_handoff_reaches_voice_page_without_running_generation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import tools, documents
        service=WorkspaceService()
        chat=service.new_conversation(self.eid,'handoff-chat')
        frozen=documents.snapshot(self.eid)
        request=dict(document=frozen,references=[])
        call=dict(name='prepare_voice',id='voice-call',arguments={})
        result=tools.execute(call,request)
        service.store.append(chat['id'],'tool',result['message'],dict(job_id='workspace-job-voice',
            calls=[call],results=[result]),message_id='handoff-message')
        self.widget.main=SimpleNamespace(open_voice_studio=Mock())
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("[data-handoff-message]"))'))
        self.js('document.querySelector("[data-handoff-message]").click();')
        self.until(lambda:self.widget.main.open_voice_studio.called)
        args=self.widget.main.open_voice_studio.call_args
        self.assertEqual(args.args[0],frozen['text'])
        self.assertEqual(args.kwargs['source']['source_id'],self.eid)
        self.assertEqual(args.kwargs['source']['source_chain'][0]['revision'],frozen['revision'])
        self.assertEqual(self.js('window.lulu.state.chat.id'),chat['id'])

    def test_corrupt_database_has_working_recovery_entry(self):
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import recovery
        service=WorkspaceService()
        chat=service.new_conversation(self.eid,'recover-ui')
        service.store.append(chat['id'],'user','恢复后仍能看到的要求')
        backup=service.backup()
        recovery.database_path().write_bytes(b'broken-ui-database')
        self.js('window.location.reload();')
        self.until(lambda:self.js('Boolean(document.querySelector("#recover-workspace"))'))
        self.js('document.querySelector("#recover-workspace").click();')
        selector='[data-restore-backup="'+backup['id']+'"]'
        self.until(lambda:self.js('Boolean(document.querySelector('+json.dumps(selector)+'))'))
        self.js('document.querySelector('+json.dumps(selector)+').click();')
        self.until(lambda:self.js('Boolean(document.querySelector("#confirm-restore"))'))
        self.assertIn('当前文稿正文保持原样',self.js('document.querySelector("#dialog-body").innerText'))
        self.js('document.querySelector("#confirm-restore").click();')
        self.until(lambda: WorkspaceService().storage_status()['state'] == 'ready')
        self.until(lambda:self.js('Boolean(window.lulu?.ready)'))
        self.assertEqual(WorkspaceService().messages(chat['id'])[0]['content'],'恢复后仍能看到的要求')

    def test_three_columns_have_independent_scroll_and_no_preview_text(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.assertNotIn('我们每天',self.js('document.querySelector("#list").innerText'))
        widths=json.loads(self.js('JSON.stringify(["list-pane","detail","agent-column"].map(id=>document.getElementById(id).getBoundingClientRect().width))'))
        self.assertEqual(round(sum(widths)+10),1448)
        self.assertGreater(widths[1],600)
        self.assertEqual(self.js('document.querySelector("#prompt").getBoundingClientRect().height'),78)
        output=Path(__file__).resolve().parents[2]/'scratch/v37-workspace-qt.png'
        self.widget.grab().save(str(output))

    def test_markdown_count_and_quote_spacing_match_the_visible_document(self):
        from src.core.document_metrics import visible_characters
        text = '## 标题\n\n> **你好** Lulu 🙂\n\n后文'
        library.save_markdown(self.eid, text)
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror blockquote p"))'))
        self.until(lambda:self.js('window.lulu.state.loading === false'))
        expected = f'{visible_characters(text)} 字'
        self.until(lambda:self.js('document.querySelector("#words").textContent') == expected)
        self.assertEqual(self.js('document.querySelector(".entry .row-words").textContent'), expected)
        self.assertEqual(self.js('getComputedStyle(document.querySelector(".ProseMirror blockquote p")).marginBottom'), '0px')
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            document.execCommand('insertText',false,'新增');})()''')
        expected = f'{visible_characters(text) + 2} 字'
        self.until(lambda:self.js('document.querySelector("#words").textContent') == expected)
        self.until(lambda:self.js('document.querySelector(".entry .row-words").textContent') == expected)
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('document.querySelector("#words")?.textContent') == expected)

    def test_multiple_references_keep_numbered_instructions_and_draft(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        def select(index):
            self.js(f'''(()=>{{document.querySelector('.ProseMirror').focus();const node=document.querySelectorAll('.ProseMirror p')[{index}].firstChild;
                const range=document.createRange();range.setStart(node,0);range.setEnd(node,node.textContent.length);
                const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
                document.dispatchEvent(new Event('selectionchange'));}})()''')
            try:
                self.until(lambda:self.js('Boolean(window.lulu.state.selection)'))
            except AssertionError:
                self.fail(self.js('JSON.stringify({dom:window.getSelection().toString(),focus:document.activeElement.className,pm:document.querySelector(".ProseMirror").pmViewDesc?.view?.state.selection.toJSON(),state:window.lulu.state.selection})'))
            self.js('document.getElementById("ask-selection").click()')
        select(0)
        self.until(lambda:self.js('window.lulu.state.refs.length')==1)
        self.assertEqual(self.js('document.getElementById("prompt").value'),'片段一：')
        self.js('document.getElementById("prompt").value+="改短一点";document.getElementById("prompt").dispatchEvent(new Event("input"));window.lulu.state.selection=null;')
        select(2)
        self.until(lambda:self.js('window.lulu.state.refs.length')==2)
        self.assertEqual(self.js('document.getElementById("prompt").value'),'片段一：改短一点\n片段二：')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.getElementById("prompt"))'))
        self.assertEqual(self.js('document.getElementById("prompt").value'),'片段一：改短一点\n片段二：')
        self.assertEqual(self.js('window.lulu.state.refs.length'),2)

    def test_immediate_switch_keeps_last_body_keystroke(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
            const s=window.getSelection();s.removeAllRanges();s.addRange(range);
            document.execCommand('insertText',false,'LAST-EDIT');})()''')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.assertIn('LAST-EDIT',library.document_snapshot(self.eid)['text'])

    def test_cross_paragraph_reference_manager_and_locate_survive_reopen(self):
        eid=library.save_document('重复的一句话。\n\n重复的一句话。\n\n第三段保留。','重复文字定位')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const nodes=el.querySelectorAll('p'), r=document.createRange();
            r.setStart(nodes[1].firstChild,0);r.setEnd(nodes[2].firstChild,nodes[2].textContent.length);
            const s=window.getSelection();s.removeAllRanges();s.addRange(r);document.dispatchEvent(new Event('selectionchange'));})()''')
        self.until(lambda:self.js('window.lulu.state.selection?.ranges?.length')==1)
        self.js('document.querySelector("#ask-selection").click()')
        self.until(lambda:self.js('window.lulu.state.refs.length')==1)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'片段一：')
        self.assertEqual(self.js('document.querySelector("#manage-references").textContent'),'共 1 段')
        before=self.js('document.querySelector(".composer").getBoundingClientRect().height')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector("#manage-references").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("[data-locate]"))'))
        self.js('document.querySelector("[data-locate]").click()')
        self.until(lambda:self.js('!document.querySelector("#dialog").open'))
        self.assertIn('重复的一句话。',self.js('window.getSelection().toString()'))
        self.assertIn('第三段保留。',self.js('window.getSelection().toString()'))
        self.assertEqual(''.join(self.js('window.getSelection().toString()').split()), '重复的一句话。第三段保留。')
        self.js('document.querySelector("#prompt").value="片段一：精简";document.querySelector("#prompt").dispatchEvent(new Event("input"));document.querySelector("#manage-references").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#clear-references"))'))
        self.js('document.querySelector("#clear-references").click()')
        self.until(lambda:self.js('window.lulu.state.refs.length')==0)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'片段一：精简')
        self.assertEqual(self.js('window.lulu.state.nextRef'),2)
        self.assertEqual(self.js('document.querySelector(".composer").getBoundingClientRect().height'),before)

    def test_ai_extended_markdown_uses_source_editor_and_preserves_syntax(self):
        from src.core.agent_workspace import documents
        from src.core.agent_workspace.service import WorkspaceService
        service,connection,runner=self.use_fake_dialogue()
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        self.dialogue_cid=self.js('window.lulu.state.chat.id')
        service.select_connection(self.dialogue_cid,connection['id'])
        doc=documents.snapshot(self.eid)
        markdown='---\ntitle: 新属性\n---\n<details><summary>完整内容</summary>\n正文\n</details>\n'
        service.proposal_plan(self.js('window.lulu.state.chat.id'),'extended-proposal',doc['revision'],doc['content_hash'],[
            dict(id='extended-edit',action='replace',start=0,end=len(doc['text'].encode()),before_hash=doc['content_hash'],markdown=markdown)])
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".conversation-proposal"))'))
        self.send_prompt('可以')
        self.until(lambda:self.js('Boolean(document.querySelector(".editor-source"))'))
        self.assertEqual(self.js('document.querySelector(".editor-source").value'),markdown)
        self.js('document.querySelector(".editor-source").value+="补充";document.querySelector(".editor-source").dispatchEvent(new Event("input"));')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.assertEqual(documents.snapshot(self.eid)['text'],markdown+'补充')

    def test_native_commands_preserve_kind_then_open_order(self):
        self.widget._set_kind('voice')
        self.widget._set_kind('transcript')
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==self.eid)
        self.assertEqual(self.js('window.lulu.state.kind'),'documents')
        self.assertTrue(self.js('Boolean(document.querySelector(".ProseMirror"))'))

    def test_frontmatter_survives_direct_edit(self):
        prefix='---\ntitle: 资料\ncustom: [one, two]\n---\n'
        eid=library.save_document(prefix+'正文。\n','包含属性')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        self.js('''(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const range=document.createRange();range.selectNodeContents(el);range.collapse(false);
            const s=window.getSelection();s.removeAllRanges();s.addRange(range);
            document.execCommand('insertText',false,'补充');})()''')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.assertTrue(library.document_snapshot(eid)['text'].startswith(prefix))

    def test_unsupported_markdown_stays_editable_without_losing_html(self):
        eid=library.save_document('<details><summary>保留的内容</summary>\n内文\n</details>\n','原始语法')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".editor-source"))'))
        self.js('const source=document.querySelector(".editor-source");source.value+="补充";source.dispatchEvent(new Event("input"));')
        self.widget.call('open',None)
        self.until(lambda:self.js('window.lulu.state.doc===null'))
        self.assertIn('<details><summary>保留的内容</summary>',library.document_snapshot(eid)['text'])




    def test_apply_ai_edit_is_one_native_undo_and_preserves_current_revision(self):
        from src.core.agent_workspace import documents
        from src.core.agent_workspace.service import WorkspaceService
        service,connection,runner=self.use_fake_dialogue()
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        doc=documents.snapshot(self.eid)
        cid=self.js('window.lulu.state.chat.id')
        self.dialogue_cid=cid
        service.select_connection(cid,connection['id'])
        service.proposal_plan(cid,'undo-proposal',doc['revision'],doc['content_hash'],[
            dict(id='edit1',action='replace',start=0,end=len(doc['text'].encode()),before_hash=doc['content_hash'],markdown='修改后的正文。')])
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".conversation-proposal"))'))
        self.send_prompt('可以')
        self.until(lambda:self.js('document.querySelector(".ProseMirror").textContent')=='修改后的正文。')
        self.js('document.querySelector(".ProseMirror").dispatchEvent(new KeyboardEvent("keydown",{key:"z",code:"KeyZ",metaKey:true,bubbles:true}));')
        self.until(lambda:self.js('document.querySelector(".ProseMirror").textContent.includes("我们每天")'))
        self.until(lambda:'我们每天' in documents.snapshot(self.eid)['text'])

    def test_model_settings_navigation_preserves_document_and_conversation(self):
        from src.core.agent_workspace import documents
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        before=documents.snapshot(self.eid)
        conversation=self.js('window.lulu.state.chat.id')
        opened=[]
        self.widget.main=SimpleNamespace(_open_workspace_models=lambda:opened.append(True))
        self.widget.call('showModels')
        self.until(lambda:bool(opened))
        self.assertEqual(documents.snapshot(self.eid)['content_hash'],before['content_hash'])
        self.assertEqual(self.js('window.lulu.state.chat.id'),conversation)
        self.assertFalse(self.js('Boolean(document.querySelector("#cloud-settings"))'))
        self.widget.main=None

    def test_overlapping_reference_extends_existing_label_without_replacing_instruction(self):
        body=library.document_snapshot(self.eid)['text']
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        for start,end in [(0,6),(3,12)]:
            self.js('window.lulu.state.selection='+json.dumps(dict(canonical=body,start=start,end=end))+';document.querySelector("#ask-selection").click()')
            self.until(lambda:self.js('window.lulu.state.refs[0]?.end')==end)
            if end==6:self.js('document.querySelector("#prompt").value+="精简";document.querySelector("#prompt").dispatchEvent(new Event("input"));')
        self.assertEqual(self.js('window.lulu.state.refs.length'),1)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'片段一：精简')

    def test_external_saved_edit_is_detected_without_overwriting_local_input(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror"))'))
        library.markdown_path(self.eid).write_text('其他入口保存的正文。',encoding='utf-8')
        self.until(lambda:self.js('document.querySelector(".ProseMirror").textContent')=='其他入口保存的正文。')
        self.assertEqual(self.js('window.lulu.state.dirty'),False)

    def test_new_input_typed_during_send_is_not_cleared_by_late_ack(self):
        import threading
        started,finish=threading.Event(),threading.Event()
        self.addCleanup(finish.set)
        original=self.widget.dispatch
        cid=[None]
        def dispatch(operation,**args):
            if operation=='send':
                cid[0]=args['conversation_id'];started.set();finish.wait(4)
                return dict(job_id='fixture-send',state='succeeded',conversation_id=cid[0])
            if operation=='run_status':return dict(job_id='fixture-send',state='succeeded',conversation_id=cid[0])
            return original(operation,**args)
        self.widget.dispatch=dispatch
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector("#prompt").value="第一条要求";document.querySelector("#prompt").dispatchEvent(new Event("input"));document.querySelector("#send").click();')
        self.until(started.is_set)
        self.js('document.querySelector("#prompt").value="我又输入的第二条要求";document.querySelector("#prompt").dispatchEvent(new Event("input"));')
        finish.set()
        self.until(lambda:self.js('window.lulu.state.outbox===null'))
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'我又输入的第二条要求')

    def test_stream_updates_one_bubble_and_removes_it_after_completion(self):
        import threading
        finished=threading.Event();stop_requested=threading.Event();self.addCleanup(finished.set)
        original=self.widget.dispatch;polls=[];states=[];cid=[None]
        def dispatch(operation,**args):
            if operation=='send':
                cid[0]=args['conversation_id']
                return dict(job_id='stream-fixture',state='running',conversation_id=cid[0])
            if operation=='conversation_snapshot':
                result=original(operation,**args)
                if finished.is_set():
                    result['messages'].append(dict(id='stream-fixture-assistant',sequence=100000,role='assistant',
                        content='模拟的完成回复',payload={'job_id':'stream-fixture'}))
                return result
            if operation=='run_status':
                polls.append(1)
                index=len(polls)
                phase='complete' if finished.is_set() else 'stopping' if stop_requested.is_set() else 'waiting_first_token' if index==1 else 'streaming'
                state='succeeded' if finished.is_set() else 'cancel_requested' if stop_requested.is_set() else 'running'
                states.append((state,finished.is_set()))
                return dict(job_id='stream-fixture',state=state,conversation_id=cid[0],phase=phase,
                            partial='' if index==1 else '正在生成'+str(index))
            return original(operation,**args)
        self.widget.dispatch=dispatch
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector("#prompt").value="继续写";document.querySelector("#send").click()')
        self.until(lambda:self.js('document.querySelector("[data-live-job] .live-status")?.dataset.phase')=='waiting_first_token')
        self.assertEqual(self.js('document.querySelector("[data-live-job] .live-status").textContent'),'正在思考…')
        self.assertEqual(self.js('document.querySelector("[data-live-job] .message-text").textContent'),'')
        self.until(lambda:self.js('document.querySelector("[data-live-job] .live-status")?.dataset.phase')=='streaming')
        self.assertIn('正在生成',self.js('document.querySelector("[data-live-job]").innerText'))
        self.assertEqual(self.js('document.querySelectorAll("[data-live-job]").length'),1)
        stop_requested.set()
        self.until(lambda:self.js('document.querySelector("[data-live-job] .live-status")?.textContent')=='正在停止…')
        finished.set()
        try:
            self.until(lambda:self.js('document.querySelectorAll("[data-live-job]").length')==0,seconds=3)
        except AssertionError:
            feedback=self.js('JSON.stringify({toast:document.querySelector("#toast")?.textContent,live:document.querySelector("[data-live-job]")?.outerHTML})')
            self.fail(f'终态任务未退出，最近状态：{states[-4:]}，轮询次数：{len(polls)}，界面：{feedback}')
        self.assertEqual(self.js('document.querySelector("[data-message-id=stream-fixture-assistant] .live-status")?.textContent'),'本轮已完成')

    def test_send_button_keeps_pointer_hover_and_gets_press_feedback(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#send"))'))
        button=json.loads(self.js('(()=>{const b=document.querySelector("#send");return JSON.stringify({cursor:getComputedStyle(b).cursor,reduced:matchMedia("(prefers-reduced-motion: reduce)").matches})})()'))
        self.assertEqual(button['cursor'],'pointer')
        css=(Path(__file__).parents[1]/'src/ui/agent_workspace/assets/local.css').read_text()
        self.assertIn('.send:hover:not(:disabled),.primary:hover:not(:disabled){filter:brightness(.93)}',css)
        self.assertIn('.send.press-flash:not(:disabled)',css)
        self.assertIn('.primary:hover:not(:disabled){filter:brightness(.93)}',css)
        self.js('document.querySelector("#send").focus();document.querySelector("#send").dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))')
        self.assertIn('press-flash',self.js('document.querySelector("#send").className'))
        expected='none' if button['reduced'] else 'send-press-flash'
        self.assertEqual(self.js('getComputedStyle(document.querySelector("#send")).animationName'),expected)

    def test_older_conversation_pages_remain_readable(self):
        from src.core.agent_workspace.service import WorkspaceService
        service=WorkspaceService();chat=service.new_conversation(self.eid,'long-chat')
        for i in range(110):service.store.append(chat['id'],'user' if i%2==0 else 'assistant',f'第{i}条消息')
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#older-messages"))'))
        self.assertEqual(self.js('document.querySelectorAll(".chat-message").length'),100)
        self.js('document.querySelector("#older-messages").click()')
        self.until(lambda:self.js('document.querySelectorAll(".chat-message").length')==110)
        self.assertIn('第0条消息',self.js('document.querySelector("#chat-scroll").innerText'))

    def test_media_picker_checks_off_main_thread_and_runs_only_after_confirmation(self):
        import threading
        from src.core.agent_workspace.service import WorkspaceService
        service = WorkspaceService()
        chat = service.new_conversation(self.eid, 'media-chat')
        service.store.append(chat['id'], 'tool', '请选择本地文件', {
            'calls': [dict(id='transcribe-call', name='prepare_transcription', arguments={'url': ''})],
            'results': [dict(state='needs_input', action='transcription', url='', label='前往文案提取')]})
        gui_thread = threading.get_ident()
        calls, state = [], {'state': 'running', 'progress': 40}
        result_id = library.save_document('这是后台转录结果。', '新访谈')
        jid = 'workspace-media-ui'
        def task():
            return dict(job_id=jid, conversation_id=chat['id'], filename='访谈.wav', engine='local_sensevoice',
                state=state['state'], progress=state['progress'], recoverable=False,
                document_id=result_id if state['state'] == 'succeeded' else None)
        original = self.widget.dispatch
        def dispatch(operation, **args):
            if operation == 'prepare_media':
                self.assertNotEqual(threading.get_ident(), gui_thread)
                calls.append((operation, args))
                return dict(plan_id='media-ui', plan_hash='a' * 64, conversation_id=chat['id'],
                            filename='访谈.wav', bytes=2048, engine='local_sensevoice', message='本地 SenseVoice，另存新文稿。')
            if operation == 'start_media':
                calls.append((operation, args))
                service.store.append(chat['id'], 'status', '正在转录 · SenseVoice', {'media_job_id': jid}, message_id=jid+'-started')
                return task()
            if operation == 'media_tasks': return []
            if operation == 'media_status': return task()
            return original(operation, **args)
        self.widget.dispatch = dispatch
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("[data-choose-media]"))'))
        with patch('src.ui.agent_workspace.widget.QFileDialog.getOpenFileName', return_value=(str(self.root / '访谈.wav'), '')):
            self.js('document.querySelector("[data-choose-media]").click()')
            self.until(lambda: self.js('Boolean(document.querySelector("#media-start"))'))
        self.assertEqual([op for op, _ in calls], ['prepare_media'])
        self.assertEqual(calls[0][1]['engine'], 'local_sensevoice')
        self.js('document.querySelector("#media-start").click()')
        self.until(lambda: self.js('document.querySelector("#chat-scroll").textContent.includes("40%")'))
        state['progress'] = 70
        self.until(lambda: self.js('document.querySelector("#chat-scroll").textContent.includes("70%")'))
        self.assertEqual(calls[1][1], {'plan_id': 'media-ui', 'expected_hash': 'a'*64})
        self.assertEqual(self.js('Math.round(document.querySelector("#prompt").getBoundingClientRect().height)'), 78)
        state['state'] = 'succeeded'
        self.until(lambda: self.js('Boolean(document.querySelector("[data-media-document]"))'))
        self.js('document.querySelector("[data-media-document]").click()')
        self.until(lambda: self.js('window.lulu.state.doc.id') == result_id)

    def test_reopened_media_task_can_be_cancelled_from_its_conversation(self):
        from src.core.agent_workspace.service import WorkspaceService
        service = WorkspaceService()
        chat = service.new_conversation(self.eid, 'media-cancel-chat')
        jid = 'workspace-media-cancel'
        service.store.append(chat['id'], 'status', '正在转录', {'media_job_id': jid}, message_id=jid+'-started')
        task = dict(job_id=jid, conversation_id=chat['id'], state='running', progress=20,
                    document_id=None, recoverable=False, filename='录音.wav')
        cancelled = []
        original = self.widget.dispatch
        def dispatch(operation, **args):
            if operation == 'media_tasks': return [task] if args['conversation_id'] == chat['id'] else []
            if operation == 'media_status': return task
            if operation == 'cancel_media':
                cancelled.append(args['job_id']); task['state'] = 'cancelled'
                return task
            return original(operation, **args)
        self.widget.dispatch = dispatch
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("[data-cancel-media]"))'))
        self.js('document.querySelector("[data-cancel-media]").click()')
        self.until(lambda: self.js('document.querySelector("#chat-scroll").textContent.includes("已停止")'))
        self.assertEqual(cancelled, [jid])
        self.assertFalse(self.js('Boolean(document.querySelector("[data-media-document]"))'))

    def test_legacy_break_and_timestamp_selection_preserve_exact_source(self):
        original = '# 导入文稿\n\n<br />\n\n## 逐字稿\n\n' + ''.join(
            f'**[{i//60:02d}:{i%60:02d}]** 重复的中文记录，保留每一处引用的位置。\n\n' for i in range(200))
        eid = library.save_document(original, '导入文稿')
        self.widget.open_entry(eid)
        self.until(lambda:self.js('Boolean(document.querySelector(".ProseMirror p"))'))
        self.assertFalse(self.js('Boolean(document.querySelector(".editor-source"))'))
        self.assertEqual(self.js('document.querySelector(".ProseMirror p").textContent'), "")
        self.js("""(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const p=[...el.querySelectorAll('p')].filter(p=>p.querySelector('strong')),r=document.createRange();
            r.setStart(p[100],0);r.setEnd(p[102],p[102].childNodes.length);
            const s=window.getSelection();s.removeAllRanges();s.addRange(r);
            document.dispatchEvent(new Event('selectionchange'));})()""")
        self.until(lambda:self.js('window.lulu.state.selection?.ranges.length')==1)
        self.js('document.querySelector("#ask-selection").click()')
        self.until(lambda:self.js('window.lulu.state.refs.length')==1)
        ref=json.loads(self.js('JSON.stringify(window.lulu.state.refs[0])'))
        self.assertTrue(ref['markdown'].startswith('**[01:40]**'))
        self.assertIn('[01:40]',ref['markdown'])
        self.assertIn('[01:42]',ref['markdown'])
        self.assertEqual(ref['markdown'],original.encode()[ref['start']:ref['end']].decode())
        self.assertEqual(library.document_snapshot(eid)['text'],original)
        self.assertEqual(self.js('document.querySelector("#prompt").value'),'片段一：')
        self.js("""(()=>{const el=document.querySelector('.ProseMirror');el.focus();
            const r=document.createRange();r.selectNodeContents(el);r.collapse(false);
            const s=window.getSelection();s.removeAllRanges();s.addRange(r);
            document.execCommand('insertText',false,'补充');})()""")
        self.until(lambda:library.document_snapshot(eid)['text'].rstrip().endswith('补充'))
        self.assertIn('<br />',library.document_snapshot(eid)['text'])

    def test_composer_minimal_and_model_menu_keyboard_layout(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#model"))'))
        self.assertEqual(self.js('document.querySelector("#context-percent").textContent'),'')
        self.assertFalse(self.js('Boolean(document.querySelector("#context-note"))'))
        self.assertIn('Songti',self.js('getComputedStyle(document.querySelector(".ProseMirror")).fontFamily'))
        before=self.js('document.querySelector(".composer").getBoundingClientRect().height')
        self.js('document.querySelector("#model").click()')
        self.assertEqual(self.js('document.querySelector("#model").getAttribute("aria-expanded")'),'true')
        self.assertEqual(self.js('document.activeElement.id'),'model-settings')
        self.js('document.querySelector("#model-menu").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
        self.assertTrue(self.js('document.querySelector("#model-menu").hidden'))
        self.assertEqual(self.js('document.activeElement.id'),'model')
        self.assertEqual(self.js('document.querySelector(".composer").getBoundingClientRect().height'),before)

    def test_model_menu_refreshes_saved_enabled_choices_without_reopening_document(self):
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import providers
        service = WorkspaceService()
        def add(model):
            return providers.save_connection(dict(provider='custom', mode='api', base='https://example.test/v1',
                protocol='openai', model=model, window=32768, max_output=4096, threshold=.8,
                credential_id='workspace-key-isolated', label=model), service.store)
        first = add('first-model')
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('typeof document.querySelector("#model")?.onclick') == 'function')
        cid = self.js('window.lulu.state.chat.id')
        second = add('second-model')
        def open_menu():
            self.until(lambda: not self.js('document.querySelector("#model").disabled'))
            self.js('document.querySelector("#model").click()')
            self.until(lambda: self.js('document.querySelector("#model-menu").getAttribute("aria-busy")') != 'true')
        open_menu()
        self.assertEqual(json.loads(self.js('JSON.stringify(Array.from(document.querySelectorAll("[data-connection]")).map(n=>n.dataset.connection))')),
                         [first['id'], second['id']])
        self.js('document.querySelector('+json.dumps('[data-connection="'+second['id']+'"]')+').click()')
        self.until(lambda: service.store.get(cid)['connection_id'] == second['id'])
        self.until(lambda: self.js('document.querySelector("#model")?.innerText') == 'second-model')
        service.store.preference('model-managed-connections', [first['id'], second['id']])
        service.store.preference('model-connection-groups', {'isolated': [first['id']]})
        open_menu()
        self.assertEqual(json.loads(self.js('JSON.stringify(Array.from(document.querySelectorAll("[data-connection]")).map(n=>n.dataset.connection))')),
                         [first['id']])
        self.assertEqual(service.store.get(cid)['connection_id'], second['id'])
        self.assertEqual(self.js('document.querySelector("#model")?.innerText'), 'second-model')

    def test_many_connected_models_have_bounded_scrollable_menu_and_search(self):
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import providers
        service = WorkspaceService()
        choices = [providers.save_connection(dict(provider='custom', mode='api', base='https://example.test/v1',
            protocol='openai', model=f'connected-{i:02}', window=32768, max_output=4096, threshold=.8,
            credential_id='workspace-key-isolated', label=f'已接入模型 {i:02}' + (' 很长的模型名称' * 8 if i == 18 else '')),
            service.store) for i in range(20)]
        service.store.preference('model-managed-connections', [c['id'] for c in choices])
        service.store.preference('model-connection-groups', {'isolated': [c['id'] for c in choices[:-1]]})
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("#model"))'))
        for width, height in [(1448, 917), (1200, 680)]:
            with self.subTest(width=width, height=height):
                self.widget.resize(width, height)
                for _ in range(4): self.tick()
                self.js('document.querySelector("#model-menu").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
                self.js('document.querySelector("#model").click()')
                self.until(lambda: self.js('Boolean(document.querySelector("#model-search"))'))
                self.until(lambda: self.js('document.querySelector("#model-menu").getAttribute("aria-busy")') != 'true')
                self.assertEqual(self.js('document.querySelectorAll("[data-connection]").length'), 19)
                sizes = json.loads(self.js('JSON.stringify((()=>{const m=document.querySelector("#model-menu"),o=m.querySelector(".model-options"),r=m.getBoundingClientRect();return {height:r.height,width:r.width,top:r.top,bottom:r.bottom,listHeight:o.clientHeight,contentHeight:o.scrollHeight,listWidth:o.clientWidth,contentWidth:o.scrollWidth};})())'))
                self.assertLessEqual(sizes['height'], 420)
                self.assertLessEqual(sizes['width'], 310)
                self.assertGreaterEqual(sizes['top'], 0)
                self.assertLessEqual(sizes['bottom'], height)
                row_height = self.js('document.querySelector(".model-option").getBoundingClientRect().height')
                self.assertLessEqual(sizes['listHeight'], 3 * row_height + 1)
                self.assertAlmostEqual(sizes['listHeight'], 3 * row_height, delta=1)
                self.assertEqual(self.js('document.querySelector(".model-option:last-child").getBoundingClientRect().height'), row_height)
                self.assertGreater(sizes['contentHeight'], sizes['listHeight'])
                self.assertLessEqual(sizes['contentWidth'], sizes['listWidth'])
                self.js('document.querySelector(".model-options").scrollTop=100000')
                self.assertGreater(self.js('document.querySelector(".model-options").scrollTop'), 0)
                self.assertTrue(self.js('(()=>{const o=document.querySelector(".model-options"),last=o.querySelector("button:last-child");return last.getBoundingClientRect().bottom<=o.getBoundingClientRect().bottom+1;})()'))
                self.assertTrue(self.js('document.querySelector("#model-settings").getBoundingClientRect().bottom<=document.querySelector("#model-menu").getBoundingClientRect().bottom'))
                self.js('(()=>{const search=document.querySelector("#model-search");search.value="模型 08";search.dispatchEvent(new Event("input"));search.focus();search.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",bubbles:true}));})()')
                self.assertEqual(self.js('document.querySelectorAll("[data-connection]:not([hidden])").length'), 1)
                self.assertEqual(self.js('document.activeElement.dataset.connection'), choices[8]['id'])
                self.js('document.querySelector("#model-menu").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')

    def test_model_menu_ignores_late_refresh_after_close_or_document_switch(self):
        import threading
        dispatch = self.widget.dispatch
        other = library.save_document('另一篇正文', '另一篇文稿')
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('typeof document.querySelector("#model")?.onclick') == 'function')
        for switch_document in (False, True):
            with self.subTest(switch_document=switch_document):
                started, release, finished = threading.Event(), threading.Event(), threading.Event()
                def delayed(operation, **arguments):
                    if operation == 'connections':
                        started.set()
                        if not release.wait(5): raise RuntimeError('test did not release model list')
                    value = dispatch(operation, **arguments)
                    if operation == 'connections': finished.set()
                    return value
                self.widget.dispatch = delayed
                try:
                    self.js('document.querySelector("#model").click()')
                    self.until(started.is_set, seconds=2)
                    self.js('document.querySelector("#model-menu").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
                    if switch_document:
                        self.widget.open_entry(other)
                        self.until(lambda: self.js('window.lulu.state.doc?.id') == other)
                    release.set()
                    self.until(finished.is_set)
                    for _ in range(5): self.tick()
                    self.assertTrue(self.js('document.querySelector("#model-menu").hidden'))
                    self.assertEqual(self.js('document.querySelector("#model").getAttribute("aria-expanded")'), 'false')
                finally:
                    release.set()
                    self.widget.dispatch = dispatch
                    self.js('document.querySelector("#model-menu").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')

    def test_model_menu_refresh_failure_keeps_choice_and_can_retry(self):
        from src.core.agent_workspace.service import WorkspaceService
        from src.core.agent_workspace import providers
        service = WorkspaceService()
        choice = providers.save_connection(dict(provider='custom', mode='api', base='https://example.test/v1',
            protocol='openai', model='connected-model', window=32768, max_output=4096, threshold=.8,
            credential_id='workspace-key-isolated', label='已接入模型'), service.store)
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('typeof document.querySelector("#model")?.onclick') == 'function')
        cid = self.js('window.lulu.state.chat.id')
        original_choice = service.store.get(cid)['connection_id']
        dispatch = self.widget.dispatch
        requests = []
        def flaky(operation, **arguments):
            if operation == 'connections':
                requests.append(operation)
                if len(requests) == 1: raise RuntimeError('隔离的连接清单读取失败')
            return dispatch(operation, **arguments)
        self.widget.dispatch = flaky
        self.js('document.querySelector("#model").click()')
        self.until(lambda: len(requests) == 1 and self.js('document.querySelector("#model-menu").hidden'))
        self.assertEqual(service.store.get(cid)['connection_id'], original_choice)
        self.js('document.querySelector("#model").click()')
        self.until(lambda: self.js('document.querySelector("[data-connection]")?.dataset.connection') == choice['id'])
        self.assertEqual(len(requests), 2)
        self.assertFalse(self.js('document.querySelector("#model-menu").hidden'))
        self.assertEqual(service.store.get(cid)['connection_id'], original_choice)

    def test_rapid_list_clicks_skip_intermediate_documents(self):
        ids=[library.save_document('正文 '+str(i),'快速切换 '+str(i)) for i in range(3)]
        self.widget.refresh()
        self.until(lambda:self.js('document.querySelectorAll("[data-entry]").length')==4)
        seen=[];dispatch=self.widget.dispatch
        def tracked(operation,**arguments):
            if operation=='open': seen.append(arguments['document_id'])
            return dispatch(operation,**arguments)
        self.widget.dispatch=tracked
        self.js('for (const id of '+json.dumps(ids)+') document.querySelector(`[data-entry="${id}"]`).click()')
        self.until(lambda:self.js('window.lulu.state.doc?.id')==ids[-1])
        self.until(lambda:self.js('!window.lulu.state.loading'))
        self.assertEqual(seen,[ids[-1]])
        self.assertEqual(library.document_snapshot(ids[0])['text'],'正文 0')

    def test_document_reuse_resets_undo_and_envelope_without_rebuilding_list(self):
        first = library.save_document('---\nkind: first\n---\n# 第一稿\n\n第一篇的文字。', '第一稿')
        second = library.save_document('# 第二稿\n\n第二篇的文字。', '第二稿')
        self.widget.open_entry(first)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt")) && !window.lulu.state.loading'))
        self.js("window.keptEditor = document.querySelector('.ProseMirror'); window.keptRow = document.querySelector('[data-entry]'); document.querySelector('.ProseMirror').focus(); document.execCommand('insertText', false, '输入');")
        self.widget.open_entry(second)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==second and self.js('!window.lulu.state.loading'))
        self.assertTrue(self.js("window.keptEditor === document.querySelector('.ProseMirror')"))
        self.assertTrue(self.js("window.keptRow === document.querySelector('[data-entry]')"))
        self.js("document.querySelector('.ProseMirror').dispatchEvent(new KeyboardEvent('keydown', {key:'z',code:'KeyZ',metaKey:true,bubbles:true}))")
        self.assertEqual(self.js("document.querySelector('.ProseMirror').textContent"),'第二篇的文字。')
        self.js('document.querySelector("#doc-title").textContent="第二稿新版";document.querySelector("#doc-title").dispatchEvent(new Event("input"))')
        self.until(lambda:library.document_snapshot(second)['title']=='第二稿新版')
        self.assertEqual(library.document_snapshot(second)['text'],'# 第二稿新版\n\n第二篇的文字。\n')
        self.widget.open_entry(first)
        self.until(lambda:self.js('window.lulu.state.doc?.id')==first and self.js('!window.lulu.state.loading'))
        self.assertTrue(library.document_snapshot(first)['text'].startswith('---\nkind: first\n---\n# 第一稿\n'))
        self.assertIn('输入', self.js("document.querySelector('.ProseMirror').textContent"))
        # A failed read leaves the already-open document and editing controls usable.
        dispatch=self.widget.dispatch
        def missing(operation, **arguments):
            if operation=='open': raise FileNotFoundError('资料暂不可读')
            return dispatch(operation, **arguments)
        self.widget.dispatch=missing
        self.widget.open_entry(second)
        self.until(lambda:self.js('document.querySelector("#toast").textContent')=='资料暂不可读')
        self.assertEqual(self.js('window.lulu.state.doc.id'),first)
        self.assertFalse(self.js('document.querySelector("#detail").inert'))

    def test_anchored_menus_filter_keyboard_and_small_window_bounds(self):
        folder=library.create_folder('transcript', '很长的文件夹名称 用来检验边缘对齐')
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt")) && !window.lulu.state.loading'))
        self.widget.refresh()
        self.until(lambda:self.js('document.querySelectorAll("#folder option").length')==2)
        for width in (968, 1248, 1448):
            self.widget.resize(width,917)
            for _ in range(6): self.tick()
            for trigger in ('doc-more','list-menu','folder-trigger','sort-trigger'):
                self.js('document.getElementById('+json.dumps(trigger)+').click()')
                self.until(lambda:self.js('Boolean(document.querySelector(".lulu-menu"))'))
                position=json.loads(self.js('JSON.stringify((()=>{const a=document.getElementById('+json.dumps(trigger)+').getBoundingClientRect(),m=document.querySelector(".lulu-menu").getBoundingClientRect();return {left:m.left,right:m.right,top:m.top,anchorBottom:a.bottom,anchorLeft:a.left,width:innerWidth,modal:document.querySelector("#dialog").open}})())'))
                self.assertFalse(position['modal'])
                self.assertGreaterEqual(position['left'],11)
                self.assertLessEqual(position['right'],position['width']-11)
                self.assertAlmostEqual(position['top'],position['anchorBottom']+6,delta=1)
                if trigger.endswith('-trigger'): self.assertAlmostEqual(position['left'],max(12,position['anchorLeft']),delta=1.2)
                self.js('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
                self.assertFalse(self.js('Boolean(document.querySelector(".lulu-menu"))'))
                self.assertEqual(self.js('document.activeElement.id'),trigger)
        self.js('document.querySelector("#sort-trigger").click();document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"End",bubbles:true}));document.activeElement.click()')
        self.assertEqual(self.js('document.querySelector("#sort").value'),'title')
        self.assertIn('标题名称',self.js('document.querySelector("#sort-trigger").textContent'))
        self.js('document.querySelector("#list-menu").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#new-folder"))'))
        self.js('document.querySelector("#new-folder").click()')
        self.until(lambda:self.js('document.querySelector("#dialog").open'))
        self.assertEqual(self.js('document.querySelector("#dialog-title").textContent'),'新建文件夹')
        self.js('document.querySelector("#dialog").close()')
        self.widget.call('setKind','voice')
        self.until(lambda:self.js('window.lulu.state.kind')=='voice' and self.js('!window.lulu.state.loading'))
        self.js('document.querySelector("#list-menu").click()')
        self.assertEqual(self.js('document.querySelector(".lulu-menu").getAttribute("aria-label")'),'音频库操作')
        self.assertNotIn('导入文稿',self.js('document.querySelector(".lulu-menu").textContent'))
        self.assertFalse(self.js('document.querySelector("#dialog").open'))
        self.js('document.querySelector("#new-folder").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#folder-form"))'))
        self.js('document.querySelector("#folder-name").value="声音素材";document.querySelector("#folder-form").dispatchEvent(new Event("submit",{cancelable:true}))')
        self.until(lambda:len(library.list_folders('voice'))==1)
        self.assertEqual(library.list_folders('voice')[0]['name'],'声音素材')
        self.assertEqual(len(library.list_folders('transcript')),1)

    def test_provider_selects_share_menu_without_native_popup(self):
        # The existing conversation picker remains anchored; configuration now
        # opens the single native model page after the document has flushed.
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#model"))'))
        opened=[]
        self.widget.main=SimpleNamespace(_open_workspace_models=lambda:opened.append(True))
        self.js('document.querySelector("#model").click()')
        self.until(lambda:self.js('Boolean(document.querySelector("#model-settings"))'))
        self.js('document.querySelector("#model-settings").click()')
        self.until(lambda:bool(opened))
        self.assertFalse(self.js('Boolean(document.querySelector("#cloud-settings"))'))
        self.widget.main=None
