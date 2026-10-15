"""Real WebEngine interaction with delayed, isolated services; no model/network."""
import json
import time
from tests.test_agent_workspace_ui import AgentWorkspaceUiTests

class WorkspaceInteractionTests(AgentWorkspaceUiTests):
    def open_document(self):
        self.widget.open_entry(self.eid)
        # The previous composer remains in the DOM while a document is loading.
        # Synthetic key events bypass inert; wait for the actual editable screen.
        self.until(lambda:self.js('window.lulu.state.doc?.id === '+json.dumps(self.eid)+
            ' && !window.lulu.state.loading && Boolean(window.lulu.state.chat?.id)'
            ' && !document.querySelector("#agent").inert && Boolean(document.querySelector("#prompt"))'))

    def test_skill_menu_slash_outside_and_pending_close(self):
        self.open_document()
        dispatch=self.widget.dispatch
        def delayed(operation,**args):
            if operation=='skills':time.sleep(.4)
            return dispatch(operation,**args)
        self.widget.dispatch=delayed
        self.assertTrue(self.js('''(()=>{document.querySelector('#prompt-menu').click();
            return !document.querySelector('#composer-popover').hidden;})()'''))
        self.js("document.querySelector('#prompt-menu').click()")
        self.until(lambda:not self.widget.pool.activeThreadCount())
        self.assertTrue(self.js("document.querySelector('#composer-popover').hidden"))
        self.js("document.querySelector('#prompt').value='/';document.querySelector('#prompt').dispatchEvent(new Event('input'))")
        self.until(lambda:self.js("Boolean(document.querySelector('[data-skill]'))"))
        self.assertFalse(self.js("document.querySelector('#composer-popover').hidden"))
        self.js("document.querySelector('#prompt').value='';document.querySelector('#prompt').dispatchEvent(new Event('input'))")
        self.assertTrue(self.js("document.querySelector('#composer-popover').hidden"))
        self.js("document.querySelector('#prompt-menu').click()")
        self.js("document.querySelector('#doc-title').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
        self.assertTrue(self.js("document.querySelector('#composer-popover').hidden"))

    def test_enter_feedback_precedes_slow_save_and_failure_preserves_text(self):
        self.open_document()
        dispatch=self.widget.dispatch
        def delayed(operation,**args):
            if operation=='send':
                time.sleep(.4)
                raise ValueError('隔离发送失败')
            if operation=='save_draft':time.sleep(.3)
            return dispatch(operation,**args)
        self.widget.dispatch=delayed
        self.assertTrue(self.js('''(()=>{const p=document.querySelector('#prompt');p.value='保留我输入的话';
            p.dispatchEvent(new Event('input'));p.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
            return document.querySelector('#send').disabled && document.querySelector('#send').getAttribute('aria-busy')==='true';})()'''))
        self.until(lambda:self.js("document.querySelector('#toast').textContent.includes('隔离发送失败')"))
        self.assertEqual(self.js("document.querySelector('#prompt').value"),'保留我输入的话')
        self.assertFalse(self.js("document.querySelector('#send').disabled"))

    def test_library_switch_reads_list_selection_and_content_once(self):
        import wave
        from src.core import library
        from src.core.agent_workspace.service import WorkspaceService
        audio=self.root/'sample.wav'
        with wave.open(str(audio),'wb') as f:
            f.setparams((1,2,8000,0,'NONE',''));f.writeframes(b'\0\0'*800)
        eid=library.save_voice(title='隔离音频',audio_path=audio,text='音频正文',engine='local_voxcpm2',voice_name='隔离音色',config_summary='隔离配置')
        WorkspaceService().view_state('audio',eid)
        self.open_document()
        calls=[];dispatch=self.widget.dispatch
        def counted(operation,**args):
            calls.append(operation)
            if operation == 'library_view':
                time.sleep(.2)
            return dispatch(operation,**args)
        self.widget.dispatch=counted
        self.widget._set_kind('voice')
        self.until(lambda:self.js('Boolean(document.querySelector("#audio-play"))'))
        self.assertEqual(calls.count('library_view'),1)
        self.assertNotIn('audio',calls)
        self.assertNotIn('list',calls)
        self.assertNotIn('view_state',calls)
        self.widget._set_kind('transcript')
        # Audio hides the old composer without removing it. Its presence alone
        # cannot prove that the asynchronous return to documents has finished.
        self.until(lambda:self.js('window.lulu.state.kind === "documents" && !window.lulu.state.loading && Boolean(document.querySelector("#prompt"))'))
        self.assertEqual(self.js('window.lulu.state.doc.id'),self.eid)

    def test_slash_keyboard_composition_and_selection(self):
        self.open_document()
        self.js("document.querySelector('#prompt').value='/校正';document.querySelector('#prompt').dispatchEvent(new Event('input'))")
        self.until(lambda:self.js("document.querySelectorAll('[data-skill]').length===1"))
        self.js("document.querySelector('#prompt').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}))")
        self.assertFalse(self.js("document.querySelector('#composer-popover').hidden"))
        self.assertEqual(self.js("document.querySelector('#prompt').value"),'/校正')
        self.js("document.querySelector('#prompt').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        self.assertEqual(self.js("document.querySelector('#prompt').value"),'/lulu-proofread ')
        self.assertTrue(self.js("document.querySelector('#composer-popover').hidden"))
        capture=__import__('os').environ.get('LULU_INTERACTION_CAPTURE')
        if capture:
            from pathlib import Path
            Path(capture).mkdir(parents=True,exist_ok=True)
            self.js("document.querySelector('#prompt-menu').click()")
            self.until(lambda:self.js("!document.querySelector('#composer-popover').hidden && document.querySelectorAll('[data-skill]').length>0"))
            from PySide6.QtTest import QTest
            QTest.qWait(250)
            self.widget.grab().save(str(Path(capture)/'skill-menu.png'))

    def test_failed_library_switch_preserves_old_document_and_restores_input(self):
        self.open_document()
        original=self.js('window.lulu.state.doc.text')
        dispatch=self.widget.dispatch
        def fail(operation,**args):
            if operation=='library_view':raise OSError('隔离读取失败')
            return dispatch(operation,**args)
        self.widget.dispatch=fail
        self.widget._set_kind('voice')
        self.until(lambda:self.js("document.querySelector('#toast').textContent.includes('隔离读取失败')"))
        self.assertEqual(self.js('window.lulu.state.kind'),'documents')
        self.assertEqual(self.js('window.lulu.state.doc.text'),original)
        self.assertFalse(self.js("document.querySelector('#workspace').inert"))
