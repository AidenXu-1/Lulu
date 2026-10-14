"""Extraction uses temporary fixtures and injected download/model/cover services."""
from dataclasses import replace
import hashlib
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from src.core import extraction
from src.core.models import Transcript,TranscriptCover
from src.core.task import TaskExecutionPolicy,Task,TaskState
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.collect.cover import CoverReceipt
from src.core.task_queue_store import _policy_to_dict,_policy_from_dict,SCHEMA_VERSION

class ExtractionOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.video=self.root/'source.mp4';self.video.write_bytes(b'video fixture')
        self.cover=self.root/'managed/douyin/test.jpg';self.cover.parent.mkdir(parents=True);self.cover.write_bytes(b'cover fixture')
        self.meta=TranscriptCover(platform='douyin',work_id='123',url='https://p3-sign.douyinpic.com/test.jpg',status='url_only').merge_meta({'title':'测试作品'})
        self.receipt=CoverReceipt(status='downloaded',relative_path='douyin/test.jpg',size=self.cover.stat().st_size,sha256=hashlib.sha256(self.cover.read_bytes()).hexdigest(),mime_type='image/jpeg')
        self.localizer=SimpleNamespace(root=self.root/'managed',localize=lambda **kw:self.receipt)
    def policy(self,chosen):
        return TaskExecutionPolicy('single_link','materialize','',MaterialPreferenceSnapshot('video' if 'video' in chosen else 'audio','video' in chosen,'cover' in chosen),'user_directory',str(self.root/'out'),False,None,tuple(chosen))
    def download(self,source,directory,progress,meta,cancelled):meta.update(self.meta);return self.video
    def text(self,*a,**kw):return Transcript(text='识别文字',meta=dict(self.meta))
    def run_case(self,chosen,**overrides):
        with patch.object(extraction,'download_video',side_effect=self.download) as video,patch.object(extraction.pipeline,'transcribe_source',side_effect=overrides.get('text',self.text)) as text,patch.object(extraction,'metadata',return_value=dict(self.meta)) as metadata:
            result=extraction.acquire_source('https://www.douyin.com/video/123',execution_policy=self.policy(chosen),engine_name='local_sensevoice',cover_workflow=self.localizer,**{k:v for k,v in overrides.items() if k!='text'})
            return result,video.call_count,text.call_count,metadata.call_count
    def test_all_seven_combinations_only_do_requested_work(self):
        for count in range(1,4):
            for selected in combinations(('text','video','cover'),count):
                with self.subTest(selected=selected):
                    result,video,text,metadata=self.run_case(selected)
                    self.assertEqual(video,int('video' in selected));self.assertEqual(text,int('text' in selected))
                    self.assertEqual(bool(result.text),'text' in selected)
                    self.assertEqual(set(result.meta['extraction_assets']),set(selected)-{'text'})
                    self.assertEqual(result.meta['extraction_errors'],[])
                    for file in result.meta['extraction_assets'].values():self.assertTrue(Path(file).is_file())
                    self.assertEqual(self.video.read_bytes(),b'video fixture')
    def test_subtitles_do_not_skip_requested_video(self):
        with patch.object(extraction,'download_video',side_effect=self.download) as video,patch.object(extraction.pipeline,'transcribe_source') as text,patch('src.core.subtitles.fetch_from_link',return_value=Transcript(text='已有字幕')):
            result=extraction.acquire_source('https://www.douyin.com/video/123',execution_policy=replace(self.policy(('text','video')),prefer_subtitles=True),engine_name='local_sensevoice')
            video.assert_called_once();text.assert_not_called();self.assertEqual(result.text,'已有字幕');self.assertTrue(Path(result.saved_media).is_file())
    def test_text_failure_preserves_selected_video_and_cover(self):
        def failed(*args,**kwargs):raise RuntimeError('fixture model error')
        result,*_=self.run_case(('text','video','cover'),text=failed)
        self.assertEqual(set(result.meta['extraction_assets']),{'video','cover'})
        self.assertIn('fixture model error',''.join(result.meta['extraction_errors']))

    def test_empty_selection_and_cancel_have_no_side_effects(self):
        with patch.object(extraction,'download_video') as download,patch.object(extraction.pipeline,'transcribe_source') as text:
            with self.assertRaises(ValueError):extraction.acquire_source('https://x.test',execution_policy=self.policy(()),engine_name='local_sensevoice')
            with self.assertRaises(extraction.pipeline._Cancelled):extraction.acquire_source('https://x.test',execution_policy=self.policy(('video',)),engine_name='local_sensevoice',is_cancelled=lambda:True)
            download.assert_not_called();text.assert_not_called()
    def test_result_receipt_roundtrip_and_non_overwriting_publication(self):
        result,*_=self.run_case(('video',));first=result.saved_media
        result2,*_=self.run_case(('video',));self.assertNotEqual(first,result2.saved_media)
        ref=extraction.save_result('gui-job-test',result,self.root/'receipts');self.assertEqual(ref,'lulu-extraction:gui-job-test')
        self.assertEqual(extraction.load_result('gui-job-test',self.root/'receipts'),result)
        with self.assertRaises(ValueError):extraction.load_result('../test',self.root)
    def test_policy_backward_compatibility_and_outputs_roundtrip(self):
        p=self.policy(('text','cover'));raw=_policy_to_dict(p)
        self.assertEqual(_policy_from_dict(raw,schema_version=SCHEMA_VERSION),p)
        del raw['requested_outputs'];self.assertIsNone(_policy_from_dict(raw,schema_version=SCHEMA_VERSION).requested_outputs)
    def test_cover_failure_keeps_successful_video(self):
        self.localizer.localize=lambda **kw:CoverReceipt.failed('cover_download_failed')
        result,*_=self.run_case(('video','cover'))
        self.assertTrue(Path(result.saved_media).is_file());self.assertTrue(result.meta['extraction_errors'])

