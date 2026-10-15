"""V24 rendered with Qt, fake tasks/services, no user files, credentials or network."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX','1')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu')
from PySide6.QtCore import QEventLoop,QTimer,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QWidget
from src.config import AppConfig
from src.core.task import Task,TaskState
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.ui.extraction.widget import ExtractionWorkspace

class FakeMain(QWidget):
    def __init__(self,root):
        super().__init__();self.tasks=[];self.task_groups=[];self.calls=[]
        self.cfg=AppConfig();self.cfg.media_dir=str(root);self.cfg.recording_dir=str(root)
        self._rec_elapsed_timer=QTimer(self);self._rec_source='microphone';self._rec_elapsed=0
        self._paused_task_group_ids=set();self._core_hidden_intents=set()
    def _material_snapshot(self):return MaterialPreferenceSnapshot('audio',False,False)
    def _task(self,tid):return next((t for t in self.tasks if t.id==tid),None)
    def _add_task(self,source,kind,title,**kw):
        t=Task(id=len(self.tasks)+1,source=source,kind=kind,title=title,execution_policy=self.workspace.initial_policy(source,kind));self.tasks.append(t);return t
    def _persist_task_queue(self):self.calls.append('save')
    def _refresh_badge(self):pass
    def _start_task(self,tid):self._task(tid).state=TaskState.QUEUED;self.calls.append(('start',tid))
    def _task_group_id_for_task(self,tid):return None
    def _cancel_task(self,tid):self._task(tid).state=TaskState.CANCELLED
    def _delete_task(self,tid):self.tasks=[t for t in self.tasks if t.id!=tid]
    def _on_rec_source_selected(self,s):self._rec_source=s
    def _toggle_record(self):self.calls.append('record')
    def _cancel_recording(self):self.calls.append('cancel_record')
    def _pick_file(self):self.calls.append('files')
    def _pick_recording_dir(self):self.calls.append('record_folder')

class ExtractionWorkspaceFixture:
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patcher=patch('src.core.models_manager.is_ready',return_value=True);self.patcher.start()
        self.main=FakeMain(self.root);self.widget=ExtractionWorkspace(self.main);self.main.workspace=self.widget;self.widget.setParent(None)
        self.widget.resize(1463,925);self.widget.show();self.until(lambda:self.widget.ready)
        self.until(lambda:self.js('Boolean(document.querySelector(".dropzone"))'))
    def tearDown(self):
        self.widget.shutdown();self.widget.close();self.widget.deleteLater();self.main.close();self.main.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.app.processEvents();self.patcher.stop();self.temp.cleanup()
    def tick(self):
        loop=QEventLoop();QTimer.singleShot(15,loop.quit);loop.exec()
    def until(self,f,seconds=12):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if f():return
            self.tick()
        self.fail('Workspace did not settle')
    def js(self,source):
        got=[];self.widget.page.runJavaScript(source,lambda x:got.append(x));self.until(lambda:bool(got));return got[0]
    def add(self,count=1):
        for i in range(count):self.main._add_task('https://www.douyin.com/video/'+str(i),'link','测试作品 '+str(i))
        self.widget.publish(force=True);self.until(lambda:self.js('tasks.length')==len(self.main.tasks))

class ExtractionWorkspaceTests(ExtractionWorkspaceFixture,unittest.TestCase):
    def test_paused_queue_shows_reason_instead_of_zero_ahead(self):
        self.add()
        task = self.main.tasks[0]
        task.state = TaskState.QUEUED
        task._core_recovered_queue = True
        task.message = '任务尚未保存，输入已保留；恢复后点击继续'
        self.widget.publish(force=True)
        self.until(lambda: self.js('document.querySelector(".process-status")?.textContent') == '已暂停')
        self.assertFalse(self.js('Boolean(document.querySelector(".queue-number"))'))
        self.assertIn('输入已保留', self.js('document.querySelector(".queue-hint").textContent'))
        task.message = '连接已恢复，点击继续'
        self.widget.publish(force=True)
        self.until(lambda: '连接已恢复' in self.js('document.querySelector(".queue-hint").textContent'))
        task._core_recovered_queue = False
        self.widget.publish(force=True)
        self.until(lambda: self.js('document.querySelector(".process-status")?.textContent') == '排队中')
        self.assertIn('0', self.js('document.querySelector(".queue-number").textContent'))

    def test_render_focus_progress_seven_choices_and_undo(self):
        self.add(6)
        self.assertEqual(self.js('document.querySelectorAll(".task-card").length'),6)
        geometry=json.loads(self.js('JSON.stringify([...document.querySelectorAll(".task-card")].map(n=>({x:n.offsetLeft,y:n.offsetTop,height:n.offsetHeight})))'))
        self.assertEqual([r['height'] for r in geometry],[232]*6)
        self.assertEqual(len({r['y'] for r in geometry}),2)
        self.until(lambda:self.js('Array.from(document.images).every(i=>i.complete&&i.naturalWidth>0)'))
        self.js("document.querySelector('#url').focus();document.querySelector('#url').value='中文输入未完成';urlText='中文输入未完成'")
        self.main.tasks[0].state=TaskState.RUNNING;self.main.tasks[0].progress=.41
        self.widget.publish(force=True);self.until(lambda:self.js('document.querySelectorAll(".live-progress").length')==1)
        self.assertEqual(self.js('document.activeElement.id'),'url')
        self.assertEqual(self.js('document.querySelector("#url").value'),'中文输入未完成')
        self.main.tasks[0].progress=.42;self.widget.publish(force=True);self.tick()
        self.assertEqual(self.js('document.activeElement.id'),'url')
        self.widget.dispatch('delete',{'ids':[2]});self.widget.publish(force=True)
        self.until(lambda:self.js('tasks.length')==5)
        self.widget.dispatch('undo_delete',{});self.widget.publish(force=True)
        self.until(lambda:self.js('tasks.length')==6)
        self.assertEqual(len(self.main.tasks),6)
        for key in ('text','video','cover'):self.widget.dispatch('choice',{'ids':[2],'key':key,'value':False})
        self.widget.publish(force=True)
        self.until(lambda:self.js('document.querySelector("[data-job=\\\"2\\\"] .task-start").disabled'))
        self.widget.dispatch('start',{'ids':[2]});self.assertEqual(self.main.tasks[1].state,TaskState.PENDING)
        self.widget.dispatch('choice',{'ids':[2],'key':'cover','value':True});self.widget.dispatch('start',{'ids':[2]})
        self.assertEqual(self.main.tasks[1].state,TaskState.QUEUED)
        with self.assertRaises(ValueError):self.widget.dispatch('choice',{'ids':[2],'key':'video','value':True})
        self.widget.hide();self.widget.show();self.assertEqual(self.js('document.querySelector("#url").value'),'中文输入未完成')
    def test_history_card_can_be_removed_and_undone_without_mutating_results(self):
        self.add(2)
        history=self.main.tasks[0]
        history._core_view_only=True;history.state=TaskState.FAILED
        history.library_id='saved-document'
        self.widget.publish(force=True)
        self.until(lambda:self.js('tasks[0].readOnly'))
        self.assertTrue(self.js("Boolean(document.querySelector('[data-job=\"1\"] .task-remove'))"))
        self.js("document.querySelector('[data-job=\"1\"] .task-remove').click()")
        self.until(lambda:self.js('tasks.length')==1)
        self.widget.dispatch('undo_delete',{});self.widget.publish(force=True)
        self.until(lambda:self.js('tasks.length')==2)
        self.widget.dispatch('delete',{'ids':[1]})
        self.widget._deleting[history.id]=0;self.widget.publish(force=True)
        self.assertIsNone(self.main._task(history.id))
        self.assertEqual(history.library_id,'saved-document')
        self.assertEqual(history.state,TaskState.FAILED)

    def test_readonly_active_card_cannot_be_cancelled_or_removed_in_a_batch(self):
        self.add(2)
        task=self.main.tasks[1];task._core_view_only=True;task.state=TaskState.RUNNING
        self.widget.publish(force=True)
        self.until(lambda:self.js('tasks[1].readOnly'))
        self.assertFalse(self.js("Boolean(document.querySelector('[data-job=\"2\"] .task-remove'))"))
        for action in ('delete','cancel'):
            with self.assertRaises(ValueError):self.widget.dispatch(action,{'ids':[1,2]})
        self.assertFalse(self.widget._deleting)
        self.assertEqual(task.state,TaskState.RUNNING)

    def test_native_actions_and_batch(self):
        for i in range(7):(self.root/f'{i}.wav').write_bytes(b'fixture')
        self.widget.add_files([str(p) for p in self.root.glob('*.wav')]);self.widget.publish(force=True)
        self.until(lambda:self.js('document.querySelectorAll(".task-group").length')==1)
        self.js('toggleGroup(1)');self.until(lambda:self.js('document.querySelectorAll(".member").length')==7)
        self.assertEqual(self.js('document.querySelector(".members").offsetHeight'),311)
        self.assertGreater(self.js('document.querySelector(".members").scrollHeight'),311)
        self.widget.dispatch('record_source',{'value':'混录'});self.assertEqual(self.main._rec_source,'mixed')
        self.widget.dispatch('files',{});self.widget.dispatch('record',{})
        self.assertIn('files',self.main.calls);self.assertIn('record',self.main.calls)
        with self.assertRaises(ValueError):self.widget.dispatch('result',{'id':1,'action':'shell','path':'/tmp/a'})
    def test_responsive_geometry_model_menu_and_reduced_motion(self):
        self.add(6)
        destination=os.environ.get('LULU_EXTRACTION_CAPTURE')
        for width, columns in ((1463,3),(1060,2),(563,1)):
            with self.subTest(width=width):
                self.widget.resize(width,925)
                self.until(lambda:self.js('window.innerWidth')==width)
                self.until(lambda:self.js('getComputedStyle(document.querySelector(".taskgrid")).gridTemplateColumns.split(" ").length')==columns)
                self.assertTrue(self.js('document.querySelector("#main").scrollWidth<=window.innerWidth'))
                if destination:self.widget.grab().save(str(Path(destination)/f'native-{width}.png'))
        self.widget.resize(1463,925);self.widget.publish(force=True)
        self.until(lambda:self.js('window.innerWidth')==1463)
        self.assertFalse(self.js('document.documentElement.hasAttribute("data-theme")'))
        self.js('toggleModelMenu()');self.assertTrue(self.js('document.querySelector("#model-switch").getAttribute("aria-expanded")==="true"'))
        self.assertGreater(self.js('document.querySelectorAll(".model-option").length'),0)
        self.assertTrue(self.js('document.querySelector("#model-menu").getBoundingClientRect().right<=innerWidth'))
        if destination:
            end=time.monotonic()+.5
            while time.monotonic()<end:self.tick()
            self.widget.grab().save(str(Path(destination)/'native-paper-model-menu.png'))
        self.assertIn('prefers-reduced-motion', (Path(__file__).parents[1]/'src/ui/extraction/assets/host.css').read_text())
    def test_skipped_batch_members_remain_visible(self):
        from src.core.task import TaskGroup,TaskGroupMember
        self.main.task_groups=[TaskGroup(id=1,name='无法加入的作品',members=[TaskGroupMember(stable_key='a',title='已下架',skipped_reason='作品已无法访问')])]
        self.widget.publish(force=True)
        self.until(lambda:self.js('document.querySelectorAll(".member").length')==1)
        self.assertFalse(self.js('document.querySelector("#board").hidden'))
        self.assertIn('作品已无法访问',self.js('document.querySelector("#board").textContent'))

class ExtractionOverlayTests(ExtractionWorkspaceFixture,unittest.TestCase):
    def completed_result(self):
        from src.core.models import Transcript
        self.add()
        task=self.main.tasks[0];task.state=TaskState.DONE;task.library_id='isolated-document'
        task.title='第三期 给编导看天赋 #编导 #代运营 #短视频'
        assets={k:str(self.root/name) for k,name in [('video','clip.mp4'),('cover','cover.png')]}
        for path in assets.values():Path(path).write_bytes(b'isolated fixture')
        task.transcript=Transcript(text='测试文稿',meta={'extraction_assets':assets})
        for route in ('view','copy','export','obsidian','feishu','voice'):
            setattr(self.main,f'_{route}_task',lambda tid,r=route:self.main.calls.append((r,tid)))
        self.main._ensure_task_transcript=lambda *args:True
        self.main._reveal=lambda path:self.main.calls.append(('reveal',path))
        self.widget.publish(force=True)
        self.until(lambda:self.js('Boolean(document.querySelector(".finished-result"))'))
        return task

    def open_result(self):
        self.js('document.querySelector(".process-footer button").focus();document.querySelector(".process-footer button").click()')
        self.assertFalse(self.js('document.querySelector("#modalback").hidden'))

    def test_native_pointer_opens_and_dismisses_result(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import QPoint,Qt
        self.completed_result()
        # Measure pointer coordinates after the 500 ms card entrance settles;
        # an in-flight 3D transform otherwise moves the target before the click.
        self.until(lambda:self.js('document.querySelectorAll(".task-enter").length')==0)
        view=self.widget.view
        def click(selector):
            x,y=json.loads(self.js(f'JSON.stringify((()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return [Math.round(r.x+r.width/2),Math.round(r.y+r.height/2)]}})())'))
            QTest.mouseClick(view.focusProxy() or view,Qt.LeftButton,Qt.NoModifier,QPoint(x,y))
        click('.process-footer button');self.until(lambda:not self.js('document.querySelector("#modalback").hidden'))
        click('.result-document h3');self.assertFalse(self.js('document.querySelector("#modalback").hidden'))
        QTest.mouseClick(view.focusProxy() or view,Qt.LeftButton,Qt.NoModifier,QPoint(16,16))
        self.until(lambda:self.js('document.querySelector("#modalback").hidden'))

    def test_result_sheet_dismissal_focus_and_all_existing_actions(self):
        self.completed_result();self.open_result()
        self.assertEqual(self.js('document.querySelectorAll(".result-assets button").length'),8)
        self.assertTrue(self.js('document.querySelector("#main").inert'))
        self.js('document.querySelector(".result-document h3").click()')
        self.assertFalse(self.js('document.querySelector("#modalback").hidden'))
        # Dragging from the sheet onto the backdrop must not dismiss it.
        self.js('document.querySelector(".modal").dispatchEvent(new PointerEvent("pointerdown",{bubbles:true}));document.querySelector("#modalback").click()')
        self.assertFalse(self.js('document.querySelector("#modalback").hidden'))
        self.js('document.querySelector("#modalback").dispatchEvent(new PointerEvent("pointerdown",{bubbles:true}));document.querySelector("#modalback").click()')
        self.assertTrue(self.js('document.querySelector("#modalback").hidden'))
        self.assertFalse(self.js('document.querySelector("#main").inert'))
        self.assertTrue(self.js('document.activeElement.matches(".process-footer button")'))
        for label,route in [('复制文案','copy'),('导出文案','export'),('存入 Obsidian','obsidian'),('存入飞书','feishu'),('查看文稿','view'),('带入文案配音','voice')]:
            self.open_result()
            self.js(f'[...document.querySelectorAll(".result-assets button")].find(n=>n.textContent.includes({json.dumps(label)})).click()')
            self.until(lambda:(route,1) in self.main.calls)
            if route in ('view','voice'):self.until(lambda:self.js('document.querySelector("#modalback").hidden'))
        self.open_result()
        for name in ('视频','封面'):
            self.js(f'document.querySelector("[aria-label=在访达中显示{name}]").click()')
        self.until(lambda:len([x for x in self.main.calls if isinstance(x,tuple) and x[0]=='reveal'])==2)
        self.js('document.querySelector("#modalback button").focus();document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",shiftKey:true,bubbles:true}))')
        self.assertEqual(self.js('document.activeElement.getAttribute("aria-label")'),'在访达中显示封面')
        self.js('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",bubbles:true}))')
        self.assertEqual(self.js('document.activeElement.getAttribute("aria-label")'),'关闭')
        self.js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape"}))')
        self.assertTrue(self.js('document.querySelector("#modalback").hidden'))

    def test_result_partial_failure_and_unavailable_file(self):
        task=self.completed_result();task.error='封面下载失败 <重试>';task.transcript.meta['extraction_assets'].pop('cover')
        self.widget.publish(force=True);self.until(lambda:self.js('tasks[0].error')==task.error)
        self.open_result()
        self.assertEqual(self.js('document.querySelectorAll(".result-file").length'),1)
        self.assertIn(task.error,self.js('document.querySelector(".result-warning").textContent'))
        self.assertTrue(self.js('Boolean(document.querySelector(".result-warning button"))'))
        Path(task.transcript.meta['extraction_assets']['video']).unlink()
        self.js('document.querySelector(".result-file").click()')
        self.until(lambda:self.js('document.querySelector("#toast").textContent')=='素材文件已移动或不存在')
        self.assertFalse(self.js('document.querySelector("#modalback").hidden'))
        self.assertNotIn('reveal',[x[0] for x in self.main.calls if isinstance(x,tuple)])
        task._core_view_only=True;self.widget.publish(force=True)
        self.until(lambda:self.js('tasks[0].readOnly'))
        self.open_result()
        self.assertFalse(self.js('Boolean(document.querySelector(".result-warning button"))'))

    def test_model_tabs_stable_height_keyboard_and_no_selection_on_browse(self):
        with patch.object(self.main.cfg,'save') as save:
            self.js('document.querySelector("#model-switch").click()')
            self.assertEqual(self.js('document.querySelectorAll(".model-option").length'),3)
            self.assertNotIn('OpenAI',self.js('document.querySelector("#model-options").textContent'))
            height=self.js('document.querySelector("#model-menu").offsetHeight')
            self.js('document.querySelector("#model-tab-cloud").click()')
            self.assertEqual(self.js('document.querySelectorAll(".model-option").length'),5)
            self.assertNotIn('SenseVoice-Small',self.js('document.querySelector("#model-options").textContent'))
            self.assertEqual(self.js('document.querySelector("#model-menu").offsetHeight'),height)
            self.assertEqual(self.js('document.querySelector("#model-options").clientHeight'),192)
            self.assertGreater(self.js('document.querySelector("#model-options").scrollHeight'),192)
            save.assert_not_called()
            self.assertIn('音频会上传',self.js('document.querySelector("#model-location-note").textContent'))
            self.js('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowLeft",bubbles:true}))')
            self.assertEqual(self.js('document.activeElement.id'),'model-tab-local')
            self.js('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",bubbles:true}))')
            self.assertTrue(self.js('document.activeElement.matches(".model-option")'))
            expected=self.js('document.activeElement.dataset.modelId')
            self.js('document.activeElement.click()');self.until(lambda:self.main.cfg.engine_name==expected)
            save.assert_called_once()
            self.until(lambda:self.js('document.querySelector("#model-menu").hidden'))
            self.assertEqual(self.js('document.activeElement.id'),'model-switch')
            self.js('toggleModelMenu();switchModelTab("cloud");document.querySelector("#model-options").scrollTop=80;document.querySelectorAll(".model-option")[2].focus()')
            focus=self.js('document.activeElement.dataset.modelId');scroll=self.js('document.querySelector("#model-options").scrollTop')
            self.widget._models[0]['ready']=False;self.widget.publish(force=True)
            self.until(lambda:self.js('!modelOptions[0].ready'))
            self.assertEqual(self.js('modelTab'),'cloud')
            self.assertEqual(self.js('document.activeElement.dataset.modelId'),focus)
            self.assertEqual(self.js('document.querySelector("#model-options").scrollTop'),scroll)
            self.js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape"}))')
            self.assertEqual(self.js('document.activeElement.id'),'model-switch')
            self.js('toggleModelMenu();document.querySelector("#url").focus()')
            self.assertTrue(self.js('document.querySelector("#model-menu").hidden'))

    def test_model_category_and_cloud_confirmation_remain_separate(self):
        cloud=next(m['id'] for m in self.widget._models if m['cloud'])
        original=self.main.cfg.engine_name
        with patch.object(self.main.cfg,'save') as save,patch('src.ui.dialogs.confirm',return_value=False) as confirm:
            self.js('toggleModelMenu();switchModelTab("cloud");document.querySelector(".model-option").click()')
            self.until(lambda:confirm.called)
            self.assertEqual(self.main.cfg.engine_name,original);save.assert_not_called()
            self.until(lambda:self.js('!modelMenuOpen'))
            confirm.return_value=True
            self.js('toggleModelMenu();switchModelTab("cloud");document.querySelector(".model-option").click()')
            self.until(lambda:self.main.cfg.engine_name==cloud);save.assert_called_once()
            self.until(lambda:self.js('!modelMenuOpen'))
            self.js('toggleModelMenu()');self.assertEqual(self.js('modelTab'),'cloud')

    def test_refined_surfaces_geometry_and_capture(self):
        task=self.completed_result()
        destination=os.environ.get('LULU_EXTRACTION_CAPTURE')
        for width,height in [(1463,925),(1060,760),(380,520)]:
            self.widget.resize(width,height);self.widget.publish(force=True)
            self.until(lambda:self.js('window.innerWidth')==width)
            self.assertFalse(self.js('document.documentElement.hasAttribute("data-theme")'))
            self.open_result()
            self.until(lambda:self.js('Array.from(document.querySelectorAll(".result-assets img")).every(i=>i.complete&&i.naturalWidth>0)'))
            self.assertTrue(self.js('(()=>{const m=document.querySelector(".modal"),r=m.getBoundingClientRect();return r.top>=0&&r.left>=0&&r.bottom<=innerHeight&&r.right<=innerWidth&&m.scrollWidth<=m.clientWidth})()'))
            if destination:
                for _ in range(8):self.tick()
                self.widget.grab().save(str(Path(destination)/f'result-paper-{width}.png'))
            self.js('closeModal();toggleModelMenu()')
            for category in ('local','cloud'):
                self.js(f'switchModelTab("{category}")')
                self.assertTrue(self.js('(()=>{const m=document.querySelector("#model-menu"),r=m.getBoundingClientRect();return r.top>=0&&r.left>=0&&r.bottom<=innerHeight&&r.right<=innerWidth&&m.scrollWidth<=m.clientWidth})()'))
                if destination:
                    for _ in range(8):self.tick()
                    self.widget.grab().save(str(Path(destination)/f'menu-{category}-paper-{width}.png'))
            self.js('closeModelMenu()')
