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
        self.stack=ExitStack();cfg=AppConfig();cfg.engine_name='local_sensevoice';cfg.media_dir=str(self.root/'media');cfg.recording_dir=str(self.root/'recordings')
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
            self.assertEqual(a.execution_policy.engine_name,'local_sensevoice');self.assertIsNone(b.execution_policy.engine_name)
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
