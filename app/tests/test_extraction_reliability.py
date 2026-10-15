"""Isolated audit regressions: no real network, keys, model or user files."""
import sys
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from src.core import pipeline
from src.core.audio import extractor, converter
from src.core.collect.media import CollectionMediaAssociationService
from tests import test_collection_media_association as media_tests
from tests.test_collection_media_association import item
from tests import test_task_groups as group_tests
from src.core.task import TaskState

class DownloadCancellationTests(unittest.TestCase):

    def test_converter_cancellation_reaps_child(self):
        real_popen = subprocess.Popen
        children = []
        cancel = threading.Event()
        def launch(*_args, **_kwargs):
            child = real_popen([sys.executable, '-c', 'import time; time.sleep(20)'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(child); cancel.set(); return child
        with tempfile.TemporaryDirectory() as tmp, patch.object(converter.subprocess, 'Popen', side_effect=launch), patch.object(converter, '_ffmpeg_exe', return_value='ffmpeg'):
            with self.assertRaises(InterruptedError):
                converter.to_wav_16k_mono(Path(tmp)/'input.wav', Path(tmp)/'out.wav', is_cancelled=cancel.is_set)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)

class MaterialCancellationTests(unittest.TestCase):
    setUp = media_tests.ManagedMediaTests.setUp
    source = media_tests.ManagedMediaTests.source
    policy = media_tests.ManagedMediaTests.policy

class RetryInputTests(unittest.TestCase):
    setUpClass = group_tests.TaskGroupRuntimeTests.__dict__['setUpClass']
    setUp = group_tests.TaskGroupRuntimeTests.setUp
    tearDown = group_tests.TaskGroupRuntimeTests.tearDown
    _make_group = group_tests.TaskGroupRuntimeTests._make_group

    def test_old_worker_notifications_cannot_change_retry_attempt(self):
        group=self._make_group(2)
        task=self.window._task(group.members[0].task_id)
        with patch('src.ui.worker.TranscribeWorker',group_tests._FakeWorker):
            self.window._start_task(task.id)
            old=self.window.current[1]
            old.finish_failed()
            self.window._retry_task_group(group.id)
            self.window._start_task(task.id)
            current=self.window.current[1]
            old.progress.emit('late',0.8);old.failed.emit('late failure')
            from src.core.models import Transcript
            old.finished_ok.emit(Transcript(text='late body'))
            self.assertEqual(task.state,TaskState.RUNNING)
            self.assertIsNone(task.transcript)
            self.assertIs(self.window.current[1],current)
            current.finish_cancelled()


class DurablePolicyTests(unittest.TestCase):
    def test_started_policy_survives_real_ledger_reload_and_cannot_drift(self):
        from dataclasses import replace
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.job_ledger.service import JobControlService
        from src.core.gui_task_ledger import CoreTaskQueueStore
        from src.core.task import Task, TaskExecutionPolicy
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
        from tests.test_shared_core_gui_task_ledger import _runtime
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); anchor=FakeIntegrityAnchorStore()
            runtime=_runtime(JobControlService(JobLedgerStore(root/'jobs',anchor)),('attempt-a','lineage-a'))
            def dispatch(cmd,args):
                if cmd=='gui.jobs.batch':
                    return {'results':[dispatch(op['command'],op['arguments']) for op in args['operations']], 'error':None}
                return getattr(runtime,cmd.replace('.','_'))(**args)
            queue=CoreTaskQueueStore(dispatch,async_write=True);queue.load()
            task=queue.prepare(Task(1,'fixture','https://example.invalid/work','link'))
            queue.save([task],[])
            task.execution_policy=TaskExecutionPolicy('single_link','materialize','',MaterialPreferenceSnapshot(), 'user_directory', engine_name='local_sensevoice')
            task.state=TaskState.QUEUED;queue.save([task],[])
            runtime=_runtime(JobControlService(JobLedgerStore(root/'jobs',anchor)))
            restored=CoreTaskQueueStore(dispatch,async_write=True).load().tasks[0]
            self.assertEqual(restored.execution_policy,task.execution_policy)
            task.state=TaskState.RUNNING;queue.save([task],[])
            task.state=TaskState.FAILED;queue.save([task],[])
            task.execution_policy=replace(task.execution_policy,engine_name='different')
            task.state=TaskState.QUEUED
            with self.assertRaises(Exception):queue.save([task],[])

class ResultRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_frontend_functional_repair import FrontendFunctionalRepairTests
        FrontendFunctionalRepairTests.setUpClass.__func__(cls)
    def setUp(self):
        from tests.test_frontend_functional_repair import FrontendFunctionalRepairTests
        FrontendFunctionalRepairTests.setUp(self)
    def tearDown(self):
        from tests.test_frontend_functional_repair import FrontendFunctionalRepairTests
        FrontendFunctionalRepairTests.tearDown(self)
    def test_failed_save_recovers_body_and_same_result_id_without_duplicate(self):
        from src.core import library
        from src.core.task import Task
        from src.core.models import Transcript
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task_queue_store import TaskQueueSnapshot
        task=Task('gui-job-test','fixture','https://example.invalid/work','link')
        self.window.tasks=[task]
        self.window._task_queue_recovery=GuiTaskRecoveryStore(self.root/'recovery.json')
        self.window._task_queue_recovery_checked=True
        body=Transcript(text='isolated complete result',source=task.source)
        with patch.object(library,'save',side_effect=OSError('disk full')):
            self.window._on_done(task.id,body)
        self.assertEqual(task.state,TaskState.FAILED)
        pending=task._pending_library_id
        snapshot,found=self.window._task_queue_recovery.merge(TaskQueueSnapshot((),()))
        self.assertTrue(found)
        restored=snapshot.tasks[0]
        self.assertEqual(restored.transcript.text,body.text)
        self.assertEqual(restored._pending_library_id,pending)
        self.window.tasks=[restored]
        self.assertTrue(self.window._retry_result_save(restored.id))
        self.assertEqual(restored.library_id,pending)
        # Simulate response lost after successful commit, then replay same input.
        self.assertEqual(library.save(body,'fixture',entry_id=pending),pending)
        self.assertEqual(len(list((self.root/'library').glob('transcript-*.json'))),1)
        # A restart after library commit but before Core acknowledgement keeps the receipt.
        stale=Task(task.id,task.title,task.source,task.kind,state=TaskState.FAILED)
        recovered=self.window._task_queue_recovery.merge(TaskQueueSnapshot((stale,),()))[0].tasks[0]
        self.assertEqual(recovered._pending_library_id,pending)
        self.assertEqual(recovered.transcript.text,body.text)
        library.save_markdown(pending,'user edited text')
        self.assertEqual(library.save(body,'fixture',entry_id=pending),pending)
        self.assertEqual(library.load_markdown(pending),'user edited text')

        self.assertEqual(self.window._task_queue_recovery.merge(TaskQueueSnapshot((restored,),()))[0].tasks[0].state,TaskState.DONE)

class CoordinationTests(unittest.TestCase):
    setUp = media_tests.ManagedMediaTests.setUp
    source = media_tests.ManagedMediaTests.source
    policy = media_tests.ManagedMediaTests.policy


    def test_waiting_pipeline_reuses_commit_then_releases_before_asr(self):
        from concurrent.futures import ThreadPoolExecutor
        from src.core.collect import media_coordination
        from src.core.collect.media import resolve_managed_transcription_media
        from src.core.models import Transcript
        from src.core.task import TaskExecutionPolicy
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        from types import SimpleNamespace
        original=item('douyin','1');self.repo.upsert([original])
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        def fetch(*_):
            entered.set()
            if not release.wait(2): raise TimeoutError()
            return self.source()
        service=CollectionMediaAssociationService(self.repo,self.store,fetch)
        policy=TaskExecutionPolicy.transcription_only(origin='homepage_batch_transcribe',stable_key=original.stable_key,snapshot=MaterialPreferenceSnapshot())
        def lookup(key):
            return resolve_managed_transcription_media(key,repository=self.repo,store=self.store)
        def asr(*_):
            self.assertEqual(media_coordination._registry,{})
            return Transcript(text='fixture')
        with patch.object(pipeline,'resolve_managed_transcription_media',side_effect=lookup), patch.object(pipeline,'resolve_source',side_effect=lambda source,*a,**kw: Path(source)) as resolver, patch.object(pipeline,'to_wav_16k_mono',side_effect=lambda src,dst,**kw:src), patch.object(pipeline,'get_engine',return_value=SimpleNamespace(display_name='fixture',transcribe=asr)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(service.run,[original],policy=self.policy())
                self.assertTrue(entered.wait(1))
                second=pool.submit(pipeline.transcribe_source,'https://example.invalid/work',execution_policy=policy,stage_changed=lambda stage: waiting.set() if stage=='waiting_media' else None)
                self.assertTrue(waiting.wait(1)); self.assertFalse(second.done())
                release.set();first.result(2)
                self.assertEqual(second.result(2).text,'fixture')
        self.assertTrue(Path(resolver.call_args.args[0]).is_absolute())


class HistoryRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_collection_history_ui import CollectionHistoryUiTests
        CollectionHistoryUiTests.setUpClass.__func__(cls)
    def setUp(self):
        from tests.test_collection_history_ui import CollectionHistoryUiTests
        CollectionHistoryUiTests.setUp(self)
    def tearDown(self):
        from tests.test_collection_history_ui import CollectionHistoryUiTests
        CollectionHistoryUiTests.tearDown(self)
    def test_failed_history_save_can_retry_same_result_without_restart(self):
        from tests.test_collection_history_ui import XHS_URL,item
        from src.core.collect.application import CollectionCommand
        from src.core.collect.contracts import CollectResult
        from src.core.collect.history import HistoryError
        token=1
        self.page._history_context[token]=(CollectionCommand(XHS_URL,'profile_public'),self.service.inspect(XHS_URL).identity)
        result=CollectResult.complete([item('1')])
        with patch.object(self.repo,'add_record',side_effect=HistoryError('disk_full')):
            self.assertFalse(self.page._persist_history_result(result,token)[1])
        self.assertIn(token,self.page._history_context)
        self.assertTrue(self.page._persist_history_result(result,token)[1])
        self.assertEqual(len(self.repo.load().active_records),1)
        self.assertNotIn(token,self.page._history_context)
        # A later successful collection also retries an earlier unsaved batch.
        self.page._history_context[2]=(CollectionCommand(XHS_URL,'profile_public'),self.service.inspect(XHS_URL).identity)
        with patch.object(self.repo,'add_record',side_effect=HistoryError('disk_full')):
            self.assertFalse(self.page._persist_history_result(CollectResult.complete([item('2')]),2)[1])
        self.page._history_context[3]=(CollectionCommand(XHS_URL,'profile_public'),self.service.inspect(XHS_URL).identity)
        self.assertTrue(self.page._persist_history_result(CollectResult.complete([item('3')]),3)[1])
        self.assertEqual(len(self.repo.load().active_records),3)
        self.assertEqual(self.page._pending_history_results,{})