class ExtractionLedgerTests(unittest.TestCase):
    def test_draft_edit_start_freeze_late_model_binding_retry_and_reload(self):
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.job_ledger.service import JobControlService
        from src.core.gui_task_ledger import CoreTaskQueueStore
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
        from tests.test_shared_core_gui_task_ledger import _runtime,_dispatcher
        with TemporaryDirectory() as directory:
            ledger=JobLedgerStore(Path(directory).resolve()/'jobs',FakeIntegrityAnchorStore())
            queue=CoreTaskQueueStore(_dispatcher(_runtime(JobControlService(ledger),('gui-job-one','gui-attempt-one','gui-lineage-one'))))
            policy=TaskExecutionPolicy('single_link','materialize','',MaterialPreferenceSnapshot('audio',False,False),'user_directory',directory,False,None,('text',))
            t=queue.register(Task(1,'test','https://www.douyin.com/video/123','link',execution_policy=policy))
            t.execution_policy=replace(policy,requested_outputs=('cover',),material_snapshot=MaterialPreferenceSnapshot('audio',False,True))
            queue.save([t],[])
            self.assertEqual(queue.load().tasks[0].execution_policy.requested_outputs,('cover',))
            t.state=TaskState.QUEUED;queue.save([t],[])
            frozen=t.execution_policy
            t.execution_policy=replace(frozen,requested_outputs=('video',));t.state=TaskState.RUNNING
            with self.assertRaises(Exception):queue.save([t],[])
            t.execution_policy=replace(frozen,engine_name='local_sensevoice');queue.save([t],[])
            self.assertEqual(queue.load().tasks[0].execution_policy.engine_name,'local_sensevoice')
            t.state=TaskState.FAILED;t.error='fixture failure';queue.save([t],[])
            t.state=TaskState.PENDING;t.execution_policy=replace(policy,requested_outputs=('video',),material_snapshot=MaterialPreferenceSnapshot('video',True,False));queue.save([t],[])
            restored=queue.load().tasks[0];self.assertEqual(restored.execution_policy.requested_outputs,('video',));self.assertIsNone(restored.execution_policy.engine_name)
            t.state=TaskState.QUEUED;queue.save([t],[])
            t.execution_policy=replace(t.execution_policy,engine_name='local_whisper_mlx');t.state=TaskState.RUNNING;queue.save([t],[])
            with patch('src.core.models_manager.app_data_dir',return_value=Path(directory).resolve()/'data'):
                t.transcript=Transcript(text='',meta={'extraction_assets':{'video':str(Path(directory)/'video.mp4')},'extraction_errors':[]})
                t._extraction_result_ref=extraction.save_result(t.id,t.transcript)
                t.state=TaskState.DONE;t.error=None;queue.save([t],[])
                restored=queue.load().tasks[0]
                self.assertEqual(restored.state,TaskState.DONE)
                self.assertEqual(restored.transcript,t.transcript)
                self.assertFalse(extraction.has_unsaved_result(restored))

