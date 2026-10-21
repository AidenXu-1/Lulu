"""Real main-window integration, with temporary library and controllable worker."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_extraction_workspace as fixture
from tests.test_task_groups import _FakeWorker
from src.config import AppConfig
from src.core import library
from src.core.models import Transcript
from src.core.task import TaskState
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.ui.main_window import MainWindow
from PySide6.QtCore import QEventLoop,QTimer,QCoreApplication,QEvent

class ExtractionMainWindowTests(fixture.ExtractionWorkspaceTests):
    # Reuse only event-loop helpers; do not inherit fixture-only test methods.
    test_capture=None
    test_skipped_batch_members_remain_visible=None
    test_responsive_geometry_model_menu_and_reduced_motion=None
    test_render_focus_progress_seven_choices_and_undo=None
    test_native_actions_and_batch=None
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.stack=ExitStack();cfg=AppConfig();cfg.engine_name='local_sensevoice_q8';cfg.media_dir=str(self.root/'media');cfg.recording_dir=str(self.root/'recordings')
        for p in [patch.object(AppConfig,'load',return_value=cfg),patch.object(AppConfig,'save',return_value=True),
            patch('src.core.models_manager.app_data_dir',return_value=self.root/'data'),patch('src.core.models_manager.is_ready',return_value=True),
            patch.object(library,'LIBRARY_DIR',self.root/'library'),patch.object(library,'FOLDERS_FILE',self.root/'library/folders.json'),
            patch.object(MainWindow,'_prepare_library'),patch.object(MainWindow,'_sync_runtime_preferences'),
            patch.object(MainWindow,'_notify_task_completed'),patch.object(MainWindow,'_notify_task_failed'),
            patch('socket.create_connection',side_effect=AssertionError('network forbidden')),patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')),
            patch('src.core.secrets._backend',side_effect=AssertionError('credentials forbidden'))]:
            self.stack.enter_context(p)
        self.main=MainWindow(task_queue_store=None,cover_localizer=SimpleNamespace(startup_recovery_receipts=(),localize=lambda **kw:None))
        self.widget=self.main.extraction_workspace;self.main.resize(1680,960);self.main.show()
        self.until(lambda:self.widget.ready);self.until(lambda:self.js('Boolean(document.querySelector(".dropzone"))'))
        _FakeWorker.reset()
    def tearDown(self):
        self.main._runtime_preferences_timer.stop();self.widget.shutdown();self.main.hide();self.main.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.app.processEvents();self.stack.close();self.temp.cleanup()
    def test_failed_preparation_banner_persists_and_web_start_preserves_input(self):
        from src.core.gui_task_ledger import CoreTaskQueueStore
        self.main._task_queue_store = CoreTaskQueueStore.unavailable()
        self.main._install_preparation_pending = True
        task = self.main._add_task(str(self.root / 'input.wav'), 'file', '本次输入')
        self.main.finish_install_preparation(False)
        self.widget.publish(force=True)
        self.until(lambda: self.js('!document.querySelector("#startup-error").hidden'))
        self.assertIn('重新打开 Lulu', self.js('document.querySelector("#startup-error").textContent'))
        self.until(lambda: self.js('!document.querySelector("#toast").hidden'))
        self.until(lambda: self.js('document.querySelector("#toast").hidden'), seconds=6)
        self.assertFalse(self.js('document.querySelector("#startup-error").hidden'))
        identifier = self.widget._ids[task.id]
        self.js(f'document.querySelector("[data-job=\\\"{identifier}\\\"] .task-start").click()')
        self.until(lambda: self.js('!document.querySelector("#toast").hidden'))
        self.assertIn('重新打开 Lulu', self.js('document.querySelector("#toast").textContent'))
        self.assertEqual(task.state, TaskState.PENDING)
        self.assertIsNone(self.main.current)
        self.assertEqual(self.js('document.querySelectorAll(".queue-hint").length'), 0)
        self.main.stack.setCurrentIndex(1);self.tick();self.main.stack.setCurrentIndex(0);self.tick()
        self.assertFalse(self.js('document.querySelector("#startup-error").hidden'))
        task.state = TaskState.CANCELLED
        policy = task.execution_policy
        task.error = '保留的上次错误'
        with self.assertRaisesRegex(RuntimeError, '重新打开 Lulu'):
            self.widget.dispatch('start', {'ids': [identifier]})
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(task.execution_policy, policy)
        self.assertEqual(task.error, '保留的上次错误')

    def test_real_queue_binds_model_at_execution_and_commits_asset_only(self):
        a=self.main._add_task('https://www.douyin.com/video/1','link','只取封面')
        b=self.main._add_task('https://www.douyin.com/video/2','link','转录')
        a.execution_policy=replace(a.execution_policy,requested_outputs=('cover',),material_snapshot=MaterialPreferenceSnapshot('audio',False,True))
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.main._start_all()
            self.assertEqual(a.execution_policy.engine_name,'local_sensevoice_q8');self.assertIsNone(b.execution_policy.engine_name)
            self.main.cfg.engine_name='local_whisper_mlx'
            output=self.root/'cover.jpg';output.write_bytes(b'fixture')
            _FakeWorker.instances[0].finished_ok.emit(Transcript(text='',meta={'extraction_assets':{'cover':str(output)},'extraction_errors':[]}))
            self.assertEqual(a.state,TaskState.DONE);self.assertIsNone(a.library_id)
            self.assertEqual(list((self.root/'library').glob('*.md')),[])
            _FakeWorker.instances[0].finish_thread()
            self.assertEqual(b.state,TaskState.RUNNING);self.assertEqual(b.execution_policy.engine_name,'local_whisper_mlx')
            _FakeWorker.instances[1].finished_ok.emit(Transcript(text='真实保存流程的隔离测试文字',title='测试文稿',meta={'extraction_assets':{},'extraction_errors':[]}))
            self.assertEqual(b.state,TaskState.DONE);self.assertTrue(b.library_id)
            self.assertIn('隔离测试文字',library.load_markdown(b.library_id))
            _FakeWorker.instances[1].finish_thread();self.assertIsNone(self.main.current)
        self.assertTrue(output.is_file())
        self.widget.publish(force=True);self.until(lambda:self.js('tasks.filter(t=>t.state==="done").length')==2)
        self.assertTrue(self.js('tasks[0].assets.cover.length>0'))
    def test_actual_window_persistent_page_and_capture(self):
        for i in range(6):self.main._add_task(f'https://www.douyin.com/video/{i}','link',f'作品 {i}')
        self.widget.publish(force=True);self.until(lambda:self.js('tasks.length')==6)
        self.js("document.querySelector('#url').value='保留输入';urlText='保留输入';document.querySelector('#main').scrollTop=100")
        page=self.main.stack.widget(0)
        # Use an existing placeholder to exercise native stack navigation without
        # constructing unrelated pages, credentials or installed services.
        self.main.stack.setCurrentIndex(1);self.tick();self.main.stack.setCurrentIndex(0);self.tick()
        self.assertIs(page,self.main.stack.widget(0));self.assertEqual(self.js('document.querySelector("#url").value'),'保留输入')
        self.assertEqual(self.js('document.querySelector("#pageTitle").textContent'),'内容提取')
        nav=next(row for index,_,row,_,label,_ in self.main._page_navs if index==0)
        self.assertEqual(nav.accessibleName(),'导航：内容提取')
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        self.main.stack.setCurrentIndex(1)
        QTest.mouseClick(nav,Qt.MouseButton.LeftButton);self.tick()
        self.assertEqual(self.main.stack.currentIndex(),0)
        self.assertEqual(self.js('document.querySelector("#url").value'),'保留输入')
        # Display names may change; stored tool records keep their stable action.
        from src.core.agent_workspace import tools
        from src.ui.agent_workspace.widget import AgentWorkspace
        from unittest.mock import Mock
        url='https://www.douyin.com/video/123'
        result=tools.execute(dict(name='prepare_transcription',arguments={'url':url}),{})
        self.assertEqual(result['label'],'前往内容提取')
        self.assertEqual(result['action'],'transcription')
        host=SimpleNamespace(main=self.main,bridge=Mock())
        for label in (result['label'],'前往文案提取'):
            self.main.stack.setCurrentIndex(1)
            AgentWorkspace.finish_handoff(host,'rename-check',json.dumps(dict(ok=True,value=dict(result,label=label))))
            self.assertEqual(self.main.stack.currentIndex(),0)
            self.until(lambda:self.js('document.querySelector("#url").value')==url)
            self.assertEqual(self.js('urlText'),url)
            host.bridge.respond.assert_called_with('rename-check',True,{'state':'needs_input','action':'transcription'})
        self.assertEqual(len(self.main.tasks),6)
        self.assertIsNone(self.main.current)
        destination=os.environ.get('LULU_EXTRACTION_CAPTURE')
        if destination:
            self.js("document.querySelector('#main').scrollTop=0")
            end=time.monotonic()+.7
            while time.monotonic()<end:self.tick()
            self.main.grab().save(str(Path(destination)/'main-window.png'))
    def test_partial_result_retry_only_missing_outputs_and_protect_unsaved_text(self):
        task=self.main._add_task('https://www.douyin.com/video/8','link','部分完成')
        task.execution_policy=replace(task.execution_policy,requested_outputs=('text','video','cover'),material_snapshot=MaterialPreferenceSnapshot('video',True,True))
        video=self.root/'video.mp4';video.write_bytes(b'fixture')
        self.main._commit_extraction_result(task,Transcript(text='已经保存的文稿',meta={'extraction_assets':{'video':str(video)},'extraction_errors':['封面获取失败']}))
        self.assertEqual(task.state,TaskState.DONE);self.assertIn('封面',task.error)
        self.widget.publish(force=True)
        identifier=self.widget._ids[task.id]
        self.widget.dispatch('result',{'id':identifier,'action':'retry_missing'})
        retry=self.main.tasks[-1]
        self.assertEqual(retry.execution_policy.requested_outputs,('cover',))
        self.assertIsNone(retry.execution_policy.engine_name)
        self.assertTrue(video.is_file())
        self.assertIn('已经保存的文稿',library.load_markdown(task.library_id))
        with patch.object(library,'save',side_effect=OSError('fixture disk full')):
            retry.execution_policy=replace(retry.execution_policy,requested_outputs=('text',))
            self.main._commit_extraction_result(retry,Transcript(text='还没保存的文字'))
        self.assertEqual(retry.message,'结果待保存')
        self.widget.publish(force=True)
        with self.assertRaisesRegex(ValueError,'请先保存'):
            self.widget.dispatch('choice',{'ids':[self.widget._ids[retry.id]],'key':'text','value':False})
        self.widget.dispatch('start',{'ids':[self.widget._ids[retry.id]]})
        self.assertEqual(retry.state,TaskState.DONE)
        self.assertIn('还没保存的文字',library.load_markdown(retry.library_id))
    def test_failed_empty_attempt_has_no_phantom_unsaved_result(self):
        from src.core.extraction import has_unsaved_result
        task=self.main._add_task('https://www.douyin.com/video/404','link','无法获取')
        task.execution_policy=replace(task.execution_policy,requested_outputs=('cover',))
        self.main._commit_extraction_result(task,Transcript(text='',meta={'extraction_errors':['封面获取失败']}))
        self.assertEqual(task.state,TaskState.FAILED)
        self.assertIsNone(task.transcript)
        self.assertFalse(has_unsaved_result(task))
        self.assertIsNone(task.library_id)

    def test_receipt_failure_after_document_commit_retries_save_after_restart(self):
        from src.core import extraction
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task_queue_store import TaskQueueSnapshot
        from src.ui.extraction.widget import needs_result_save
        task=self.main._add_task('https://example.com/video','link','保存重试')
        task.id='gui-job-receipt-review'
        task.execution_policy=replace(task.execution_policy,requested_outputs=('text','video'))
        self.main._task_queue_recovery=GuiTaskRecoveryStore(self.root/'recovery.json')
        self.main._task_queue_recovery_checked=True
        video=self.root/'video.mp4';video.write_bytes(b'preserved video')
        with patch.object(extraction,'save_result',side_effect=OSError('receipt storage unavailable')):
            self.main._commit_extraction_result(task,Transcript(text='已完成的正文',meta={'extraction_assets':{'video':str(video)}}))
        identity=task.library_id or task._pending_library_id
        self.assertIn('已完成的正文',library.load_markdown(identity))
        self.assertEqual(task.message,'结果待保存')
        self.assertTrue(needs_result_save(task))
        self.assertTrue(extraction.has_unsaved_result(task))
        # Restart before the result receipt was committed; retain the original document identity.
        recovered,found=self.main._task_queue_recovery.merge(TaskQueueSnapshot((),()))
        self.assertTrue(found)
        restored=recovered.tasks[0]
        self.assertEqual(restored._pending_library_id,identity)
        self.assertEqual(restored.transcript.meta['extraction_assets']['video'],str(video))
        self.main.tasks=[restored]
        library.save_markdown(identity,'用户已编辑的正文')
        self.widget.publish(force=True)
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.widget.dispatch('start',{'ids':[self.widget._ids[restored.id]]})
            self.assertEqual(len(_FakeWorker.instances),0)
        self.assertEqual(restored.state,TaskState.DONE)
        self.assertEqual(restored.library_id,identity)
        self.assertEqual(library.load_markdown(identity),'用户已编辑的正文')
        self.assertEqual(video.read_bytes(),b'preserved video')
        self.assertEqual(extraction.load_result(restored.id).meta['extraction_library_id'],identity)
        self.assertEqual(len(list((self.root/'library').glob('transcript-*.json'))),1)

    def test_cancelled_group_member_leaves_visible_resume_for_remaining(self):
        from src.core.task import TaskGroup,TaskGroupMember
        first=self.main._add_task('https://example.com/1','link','第一项')
        second=self.main._add_task('https://example.com/2','link','第二项')
        self.main.task_groups=[TaskGroup(id=1,name='批量',members=[TaskGroupMember(stable_key='',title=t.title,
            source=t.source,kind=t.kind,task_id=t.id) for t in (first,second)],expanded=True)]
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.main._start_all()
            self.main._cancel_task(first.id)
            _FakeWorker.instances[0].finish_thread()
            self.assertEqual(first.state,TaskState.CANCELLED)
            self.assertEqual(second.state,TaskState.QUEUED)
            self.main._delete_task(first.id)
            self.widget.publish(force=True)
            self.until(lambda:self.js('tasks.length')==1)
            self.assertTrue(self.js('isRecoverable(tasks[0])'))
            self.assertIn('已暂停',self.js('document.querySelector(".group-status").textContent'))
            self.assertIn('继续这组',self.js('document.querySelector(".group-actions").textContent'))
            self.js('document.querySelector(".group-actions .primary").click()')
            self.until(lambda:second.state==TaskState.RUNNING)
            self.assertNotIn(1,self.main._paused_task_group_ids)
            self.main._request_task_cancel(second.id)
            _FakeWorker.instances[-1].finish_thread()

    def test_retry_elapsed_excludes_idle_time(self):
        task=self.main._add_task('https://example.com/elapsed','link','用时')
        task.state=TaskState.RUNNING
        with patch('src.ui.extraction.widget.time.monotonic',return_value=100):self.widget.rows()
        with patch('src.ui.extraction.widget.time.monotonic',return_value=110):self.assertEqual(self.widget.rows()[0]['elapsed'],10)
        task.state=TaskState.FAILED
        with patch('src.ui.extraction.widget.time.monotonic',return_value=111):self.widget.rows()
        task.state=TaskState.RUNNING
        with patch('src.ui.extraction.widget.time.monotonic',return_value=1000):self.assertEqual(self.widget.rows()[0]['elapsed'],0)

    def test_group_resume_preserves_cancelled_failed_and_completed_members(self):
        self._check_resume_preserves_stopped_members('.group-actions .primary')

    def test_resume_all_preserves_cancelled_failed_and_completed_members(self):
        self._check_resume_preserves_stopped_members('button[onclick="startAll()"]')

    def _check_resume_preserves_stopped_members(self,selector):
        from src.core.task import TaskGroup,TaskGroupMember
        members=[self.main._add_task(f'https://example.com/{i}','link',f'任务 {i}') for i in range(5)]
        first,second,failed,done,waiting=members
        failed.state=TaskState.FAILED;failed.error='保留失败原因'
        done.state=TaskState.DONE
        self.main.task_groups=[TaskGroup(id=1,name='批量',members=[TaskGroupMember(stable_key='',title=t.title,
            source=t.source,kind=t.kind,task_id=t.id) for t in members],expanded=True)]
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.main._start_task(first.id);self.main._start_task(second.id)
            self.main._cancel_task(first.id);_FakeWorker.instances[0].finish_thread()
            self.assertEqual(first.state,TaskState.CANCELLED)
            self.assertEqual(second.state,TaskState.QUEUED)
            self.widget.publish(force=True)
            self.until(lambda:self.js('tasks.filter(isRecoverable).length')==1)
            self.js(f'document.querySelector({json.dumps(selector)}).click()')
            self.until(lambda:len(_FakeWorker.instances)>1)
            try:
                self.assertEqual(first.state,TaskState.CANCELLED)
                self.assertEqual(failed.state,TaskState.FAILED)
                self.assertEqual(failed.error,'保留失败原因')
                self.assertEqual(done.state,TaskState.DONE)
                self.assertEqual(waiting.state,TaskState.PENDING)
                self.assertEqual(second.state,TaskState.RUNNING)
                self.assertEqual(_FakeWorker.instances[-1].source,second.source)
            finally:
                self.main._cancel_task(self.main.current[0].id)
                _FakeWorker.instances[-1].finish_thread()

    def test_recording_paths_do_not_reuse_same_second(self):
        from datetime import datetime
        from pathlib import Path
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,10,17,12,0,0)
        with patch('datetime.datetime',FrozenDateTime):
            for prefix in ('录音','系统声音录音','混合录音'):
                first=Path(self.main._recording_path(prefix));first.write_bytes(b'original recording')
                second=Path(self.main._recording_path(prefix))
                self.assertNotEqual(first,second)
                self.assertEqual(first.read_bytes(),b'original recording')

    def test_repeated_receipt_failure_only_retries_save_for_text_and_assets(self):
        from src.core import extraction
        from src.ui.extraction.widget import needs_result_save
        for chosen in (('text','video','cover'),('video','cover')):
            with self.subTest(outputs=chosen):
                task=self.main._add_task('https://example.com/retry','link','连续补保存')
                task.execution_policy=replace(task.execution_policy,requested_outputs=chosen)
                assets={key:str(self.root/f'{task.id}-{key}') for key in ('video','cover')}
                for path in assets.values():Path(path).write_bytes(b'keep original asset')
                transcript=Transcript(text='完整正文' if 'text' in chosen else '',meta={'extraction_assets':assets})
                with patch.object(extraction,'save_result',side_effect=OSError('receipt unavailable')),patch('src.ui.worker.TranscribeWorker',_FakeWorker):
                    self.main._commit_extraction_result(task,transcript)
                    identity=getattr(task,'_pending_library_id',None)
                    if identity:library.save_markdown(identity,'用户编辑保留')
                    self.widget.publish(force=True)
                    self.widget.dispatch('start',{'ids':[self.widget._ids[task.id]]})
                    self.assertTrue(needs_result_save(task))
                    self.assertEqual(getattr(task,'_pending_library_id',None),identity)
                    self.assertEqual(len(_FakeWorker.instances),0)
                with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
                    self.widget.dispatch('start',{'ids':[self.widget._ids[task.id]]})
                    self.assertEqual(len(_FakeWorker.instances),0)
                self.assertEqual(task.state,TaskState.DONE)
                self.assertFalse(needs_result_save(task))
                self.assertEqual(extraction.load_result(str(task.id)).meta['extraction_assets'],assets)
                if identity:
                    self.assertEqual(task.library_id,identity)
                    self.assertEqual(library.load_markdown(identity),'用户编辑保留')
                else:self.assertIsNone(task.library_id)
                for path in assets.values():self.assertEqual(Path(path).read_bytes(),b'keep original asset')

    def test_real_failure_and_web_retry_reset_elapsed(self):
        task=self.main._add_task('https://example.com/clock','link','实际重试')
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.main._start_task(task.id)
            self.widget._started[task.id]=time.monotonic()-900
            _FakeWorker.instances[0].finish_failed()
            self.assertEqual(task.state,TaskState.FAILED)
            self.widget.publish(force=True)
            self.until(lambda:self.js('tasks[0].state')=='error')
            self.js('document.querySelector(".task-start").click()')
            self.until(lambda:task.state==TaskState.RUNNING)
            try:self.assertLess(self.widget.rows()[0]['elapsed'],5)
            finally:
                self.main._cancel_task(task.id);_FakeWorker.instances[-1].finish_thread()

    def test_incomplete_moss_result_fails_real_worker_without_success_receipt(self):
        from src.core import extraction
        from src.core.engines.local_native import NativeASREngine, parse_moss
        from tests.test_native_asr import NativeASRTests
        source = self.root/'fixture.wav'
        source.write_bytes(b'preserved test input')
        library.save(Transcript(text='已有文稿保留'), '已有文稿')
        before = {p.name:p.read_bytes() for p in (self.root/'library').glob('*.md')}
        self.main.cfg.engine_name = 'local_moss_q5'
        task = self.main._add_task(str(source), 'file', '异常 MOSS 输出')
        task.execution_policy = replace(task.execution_policy, requested_outputs=('text',))
        row = NativeASRTests().row()
        row.update(file=str(source), raw_text=row['raw_text']+'[2.00][S03]未闭合尾段')
        engine = NativeASREngine('local_moss_q5')
        def acquire(*args, **kwargs):
            return engine.transcribe(source)
        with patch.object(engine, '_transcribe',
                          side_effect=lambda *a:parse_moss(json.dumps(row), source, 3)), \
                patch.object(extraction, 'acquire_source', side_effect=acquire):
            self.main._start_task(task.id)
            self.until(lambda:self.main.current is None)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertIsNone(task.library_id)
        self.assertIsNone(task.transcript)
        with self.assertRaises(FileNotFoundError):
            extraction.load_result(str(task.id))
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'library').glob('*.md')}, before)
        self.assertEqual(source.read_bytes(), b'preserved test input')
        recovered = list((self.root/'data/incomplete-transcriptions').glob('*.txt'))
        self.assertEqual(len(recovered), 1)
        self.assertIn('未闭合尾段', recovered[0].read_text())
        self.widget.publish(force=True)
        self.until(lambda:self.js('tasks[0].state') == 'error')

    def test_paused_group_does_not_block_other_group_or_independent_task(self):
        from src.core.task import TaskGroup,TaskGroupMember
        tasks=[self.main._add_task(f'https://example.com/queue/{i}','link',f'任务 {i}') for i in range(4)]
        first,second,other,single=tasks
        self.main.task_groups=[TaskGroup(id=gid,name=f'批次 {gid}',members=[TaskGroupMember(stable_key='',title=t.title,
            source=t.source,kind=t.kind,task_id=t.id) for t in members]) for gid,members in ((1,(first,second)),(2,(other,)))]
        with patch('src.ui.worker.TranscribeWorker',_FakeWorker):
            self.main._start_all()
            self.main._cancel_task(first.id);_FakeWorker.instances[-1].finish_thread()
            self.assertEqual(second.state,TaskState.QUEUED)
            self.assertEqual(other.state,TaskState.RUNNING)
            _FakeWorker.instances[-1].finish_failed()
            self.assertEqual(single.state,TaskState.RUNNING)
            _FakeWorker.instances[-1].finish_failed()
            self.assertIsNone(self.main.current)
            self.widget.publish(force=True)
            self.until(lambda:self.js('tasks.filter(isRecoverable).length')==1)
            selector=f'[data-group="{self.widget._ids[first.id]}"] .group-actions .primary'
            self.js(f'document.querySelector({json.dumps(selector)}).click()')
            self.until(lambda:second.state==TaskState.RUNNING)
            self.assertEqual(first.state,TaskState.CANCELLED)
            self.main._cancel_task(second.id);_FakeWorker.instances[-1].finish_thread()
