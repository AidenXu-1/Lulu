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