class ExtractionRecoveryTests(unittest.TestCase):
    def test_asset_only_partial_receipt_wins_over_stale_recovery_draft(self):
        from src.core.gui_task_ledger import _task_from_job, _task_payload
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.task_queue_store import TaskQueueSnapshot
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            policy = TaskExecutionPolicy('single_link','materialize','',MaterialPreferenceSnapshot('video',True,True),'user_directory',str(root),False,'local_sensevoice',('video','cover'))
            task = Task('gui-job-result', '作品', 'https://www.douyin.com/video/123','link',execution_policy=policy)
            task.transcript = Transcript(text='',meta={'extraction_assets':{'video':str(root/'video.mp4')},'extraction_errors':['封面保存失败']})
            task.state = TaskState.DONE
            recovery = GuiTaskRecoveryStore(root/'recovery/unsynced.json')
            recovery.save([task],[])
            with patch('src.core.models_manager.app_data_dir',return_value=root):
                ref = extraction.save_result(task.id,task.transcript)
                job = dict(job_id=task.id,job_revision=3,state='partial',job_kind='gui_transcription',input_ref='',gui_task=_task_payload(task),result_ref=ref,created_by='gui')
                restored = _task_from_job(job)
                merged, found = recovery.merge(TaskQueueSnapshot((restored,),()))
                self.assertTrue(found)
                self.assertEqual(merged.tasks[0].state,TaskState.DONE)
                self.assertEqual(merged.tasks[0].message,'部分完成')
                self.assertEqual(merged.tasks[0].transcript,task.transcript)
                self.assertFalse(extraction.has_unsaved_result(merged.tasks[0]))
                (root/'extraction-results'/f'{task.id}.json').chmod(0o644)
                self.assertIn('无法读取',_task_from_job(job).message)

    def test_subtitle_failure_falls_back_without_losing_video(self):
        case = ExtractionOutputTests(); case.setUp()
        try:
            with patch.object(extraction,'download_video',side_effect=case.download),patch.object(extraction.pipeline,'transcribe_source',side_effect=case.text) as text,patch('src.core.subtitles.fetch_from_link',side_effect=RuntimeError('字幕不可用')):
                result=extraction.acquire_source('https://www.douyin.com/video/123',execution_policy=replace(case.policy(('text','video')),prefer_subtitles=True),engine_name='local_sensevoice')
                text.assert_called_once()
                self.assertEqual(result.text,'识别文字')
                self.assertTrue(Path(result.saved_media).is_file())
        finally: case.doCleanups()

    def test_audio_only_download_cannot_be_published_as_video(self):
        with TemporaryDirectory() as directory:
            audio = Path(directory)/'podcast.mp3'; audio.write_bytes(b'fixture')
            with patch.object(extraction.extractor,'_download_via_ytdlp',return_value=audio):
                with self.assertRaisesRegex(ValueError,'仅提供音频'):
                    extraction._video_download('https://audio.example/episode',Path(directory),lambda *args:None,{})

    def test_extraction_reference_is_bound_to_its_task_during_upgrade(self):
        from src.core.agent_entry.task_handover import validate_records
        from tests.test_task_handover import record
        row=record(); row['result_ref']='lulu-extraction:job-old'
        self.assertEqual(validate_records([row]),[row])
        for invalid in ('lulu-extraction:other','lulu-extraction:../job-old'):
            with self.subTest(invalid=invalid),self.assertRaises(RuntimeError):
                validate_records([dict(row,result_ref=invalid)])
