"""Truthful per-stage progress and stable lifecycle presentation; isolated only."""
import json
import threading
import time
import unittest
from unittest.mock import patch, Mock

from src.core import library, pipeline, extraction
from src.core.models import Transcript
from src.core.task import TaskExecutionPolicy
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.agent_workspace.batches import BatchService
from tests.test_extraction_batches import ExtractionBatchTests
from tests.test_extraction_batches_ui import ExtractionBatchUITests
from tests.test_live_recording import until


class ExtractionProgressTests(ExtractionBatchTests):
    def test_raw_engine_progress_is_separate_from_weighted_legacy_progress(self):
        source=self.root/'input.wav';source.write_bytes(b'isolated audio')
        raw, weighted, stages=[],[],[]
        class Engine:
            display_name='隔离模型'
            def transcribe(self, path, progress, is_cancelled=None):
                progress('分段识别', .5);progress('等待下一段', -1)
                return Transcript('测试正文')
        with patch.object(pipeline,'resolve_source',return_value=source), patch.object(pipeline,'to_wav_16k_mono',return_value=source), patch.object(pipeline,'get_engine',return_value=Engine()):
            pipeline.transcribe_source(str(source),stage_changed=stages.append,
                stage_progress=lambda *v:raw.append(v),progress=lambda *v:weighted.append(v))
        self.assertIn(('分段识别',.5),raw)
        self.assertIn(('等待下一段',-1),raw)
        self.assertIn('transcription',stages)
        self.assertAlmostEqual(next(f for m,f in weighted if m=='分段识别'),.635)

    def test_service_persists_real_stage_unknown_progress_and_final_save_boundary(self):
        entered, release=threading.Event(),threading.Event();self.addCleanup(release.set)
        seen={}
        def acquire(source, **kw):
            seen.update(kw);kw['stage_changed']('transcription')
            kw['stage_progress']('识别到一半',.5);kw['progress']('识别到一半',.635)
            entered.set();release.wait(4)
            kw['stage_progress']('等待最后一段',-1);kw['progress']('等待最后一段',-1)
            return Transcript('正文',[],engine='local_sensevoice_q8')
        service=BatchService(None);plan=self.stage(service)
        with patch('src.core.extraction.acquire_source',side_effect=acquire):
            self.control(service,plan,'start',network_consent=True);self.assertTrue(entered.wait(2))
            item=service.show(plan['batch_id'])['items'][0]
            self.assertEqual(item['activity']['stage'],'text');self.assertEqual(item['activity']['fraction'],.5)
            self.assertEqual(item['activity']['steps'],['prepare','text','save'])
            self.assertEqual(item['activity']['attempt_id'],item['attempt_id'])
            seen['stage_progress']('仍在处理',-1);seen['progress']('仍在处理',-1)
            self.assertIsNone(service.show(plan['batch_id'])['items'][0]['activity']['fraction'])
            original=library.save
            def save(*args,**kwargs):
                current=service.show(plan['batch_id'])['items'][0]
                self.assertEqual(current['activity']['stage'],'save');self.assertEqual(current['state'],'running')
                return original(*args,**kwargs)
            with patch.object(library,'save',side_effect=save):
                release.set();until(lambda:not service.workers)
        final=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(final['state'],'succeeded');self.assertGreaterEqual(final['activity']['finished_at'],final['activity']['started_at'])
        self.assertEqual(service._read(plan['batch_id'])['plan_hash'],plan['plan_hash'])

    def test_cached_save_retry_does_not_reacquire_and_starts_in_save_stage(self):
        calls=[]
        service=BatchService(None,runner=lambda *a:(calls.append(1) or Transcript('可恢复正文',[],engine='local_sensevoice_q8')))
        plan=self.stage(service)
        with patch.object(library,'save',side_effect=OSError('隔离失败')):
            self.control(service,plan,'start',network_consent=True);until(lambda:not service.workers)
        failed=service.show(plan['batch_id'])['items'][0]
        self.assertTrue(failed['activity'].get('finished_at'))
        original=library.save
        def save(*args,**kwargs):
            item=service.show(plan['batch_id'])['items'][0]
            self.assertEqual(item['activity']['step'],'saving')
            self.assertIsNone(item['activity']['fraction'])
            return original(*args,**kwargs)
        with patch.object(library,'save',side_effect=save):
            self.control(service,plan,'check',[0]);until(lambda:not service.workers)
        self.assertEqual(calls,[1]);self.assertEqual(service.show(plan['batch_id'])['items'][0]['state'],'succeeded')

    def test_media_only_steps_do_not_invent_transcription(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def acquire(source,**kw):
            kw['stage_changed']('cover');entered.set();release.wait(4)
            return Transcript('',[],engine='')
        service=BatchService(None)
        plan=service.stage_extraction('only-cover',[dict(source='https://example.com/cover',title='封面',outputs=['cover'],media_directory=str(self.root))])
        with patch('src.core.extraction.acquire_source',side_effect=acquire):
            self.control(service,plan,'start',network_consent=True);self.assertTrue(entered.wait(2))
            item=service.show(plan['batch_id'])['items'][0]
            self.assertEqual(item['activity']['steps'],['prepare','cover','save']);self.assertEqual(item['activity']['stage'],'cover')
            release.set();until(lambda:not service.workers)

    def test_actual_acquisition_forwards_raw_video_and_subtitle_events(self):
        events,stages=[],[]
        media=self.root/'input.mp4';media.write_bytes(b'isolated media')
        def download(source,directory,progress,meta,cancelled):
            progress('下载字节',.3);return media
        def subtitle(source,progress):
            progress('字幕读取',1);return Transcript('平台正文',[],engine='平台字幕')
        policy=TaskExecutionPolicy(origin='single_link',mode='materialize',stable_key='',
            material_snapshot=MaterialPreferenceSnapshot('video',True,False),media_destination='user_directory',
            requested_outputs=('text','video'),media_directory=str(self.root/'output'),prefer_subtitles=True)
        with patch.object(extraction,'download_video',side_effect=download), patch('src.core.subtitles.fetch_from_link',side_effect=subtitle), patch.object(pipeline,'transcribe_source',side_effect=AssertionError('字幕命中不能启动识别')):
            result=extraction.acquire_source('https://example.com/video',execution_policy=policy,engine_name='local_sensevoice_q8',stage_changed=stages.append,stage_progress=lambda *v:events.append(v))
        self.assertIn(('下载字节',.3),events);self.assertIn(('字幕读取',1),events)
        self.assertEqual(stages[:2],['download','subtitles']);self.assertNotIn('transcription',stages)
        self.assertEqual(result.text,'平台正文')

    def test_cloud_markers_are_not_shown_as_measured_recognition_percent(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def acquire(source,**kw):
            kw['stage_changed']('transcription');kw['stage_progress']('等待云端结果',.5);kw['progress']('等待云端结果',.635)
            entered.set();release.wait(4);return Transcript('云端正文',[],engine='dashscope')
        service=BatchService(None);plan=self.stage(service,engine='dashscope')
        with patch.object(service,'_prepare_cloud_engine',return_value=Mock()), patch.object(service,'_seed_extraction_usage'), patch('src.core.extraction.acquire_source',side_effect=acquire):
            self.control(service,plan,'start',network_consent=True,cloud_upload_consent=True)
            self.assertTrue(entered.wait(2));item=service.show(plan['batch_id'])['items'][0]
            self.assertEqual(item['activity']['stage'],'text');self.assertIsNone(item['activity']['fraction'])
            release.set();until(lambda:not service.workers)

    def test_failed_text_is_reported_while_other_output_is_still_processing(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def cover(*args,**kwargs):entered.set();release.wait(4)
        service=BatchService(None)
        plan=service.stage_extraction('partial-progress',[dict(source='https://example.com/partial',title='部分产物',outputs=['text','cover'],media_directory=str(self.root))])
        with patch.object(pipeline,'transcribe_source',side_effect=ValueError('隔离文案失败')), patch.object(extraction,'metadata',return_value={'cover_platform':'douyin','cover_work_id':'1234567890','cover_url':'https://example.com/c.jpg','cover_status':'url_only'}), patch('src.core.collect.cover.CoverLocalizationService.localize',side_effect=cover):
            try:
                self.control(service,plan,'start',network_consent=True);self.assertTrue(entered.wait(2))
                current=service.show(plan['batch_id'])['items'][0]
                self.assertEqual(current['state'],'running');self.assertEqual(current['assets']['text']['state'],'failed')
                self.assertEqual(current['activity']['stage'],'cover')
            finally:
                release.set();until(lambda:not service.workers)


class ExtractionProgressUITests(ExtractionBatchUITests):
    def present(self,state='running',step='transcription',fraction=.5):
        payload=dict(stage='text' if step in {'transcription','subtitles'} else 'save',step=step,
            steps=['prepare','text','save'],fraction=fraction,started_at=time.time()-37,attempt_id='isolated-attempt')
        self.js(f"(()=>{{let i=workspaceBatches[0].items[0];i.state={json.dumps(state)};i.message='隔离过程消息';i.activity={json.dumps(payload)};renderBoard()}})()")

    def test_running_progress_uses_existing_card_footprint_and_restores_finished_style(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        self.js("finishCardEntrance(document.querySelector('.nx-single'))")
        for width in (1463,900,540):
            self.widget.resize(width,925);self.tick()
            self.present('draft');self.tick()
            before=self.js("document.querySelector('.nx-single').getBoundingClientRect().height")
            self.present();self.tick()
            self.assertTrue(self.js("document.querySelector('.nx-live-panel').textContent.includes('本地识别')"))
            self.assertTrue(self.js("document.querySelector('.nx-live-panel [role=progressbar]').getAttribute('aria-valuenow')==='50'"))
            self.assertAlmostEqual(before,self.js("document.querySelector('.nx-single').getBoundingClientRect().height"),delta=1)
            self.present('succeeded','saving',None);self.tick()
            self.assertEqual(self.js("document.querySelector('.nx-settings-panel').getAttribute('aria-hidden')"),'false')
            self.assertAlmostEqual(before,self.js("document.querySelector('.nx-single').getBoundingClientRect().height"),delta=1)

    def test_progress_refresh_retains_nodes_and_unknown_removes_old_percentage(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle();self.present()
        self.js("window.progressNodes=[document.querySelector('.nx-single'),document.querySelector('.nx-live-panel'),document.querySelector('.nx-single-footer > button')]")
        for fraction in (.6,.7,None):self.present(fraction=fraction)
        self.assertTrue(self.js("window.progressNodes.every((n,j)=>n===[document.querySelector('.nx-single'),document.querySelector('.nx-live-panel'),document.querySelector('.nx-single-footer > button')][j])"))
        self.assertFalse(self.js("document.querySelector('.nx-live-panel [role=progressbar]').hasAttribute('aria-valuenow')"))
        self.assertFalse(self.js("document.querySelector('.nx-live-value').textContent.includes('%')"))

    def test_phase_motion_is_bounded_and_cancellation_restores_latest_controls(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle();self.present()
        self.present(step='saving',fraction=None)
        self.assertTrue(self.js("document.querySelector('.nx-live-stage').textContent.includes('保存')"))
        self.present('cancelled','saving',None)
        for _ in range(18):self.tick()
        self.assertEqual(self.js("document.querySelector('.nx-settings-panel').getAttribute('aria-hidden')"),'false')
        self.assertTrue(self.js("document.querySelector('.nx-live-panel').inert"))
        self.assertFalse(self.js("document.querySelector('.nx-live-stage').getAnimations().some(a=>a.playState==='running')"))
        self.assertEqual(self.js("document.querySelector('.nx-single-footer > button').textContent.trim()"),'开始提取')

    def test_reduced_motion_avoids_flip_and_subtitle_route_has_truthful_label(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        self.js("Object.defineProperty(motionPreference,'matches',{value:true,configurable:true})")
        self.present(step='subtitles',fraction=None)
        self.assertTrue(self.js("document.querySelector('.nx-live-stage').textContent.includes('字幕')"))
        self.assertFalse(self.js("document.querySelector('.nx-live-stage').getAnimations().some(a=>a.effect.getKeyframes().some(k=>k.transform&&k.transform!=='none'))"))

    def test_elapsed_clock_advances_without_new_backend_messages(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle();self.present(fraction=None)
        before=self.js("document.querySelector('.nx-live-elapsed').textContent")
        self.until(lambda:self.js("document.querySelector('.nx-live-elapsed').textContent")!=before,seconds=2)
        self.assertEqual(self.run_calls,[])

    def test_partial_failure_is_not_drawn_as_a_completed_phase(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle();self.present(step='saving',fraction=None)
        self.js("workspaceBatches[0].items[0].assets.text.state='failed';renderBoard()")
        self.assertTrue(self.js("document.querySelector('[data-progress-phase=text]').classList.contains('is-failed')"))
        self.assertEqual(self.js("document.querySelector('[data-progress-phase=text] > i').textContent"),'!')


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(cls(name) for cls in (ExtractionProgressTests,ExtractionProgressUITests)
        for name in cls.__dict__ if name.startswith('test_'))
