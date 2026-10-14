"""Isolated extraction contracts: no network, models, Keychain or user data."""
import base64
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.core.link_extraction_jobs import LinkExtractionJobs
from src.core.link_assets import acquire, work_data
from src.core.job_ledger.contracts import JobState
from tests import test_cli_all_local_asr as asr_fixture


class LinkExtractionTests(unittest.TestCase):
    def setUp(self):
        asr_fixture.AllLocalAsrTests.setUp(self)
        self.destination = self.root / 'downloads'
        self.destination.mkdir(mode=0o700)
        self.calls = []
        self.service = LinkExtractionJobs(application=self.application, job_store=self.store,
            result_store=self.results, now=lambda:'2026-09-26T00:00:00Z',
            id_factory=lambda kind:kind + '-' + uuid4().hex, runner=self.acquire)
        self.runtime._link_extraction_service = self.service
        self.url = 'https://www.youtube.com/watch?v=fixture'

    pipeline = asr_fixture.AllLocalAsrTests.pipeline
    cli = asr_fixture.AllLocalAsrTests.cli

    def acquire(self, source, chosen, root, progress, cancelled, now):
        self.calls.append(tuple(chosen))
        files = {}
        for kind in chosen:
            progress(kind)
            path = root / (kind + dict(video='.mp4', audio='.m4a', cover='.jpg', data='.json')[kind])
            path.write_bytes(json.dumps({'title':'作品标题'}).encode() if kind == 'data' else b'fixture bytes')
            files[kind] = path
        return files, {}

    def prepare(self, outputs='video,audio,cover,data'):
        code, result = self.cli('extract', 'prepare', self.url, '--outputs', outputs, '--destination', str(self.destination))
        self.assertEqual(code, 0, result)
        return result['result']

    def run_plan(self, plan, request=None):
        code, result = self.cli('extract', 'run', plan['source_url'], '--outputs', ','.join(plan['outputs']),
            '--destination', plan['destination'], '--plan-digest', plan['plan_digest'], '--request-id', request or uuid4().hex)
        self.assertEqual(code, 0, result)
        jid = result['result']['job_id']
        self.service.wait_for_worker(jid, 5)
        return jid

    def test_each_output_and_combination_without_model_probe(self):
        for outputs in ('video', 'audio', 'cover', 'data', 'video,audio,cover,data'):
            plan = self.prepare(outputs)
            jid = self.run_plan(plan)
            self.assertEqual(self.store.get_job(jid).state, JobState.SUCCEEDED)
            code, result = self.cli('jobs', 'receipt', '--job-id', jid)
            self.assertEqual(code, 0, result)
            items = result['result']['extraction']['items']
            self.assertEqual(set(items), set(outputs.split(',')))
            for row in items.values():
                self.assertTrue(Path(row['path']).is_file())
                self.assertEqual(len(row['sha256']), 64)
        self.assertEqual(self.probes, [])

    def test_prepare_no_work_and_discovery_reports_real_service(self):
        plan = self.prepare('cover,data')
        self.assertFalse(plan['effects']['runs_model'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.list_jobs(), ())
        code, result = self.cli('capabilities')
        self.assertEqual(code, 0, result)
        # The capability response itself is a control payload.
        rows = result.get('result', result)['capabilities']
        self.assertEqual(sum(row['state'] == 'available' for row in rows if row['capability_id'].startswith('EXTRACT-')), 4)

    def test_changed_plan_duplicate_unknown_and_local_url_do_not_execute(self):
        plan = self.prepare()
        code, result = self.cli('extract', 'run', self.url, '--outputs', 'audio', '--destination', str(self.destination),
            '--plan-digest', plan['plan_digest'], '--request-id', uuid4().hex)
        self.assertNotEqual(code, 0)
        for url, outputs in [(self.url, 'cover,cover'), (self.url, 'text'), ('http://127.0.0.1/private', 'data')]:
            code, _ = self.cli('extract','prepare', url,'--outputs',outputs,'--destination',str(self.destination))
            self.assertNotEqual(code, 0)
        code, _ = self.cli('extract','prepare',self.url,'--outputs','data','--destination',str(self.destination),'--engine','anything')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_same_request_reuses_job_and_changed_request_is_rejected(self):
        plan, request = self.prepare('data'), uuid4().hex
        first = self.run_plan(plan, request)
        self.assertEqual(self.run_plan(plan, request), first)
        other = self.prepare('video')
        with self.assertRaisesRegex(Exception, 'CONFLICT'):
            self.service.run(self.url, 'video', str(self.destination), other['plan_digest'], request)
        self.assertEqual(self.calls, [('data',)])

    def test_partial_and_all_failed_have_per_item_results(self):
        original = self.acquire
        def partial(source, chosen, *args):
            files, _ = original(source, ['audio'] if 'audio' in chosen else [], *args)
            return files, {'cover':dict(code='EXTRACT_COVER_FAILED', message='封面获取失败')}
        self.service.runner = partial
        for outputs, state in [('audio,cover', JobState.PARTIAL), ('cover', JobState.FAILED)]:
            jid = self.run_plan(self.prepare(outputs))
            self.assertEqual(self.store.get_job(jid).state, state)
            code, result = self.cli('jobs','receipt','--job-id',jid)
            self.assertEqual(code, 0, result)
            self.assertEqual(result['result']['extraction']['items']['cover']['state'],'failed')
            self.assertTrue(result['result']['partial_failures'])

    def test_cancellation_stops_publication_and_same_request_does_not_replay(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args):
            entered.set()
            release.wait(3)
            return self.acquire(*args)
        self.service.runner = blocked
        plan, request = self.prepare('video'), uuid4().hex
        result = self.service.run(self.url, 'video', str(self.destination), plan['plan_digest'], request)
        self.assertTrue(entered.wait(2))
        self.cli('jobs','cancel','--job-id', result['job_id'])
        release.set()
        self.service.wait_for_worker(result['job_id'],5)
        self.assertEqual(self.store.get_job(result['job_id']).state, JobState.CANCELLED)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.run_plan(plan,request), result['job_id'])

    def test_full_data_result_is_bounded_and_tamper_detected(self):
        jid = self.run_plan(self.prepare('data'))
        code, result = self.cli('jobs','result','--job-id',jid,'--offset','0')
        self.assertEqual(code, 0, result)
        raw = base64.b64decode(result['result']['data'])
        self.assertEqual(json.loads(raw)['extraction']['work_data']['title'],'作品标题')
        for offset in ('-1', str(len(raw))):
            code, _ = self.cli('jobs','result','--job-id',jid,'--offset',offset)
            self.assertNotEqual(code,0)
        (self.results.directory / (jid + '.json')).write_text('{}')
        code, _ = self.cli('jobs','result','--job-id',jid,'--offset','0')
        self.assertNotEqual(code,0)

    def test_cancel_during_publication_keeps_completed_file_receipt(self):
        from src.core.link_extraction_jobs import publish
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args, **kwargs):
            if args[5].endswith('-audio'):
                entered.set()
                release.wait(3)
            return publish(*args, **kwargs)
        plan = self.prepare('video,audio')
        with patch('src.core.link_extraction_jobs.publish',side_effect=blocked):
            result = self.service.run(self.url,'video,audio',str(self.destination),plan['plan_digest'],uuid4().hex)
            self.assertTrue(entered.wait(2))
            self.cli('jobs','cancel','--job-id',result['job_id'])
            release.set()
            self.service.wait_for_worker(result['job_id'],5)
        code, receipt = self.cli('jobs','receipt','--job-id',result['job_id'])
        self.assertEqual(code,0,receipt)
        self.assertEqual(receipt['result']['state'],'cancelled')
        items = receipt['result']['extraction']['items']
        self.assertEqual(items['video']['state'],'succeeded')
        self.assertEqual(items['audio']['state'],'cancelled')
        self.assertEqual(len(list(self.destination.iterdir())),1)

    def test_uncertain_publication_preserves_path_and_stops_remaining_outputs(self):
        from src.core.link_extraction_jobs import _check_destination
        calls = []
        def changed(*args):
            calls.append(1)
            if len(calls) == 3:
                raise OSError('simulated directory moved after publication')
            return _check_destination(*args)
        with patch('src.core.link_extraction_jobs._check_destination',side_effect=changed):
            jid = self.run_plan(self.prepare('video,audio'))
        code, receipt = self.cli('jobs','receipt','--job-id',jid)
        self.assertEqual(code,0,receipt)
        self.assertEqual(receipt['result']['state'],'recovery_required')
        items = receipt['result']['extraction']['items']
        self.assertEqual(items['video']['state'],'unknown')
        self.assertTrue(Path(items['video']['path']).is_file())
        self.assertEqual(items['audio']['state'],'not_completed')

    def test_destination_symlink_rejected_and_replacement_not_written(self):
        link = self.root / 'shortcut'
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(Exception):
            self.service.prepare(self.url, 'data', str(link))
        original = self.acquire
        def replaced(*args):
            self.destination.rename(self.root / 'moved')
            self.destination.mkdir()
            return original(*args)
        self.service.runner = replaced
        jid = self.run_plan(self.prepare('video'))
        self.assertEqual(self.store.get_job(jid).state, JobState.FAILED)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list((self.root / 'moved').iterdir()), [])

    def test_no_overwrite_and_recovery_on_receipt_failure(self):
        original = self.acquire
        def conflict(*args):
            jid = next(job.job_id for job in self.control_jobs() if job.state is JobState.RUNNING)
            (self.destination / (jid + '-video.mp4')).write_bytes(b'KEEP')
            return original(*args)
        self.service.runner = conflict
        jid = self.run_plan(self.prepare('video'))
        self.assertEqual(self.store.get_job(jid).state,JobState.FAILED)
        self.assertEqual((self.destination / (jid + '-video.mp4')).read_bytes(), b'KEEP')
        self.service.runner = original
        with patch.object(self.results,'write_extraction',side_effect=OSError('fixture')):
            jid = self.run_plan(self.prepare('audio'))
        self.assertEqual(self.store.get_job(jid).state, JobState.RECOVERY_REQUIRED)

    def control_jobs(self):
        return self.service.control.list()

    def test_owner_loss_fences_worker_and_history_keeps_result(self):
        from src.core.agent_entry.task_handover import capture_terminal_tasks
        jid = self.run_plan(self.prepare('data'))
        def invoke(args):
            # capture_terminal_tasks supplies its own output flag.
            code, result = self.cli(*args[:-2])
            self.assertEqual(code,0,result)
            return result['result']
        history = capture_terminal_tasks(invoke)
        self.assertEqual(history[0]['job_kind'], 'link_extraction')
        self.runtime._history = {jid:history[0]}
        code, result = self.cli('jobs','receipt','--job-id',jid)
        self.assertEqual(code,0,result)
        self.assertIn('data',result['result']['extraction']['items'])
        code, result = self.cli('jobs','result','--job-id',jid,'--offset','0')
        self.assertEqual(code,0,result)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args):
            entered.set()
            release.wait(3)
            return self.acquire(*args)
        self.service.runner = blocked
        plan, request = self.prepare('video'), uuid4().hex
        result = self.service.run(self.url,'video',str(self.destination),plan['plan_digest'],request)
        self.assertTrue(entered.wait(2))
        self.store.recover_orphaned_running(changed_at='2026-09-26T00:01:00Z')
        release.set()
        self.service.wait_for_worker(result['job_id'],5)
        self.assertEqual(self.store.get_job(result['job_id']).state,JobState.RECOVERY_REQUIRED)
        self.assertFalse(any(self.destination.glob('*video*')))
        self.assertEqual(self.run_plan(plan,request),result['job_id'])

    def test_unverified_runtime_cannot_prepare_or_start_network(self):
        self.application._runtime_verification = {}
        code, result = self.cli('extract','prepare',self.url,'--outputs','data','--destination',str(self.destination))
        self.assertNotEqual(code,0,result)
        self.assertEqual(self.calls,[])


