"""Shared GUI/CLI extraction state; temporary library and fake media/model only."""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from src.core import library
from src.core.models import Transcript
from src.core.agent_workspace.batches import BatchService
from tests import test_agent_workspace as workspace_fixtures
from tests import test_cli_expansion as cli_fixtures
from tests.test_live_recording import until


class ExtractionBatchTests(unittest.TestCase):
    def setUp(self):
        workspace_fixtures.AgentWorkspaceTests.setUp(self)
        data_patch=patch('src.core.models_manager.app_data_dir', return_value=self.root/'appdata')
        data_patch.start();self.addCleanup(data_patch.stop)
        self.sequence = 0

    def stage(self, service, count=1, **kwargs):
        return service.stage_extraction('test-stage', [dict(source=f'https://example.com/{i}.mp4',
            title=f'作品{i}', outputs=['text']) for i in range(count)], **kwargs)

    def control(self, service, plan, action, indices=None, **kwargs):
        self.sequence += 1
        current = service.show(plan['batch_id'])
        versions = {str(i):current['items'][i]['version'] for i in indices} if indices is not None else None
        return service.control_extraction(f'control-{self.sequence}', current['batch_id'],
            current['plan_hash'], action, indices, versions, **kwargs)

    def test_cli_stage_discovery_and_mixed_per_item_roundtrip(self):
        runtime = cli_fixtures.ExpansionTests.runtime(self, lambda item,*args:Transcript('正文',[],engine='local_sensevoice_q8'))
        invoke = lambda command,args:cli_fixtures.ExpansionTests.call(self, command,args,runtime)
        catalog = invoke('tools.catalog', {})
        self.assertTrue({'batches.stage','batches.configure','batches.control'} <= {c['command'] for c in catalog['commands']})
        plan = invoke('batches.stage', dict(request_id='cli-choices', engine='local_sensevoice_q8', group=True,
            items=[dict(source='https://example.com/a.mp4',outputs=['text'],prefer_subtitles=True),
                   dict(source='https://example.com/b.mp4',outputs=['video','cover'],prefer_subtitles=False,media_directory=str(self.root))]))
        self.assertEqual(plan['items'][0]['selection']['outputs'],['text'])
        self.assertEqual(plan['items'][1]['selection']['outputs'],['video','cover'])
        self.assertFalse(plan['items'][1]['selection']['prefer_subtitles'])
        self.assertFalse(runtime.expansion_batches().workers)

    invoke = cli_fixtures.ExpansionTests.invoke

    def test_failed_text_preserves_successful_assets_and_only_missing_is_retried(self):
        video = self.root/'saved.mp4';video.write_bytes(b'video')
        calls = []
        def runner(item,batch,*args):
            calls.append(item['requested_outputs'])
            if len(calls)==1:
                return Transcript('',[],engine='',meta={'extraction_assets':{'video':str(video)},'extraction_errors':['文字提取失败']})
            return Transcript('完整文案',[],engine=batch['engine'])
        service = BatchService(None,runner=runner)
        plan = service.stage_extraction('partial', [dict(source='https://example.com/a.mp4',outputs=['text','video'],media_directory=str(self.root))])
        self.control(service,plan,'start',[0],network_consent=True)
        until(lambda:not service.workers)
        item = service.show(plan['batch_id'])['items'][0]
        self.assertEqual(item['assets']['video']['state'],'ready')
        self.assertEqual(item['assets']['text']['state'],'failed')
        self.control(service,plan,'retry',[0],network_consent=True)
        until(lambda:not service.workers)
        item = service.show(plan['batch_id'])['items'][0]
        self.assertEqual(calls,[['text','video'],['text']])
        self.assertTrue(library.load(item['document_id']).text.endswith('完整文案\n'))
        self.assertEqual(item['assets']['video']['path'],str(video))


    def test_deleted_document_can_be_supplemented_without_repeating_saved_media(self):
        video=self.root/'retained.mp4';video.write_bytes(b'video')
        calls=[]
        def runner(item,batch,*args):
            calls.append(item['requested_outputs'])
            return Transcript('补充的文案',[],engine=batch['engine'],
                              meta={'extraction_assets':{'video':str(video)}})
        service=BatchService(None,runner=runner)
        plan=service.stage_extraction('missing-text',[dict(source='https://example.com/a.mp4',
            outputs=['text','video'],media_directory=str(self.root))])
        self.control(service,plan,'start',[0],network_consent=True)
        until(lambda:not service.workers)
        original=service.show(plan['batch_id'])['items'][0]['document_id']
        library.delete(original)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(item['assets']['text']['state'],'failed')
        self.assertIsNone(item.get('document_id'))
        self.assertEqual(item['assets']['video']['state'],'ready')
        # Missing text is an invitation to act, never an automatic new request.
        self.assertEqual(len(calls),1)
        self.control(service,plan,'retry',[0],network_consent=True)
        until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(calls,[['text','video'],['text']])
        self.assertNotEqual(item['document_id'],original)
        self.assertIsNone(library.load(original))
        self.assertEqual(item['assets']['text']['state'],'ready')


    def test_configuration_is_per_work_stale_edits_and_queued_edits_fail(self):
        entered,release = threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def runner(item,batch,*args):
            entered.set();release.wait(4)
            return Transcript('完成',[],engine=batch['engine'])
        service=BatchService(None,runner=runner);plan=self.stage(service,2)
        self.control(service,plan,'start',[0],network_consent=True)
        self.assertTrue(entered.wait(2))
        current=service.show(plan['batch_id']);row=current['items'][1]
        updated=service.configure_extraction('change-1',plan['batch_id'],plan['plan_hash'],
            [dict(index=1,version=row['version'],outputs=['video'],prefer_subtitles=True,media_directory=str(self.root))])
        self.assertEqual(updated['items'][0]['selection']['outputs'],['text'])
        self.assertEqual(updated['items'][1]['selection']['outputs'],['video'])
        with self.assertRaises(library.RevisionConflict):
            service.configure_extraction('change-stale',plan['batch_id'],plan['plan_hash'],
                [dict(index=1,version=row['version'],outputs=['cover'])])
        with self.assertRaises(ValueError):
            service.configure_extraction('change-running',plan['batch_id'],plan['plan_hash'],
                [dict(index=0,version=updated['items'][0]['version'],outputs=['cover'])])
        release.set();until(lambda:not service.workers)

    def test_no_fixed_count_limit_and_paged_results_preserve_identity(self):
        service=BatchService(None,runner=lambda *a:self.fail('准备和查询不能执行'))
        plan=self.stage(service,1201,group=True)
        page=service.show(plan['batch_id'],offset=1198,limit=3)
        self.assertEqual(page['total'],1201)
        self.assertEqual([i['index'] for i in page['items']],[1198,1199,1200])
        self.assertFalse(page['has_more'])
        self.assertEqual(service.list()[0]['total'],1201)

    def test_model_freezes_but_other_drafts_remain_editable_and_local_is_serial(self):
        lock=threading.Lock();active=[0];peak=[0]
        def runner(item,batch,*args):
            with lock:active[0]+=1;peak[0]=max(peak[0],active[0])
            time.sleep(.03)
            with lock:active[0]-=1
            return Transcript('正文',[],engine=batch['engine'])
        service=BatchService(None,runner=runner);plan=self.stage(service,7)
        self.control(service,plan,'start',network_consent=True)
        until(lambda:not service.workers)
        self.assertEqual(peak[0],1)
        with self.assertRaises(ValueError):
            service.configure_extraction('engine-change',plan['batch_id'],plan['plan_hash'],engine='dashscope')

    def test_cloud_parallelism_unknown_results_never_enter_normal_retry(self):
        active=[0];peak=[0];lock=threading.Lock();calls=[]
        def runner(item,batch,*args):
            from src.core.engines.cloud_http import CloudFailure
            with lock:active[0]+=1;peak[0]=max(peak[0],active[0]);calls.append(item['index'])
            time.sleep(.03)
            with lock:active[0]-=1
            if item['index']==0:raise CloudFailure('unknown')
            return Transcript('云端正文',[],engine='dashscope')
        service=BatchService(None,runner=runner);plan=self.stage(service,9,engine='dashscope')
        self.control(service,plan,'start',network_consent=True,cloud_upload_consent=True)
        until(lambda:not service.workers)
        self.assertGreater(peak[0],1)
        self.assertEqual(service.show(plan['batch_id'])['items'][0]['assets']['text']['state'],'unknown')
        before=list(calls)
        self.control(service,plan,'retry',network_consent=True,cloud_upload_consent=True)
        until(lambda:not service.workers)
        self.assertEqual(calls,before)

    def test_unknown_text_does_not_block_an_explicit_media_only_supplement(self):
        from src.core.engines.cloud_http import CloudFailure
        video=self.root/'supplement.mp4';video.write_bytes(b'video')
        calls=[]
        def runner(item,batch,*args):
            calls.append(item['requested_outputs'])
            if len(calls)==1:raise CloudFailure('unknown')
            return Transcript('',[],meta={'extraction_assets':{'video':str(video)}})
        service=BatchService(None,runner=runner);plan=self.stage(service,engine='dashscope')
        self.control(service,plan,'start',[0],network_consent=True,cloud_upload_consent=True)
        until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        service.configure_extraction('supplement',plan['batch_id'],plan['plan_hash'],
            [dict(index=0,version=item['version'],outputs=['text','video'],media_directory=str(self.root))])
        self.control(service,plan,'start',[0],network_consent=True)
        until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(calls,[['text'],['video']])
        self.assertEqual(item['assets']['text']['state'],'unknown')
        self.assertEqual(item['assets']['video']['state'],'ready')

    def test_media_only_uses_no_asr_key_and_empty_choice_is_not_completed(self):
        video=self.root/'saved-video.mp4';video.write_bytes(b'video')
        service=BatchService(None)
        plan=service.stage_extraction('media-only',[dict(source='https://example.com/a.mp4',outputs=['video'],media_directory=str(self.root))],engine='dashscope')
        result=Transcript('',[],meta={'extraction_assets':{'video':str(video)}})
        with patch.object(service,'_prepare_cloud_engine',side_effect=AssertionError('不能读密钥')),patch('src.core.extraction.acquire_source',return_value=result):
            self.control(service,plan,'start',[0],network_consent=True)
            until(lambda:not service.workers)
        self.assertEqual(service.show(plan['batch_id'])['items'][0]['assets']['video']['state'],'ready')
        empty=service.stage_extraction('empty',[dict(source='https://example.com/empty.mp4',outputs=[])])
        self.assertEqual(empty['items'][0]['state'],'draft')

    def test_result_save_failure_recovers_cache_without_second_recognition(self):
        calls=[]
        def runner(item,batch,*args):
            calls.append(item['index']);return Transcript('不可重复收费的正文',[],engine='dashscope')
        service=BatchService(None,runner=runner);plan=self.stage(service,engine='dashscope')
        with patch.object(library,'finish_transcript_document',side_effect=OSError('fixture disk busy')):
            self.control(service,plan,'start',[0],network_consent=True,cloud_upload_consent=True)
            until(lambda:not service.workers)
        self.assertTrue(service.show(plan['batch_id'])['items'][0]['save_pending'])
        self.control(service,plan,'check',[0])
        until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(calls,[0])
        self.assertEqual(item['state'],'succeeded')
        self.assertIn('不可重复收费的正文',library.load(item['document_id']).text)

    def test_readding_unfinished_work_locates_original_and_new_text_is_explicit(self):
        service=BatchService(None,runner=lambda *a:self.fail('创建任务不能执行'))
        first=self.stage(service)
        again=service.stage_extraction('readd',[dict(source=first['items'][0]['source'])])
        item=again['items'][0]
        self.assertEqual(item['state'],'duplicate')
        self.assertEqual(item['existing_task'],dict(batch_id=first['batch_id'],index=0))
        explicit=service.stage_extraction('explicit',[dict(source=item['source'],new_text=True)])
        self.assertEqual(explicit['items'][0]['state'],'draft')

    def test_model_change_replay_is_idempotent_and_bad_choices_do_not_mutate(self):
        service=BatchService(None,runner=lambda *a:self.fail('设置不能执行'))
        plan=self.stage(service)
        args=('model-command',plan['batch_id'],plan['plan_hash'])
        changed=service.configure_extraction(*args,engine='local_whisper_mlx_4bit')
        replay=service.configure_extraction(*args,engine='local_whisper_mlx_4bit')
        self.assertEqual(replay['plan_hash'],changed['plan_hash'])
        for outputs in ([{}],['text','text'],True,'text'):
            with self.assertRaises(ValueError):
                service.configure_extraction('invalid',changed['batch_id'],changed['plan_hash'],
                    [dict(index=0,version=changed['items'][0]['version'],outputs=outputs)])
        self.assertEqual(service.show(plan['batch_id'])['items'][0]['selection']['outputs'],['text'])

    def test_individual_pause_survives_batch_resume_and_removal_undo_keeps_identity(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        calls=[]
        def runner(item,batch,*args):
            calls.append(item['index']);entered.set();release.wait(3)
            return Transcript('正文',[],engine=batch['engine'])
        service=BatchService(None,runner=runner);plan=self.stage(service,4)
        self.control(service,plan,'start',network_consent=True)
        self.assertTrue(entered.wait(2))
        self.control(service,plan,'pause',[1])
        self.control(service,plan,'pause')
        release.set();until(lambda:not service.workers)
        self.control(service,plan,'resume',network_consent=True)
        until(lambda:not service.workers)
        self.assertEqual(calls,[0,2,3])

        before=service.show(plan['batch_id'])
        removed=self.control(service,plan,'dismiss')
        self.assertEqual(removed['visible_total'],0)
        self.assertEqual(removed['total'],0)
        restored=service.control_extraction('undo',plan['batch_id'],plan['plan_hash'],'restore',
            [int(n) for n in removed['dismissed']],removed['dismissed'])
        self.assertEqual(restored['total'],4)
        self.assertEqual([i['document_id'] for i in before['items']],[i['document_id'] for i in restored['items']])
        self.assertEqual(calls,[0,2,3])

    def test_pause_before_first_start_can_resume_after_reopening(self):
        calls=[]
        service=BatchService(None,runner=lambda *a:self.fail('暂停不能执行'))
        plan=self.stage(service)
        self.control(service,plan,'pause',[0])
        reopened=BatchService(None,runner=lambda item,batch,*a:(calls.append(item['index']) or
            Transcript('恢复后正文',[],engine=batch['engine'])))
        self.control(reopened,plan,'resume',[0],network_consent=True)
        until(lambda:not reopened.workers)
        self.assertEqual(calls,[0])
        self.assertEqual(reopened.show(plan['batch_id'])['items'][0]['state'],'succeeded')

    def test_long_valid_request_id_still_saves_a_readable_document(self):
        service=BatchService(None,runner=lambda item,batch,*a:Transcript('完整长编号正文',[],engine=batch['engine']))
        plan=service.stage_extraction('r'*100,[dict(source='https://example.com/long-id.mp4')])
        self.control(service,plan,'start',network_consent=True)
        until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(item['state'],'succeeded',item)
        self.assertLessEqual(len(item['document_id']),160)
        self.assertIn('完整长编号正文',library.load(item['document_id']).text)

    def test_restart_sent_request_becomes_unknown_and_never_replays(self):
        service=BatchService(None,runner=lambda *a:self.fail('未知请求不能自动执行'))
        plan=self.stage(service,engine='dashscope')
        batch=service._read(plan['batch_id']);item=batch['items'][0]
        item.update(state='interrupted',attempt_id='sent-once',attempt_selection=item['selection'],
                    cloud_attempt={'state':'sending'})
        item['assets']['text']['state']='pending'
        service._save(batch)
        restarted=BatchService(None,runner=lambda *a:self.fail('未知请求不能重放'))
        checked=restarted.show(plan['batch_id'])
        self.assertEqual(checked['items'][0]['assets']['text']['state'],'unknown')
        self.control(restarted,plan,'retry',network_consent=True,cloud_upload_consent=True)
        self.assertFalse(restarted.workers)

    def test_two_local_batches_wait_for_the_same_actual_resource(self):
        from src.core.engines.asr_resource import model_use
        active=[0];peak=[0];lock=threading.Lock()
        def runner(item,batch,*args):
            with model_use('fake-model',retain=True,root=self.root/'model-lock'):
                with lock:active[0]+=1;peak[0]=max(peak[0],active[0])
                time.sleep(.04)
                with lock:active[0]-=1
            return Transcript('串行结果',[],engine=batch['engine'])
        service=BatchService(None,runner=runner);first=self.stage(service,3)
        second=service.stage_extraction('second',[dict(source='https://example.com/second.mp4')])
        self.control(service,first,'start',network_consent=True)
        self.control(service,second,'start',network_consent=True)
        until(lambda:not service.workers)
        self.assertEqual(peak[0],1)
        self.assertEqual(service.show(first['batch_id'])['counts']['succeeded'],3)
        self.assertEqual(service.show(second['batch_id'])['counts']['succeeded'],1)

    def test_cli_private_manifest_admits_large_batch_and_checksum_refuses_changes(self):
        import hashlib,json,os
        runtime=cli_fixtures.ExpansionTests.runtime(self,lambda *a:self.fail('清单准备不能执行'))
        manifest=self.root/'works.jsonl'
        payload=''.join(json.dumps(dict(source=f'https://example.com/manifest/{n}.mp4',title='长标题'*50))+'\n'
                        for n in range(2601)).encode()
        manifest.write_bytes(payload);os.chmod(manifest,0o600)
        args=dict(request_id='manifest',items_file=str(manifest),items_sha256=hashlib.sha256(payload).hexdigest(),group=True)
        plan=cli_fixtures.ExpansionTests.call(self,'batches.stage',args,runtime)
        self.assertEqual(plan['total'],2601)
        self.assertLessEqual(len(plan['items']),50)
        manifest.write_bytes(payload+b'{}\n')
        code,reply=self.invoke('batches.stage',dict(args,request_id='changed'),runtime)
        self.assertNotEqual(code,0)
        self.assertEqual(len(runtime.expansion_batches().list()),1)

    def test_homepage_share_and_work_links_stage_the_same_saved_identity(self):
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        collected = CollectedItem('12345', title='同一个作品')
        self.assertTrue(CollectionRepository().upsert_collected([collected]).success)
        service = BatchService(None, runner=lambda *a:self.fail('准备任务不能下载或转录'))
        sources = [collected.work_url, 'https://www.iesdouyin.com/share/video/12345/',
                   'https://www.douyin.com/video/12345/?from=share#detail']
        plan = service.stage_extraction('share-aliases', [dict(source=s, stable_key=collected.stable_key)
                                                        for s in sources], group=True)
        self.assertEqual({r['source'] for r in plan['items']}, {collected.work_url})
        self.assertEqual({r['stable_key'] for r in plan['items']}, {collected.stable_key})
        reopened = BatchService(None).show(plan['batch_id'])
        self.assertEqual(reopened['total'], 3)
        self.assertEqual(len([r for r in reopened['items'] if r['state'] != 'duplicate']), 1)
        self.assertFalse(service.workers)

    def test_homepage_mismatched_identity_still_rejects_without_a_partial_batch(self):
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        collected = CollectedItem('12345', title='必须对应本人作品')
        CollectionRepository().upsert_collected([collected])
        service = BatchService(None, runner=lambda *a:self.fail('错误身份不能执行'))
        for n, source in enumerate(['https://www.iesdouyin.com/share/video/99999/',
                'https://www.douyin.com.evil.example/video/12345',
                'https://example.com/?url=https://www.douyin.com/video/12345',
                'https://www.douyin.com/user/12345', 'https://v.douyin.com/unknown/']):
            with self.subTest(source=source), self.assertRaises(ValueError):
                service.stage_extraction('bad-identity-'+str(n), [
                    dict(source=collected.work_url, stable_key=collected.stable_key),
                    dict(source=source, stable_key=collected.stable_key)], group=True)
        self.assertEqual(service.list(), [])

    def test_homepage_identity_reuses_existing_input_pipeline_and_saves_history_status(self):
        import subprocess
        from types import SimpleNamespace
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        from src.core.live_recording.media import bundled_ffmpeg
        collected=CollectedItem('12345',title='已采集作品',work_url='https://www.douyin.com/video/12345')
        sample=self.root/'sample.mp4'
        subprocess.run([bundled_ffmpeg(),'-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','1','-c:v','libx264','-c:a','aac',str(sample)],check=True)
        fetched=[]
        def fetch(stable_key,destination,progress,meta):
            fetched.append(stable_key)
            output=Path(destination)/'video.mp4';output.write_bytes(sample.read_bytes());return output
        engine=SimpleNamespace(display_name='隔离模型',transcribe=lambda *a,**kw:Transcript('采集完整正文',[],engine='local_sensevoice_q8'))
        repository=CollectionRepository();repository.upsert_collected([collected])
        with patch('src.core.collect.transcription_input.download_collected_input',side_effect=fetch), \
                patch('src.core.pipeline.resolve_source',side_effect=AssertionError('禁止联网回退')), \
                patch('src.core.pipeline.get_engine',return_value=engine):
            service=BatchService(None)
            plan=service.stage_extraction('collected',[dict(source='https://www.iesdouyin.com/share/video/12345/',stable_key=collected.stable_key)])
            self.control(service,plan,'start',network_consent=True)
            until(lambda:not service.workers)
        item=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(item['state'],'succeeded',item)
        self.assertEqual(fetched,[collected.stable_key])
        self.assertEqual(repository.load().items[0].transcript_status,'completed')
        self.assertIn('采集完整正文',library.load(item['document_id']).text)


if __name__=='__main__':unittest.main()