class LinkAssetAdapterTests(unittest.TestCase):
    def test_data_preserves_zero_and_marks_missing(self):
        data = work_data({'title':'测试', 'stats':{'like':0}, 'author':{'name':'作者'}}, 'https://example.com/work', 'now')
        self.assertEqual(data['stats']['like'],0)
        self.assertIsNone(data['stats']['view'])
        self.assertIn('stats.view', data['missing_fields'])
        self.assertEqual(data['source_url'],'https://example.com/work')

    def test_data_only_metadata_does_not_download_media(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory, patch('src.core.extraction.metadata', return_value={'title':'标题'}), \
                patch('src.core.extraction.download_video') as video, patch('src.core.audio.download_process.download_in_process') as audio:
            files, errors = acquire('https://example.com/work',['data'],Path(directory),lambda *_:None,lambda:False,'now')
            self.assertEqual(json.loads(files['data'].read_text())['title'],'标题')
            self.assertEqual(errors,{})
            video.assert_not_called()
            audio.assert_not_called()

    def test_audio_reuses_video_and_has_audio_extension(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / 'source.mp4'
            video.write_bytes(b'fixture')
            def audio(source, target, cancelled):
                self.assertEqual(source,video)
                target.write_bytes(b'audio')
                return target
            with patch('src.core.extraction.download_video',return_value=video), \
                    patch('src.core.link_assets.separate_audio',side_effect=audio), \
                    patch('src.core.audio.download_process.download_in_process') as download:
                files, errors = acquire('https://example.com/work',['video','audio'],root,lambda *_:None,lambda:False,'now')
                self.assertEqual(errors,{})
                self.assertEqual(files['audio'].suffix,'.m4a')
                download.assert_not_called()

    def test_cover_only_uses_verified_cover_receipt_and_failure_is_explicit(self):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from src.core.models import TranscriptCover
        from src.core.collect.cover import CoverReceipt
        import hashlib
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'covers/douyin/test.jpg'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture')
            meta = TranscriptCover(platform='douyin',work_id='123',url='https://p3-sign.douyinpic.com/test.jpg',status='url_only').merge_meta({})
            receipt = CoverReceipt(status='downloaded',relative_path='douyin/test.jpg',size=7,
                sha256=hashlib.sha256(b'fixture').hexdigest(),mime_type='image/jpeg')
            service = SimpleNamespace(root=root / 'covers',localize=lambda **_:receipt)
            with patch('src.core.extraction.metadata',return_value=meta), \
                    patch('src.core.collect.cover.CoverLocalizationService',return_value=service), \
                    patch('src.core.extraction.download_video') as video:
                files, errors = acquire('https://www.douyin.com/video/123',['cover'],root,lambda *_:None,lambda:False,'now')
                self.assertEqual(files,{'cover':path})
                self.assertEqual(errors,{})
                video.assert_not_called()
                service.localize = lambda **_:CoverReceipt.failed('cover_download_failed')
                files, errors = acquire('https://www.douyin.com/video/123',['cover'],root,lambda *_:None,lambda:False,'now')
                self.assertEqual(files,{})
                self.assertEqual(errors['cover']['code'],'EXTRACT_COVER_FAILED')

    def test_real_ffmpeg_audio_is_decodable_stereo_without_video(self):
        from tempfile import TemporaryDirectory
        import subprocess
        import wave
        from src.core.audio.converter import _ffmpeg_exe
        from src.core.link_assets import separate_audio
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.wav'
            with wave.open(str(source),'wb') as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(44100)
                writer.writeframes(b'\0' * (44100 * 4 // 5))
            video = root / 'source.mp4'
            created = subprocess.run([_ffmpeg_exe(),'-v','error','-f','lavfi','-i','color=c=black:s=16x16:r=5',
                '-i',str(source),'-t','0.2','-c:v','mpeg4','-c:a','aac',str(video)],capture_output=True,timeout=10)
            self.assertEqual(created.returncode,0,created.stderr)
            target = separate_audio(video,root / 'audio.m4a',lambda:False)
            video_check = subprocess.run([_ffmpeg_exe(),'-v','error','-i',str(target),'-map','0:v:0','-f','null','-'],capture_output=True,timeout=10)
            self.assertNotEqual(video_check.returncode,0)
            self.assertIn(b'matches no streams',video_check.stderr)
            decoded = root / 'decoded.wav'
            process = subprocess.run([_ffmpeg_exe(),'-v','error','-i',str(target),'-c:a','pcm_s16le',str(decoded)],capture_output=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            with wave.open(str(decoded),'rb') as reader:
                self.assertEqual(reader.getnchannels(),2)
                self.assertEqual(reader.getframerate(),44100)
                self.assertGreater(reader.getnframes(),0)
