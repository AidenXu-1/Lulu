"""Formal CLI adapter with isolated library/config, injected network and engines."""
from io import StringIO
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.agent_cli.main import main
from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core import library
from src.core.agent_workspace.batches import BatchService
from src.core.agent_workspace.control import ControlService, SETTINGS
from src.core.agent_workspace.documents import digest
from src.core.agent_workspace.store import encode
from src.core.models import Transcript, Segment
from src.core.podcast import Feed, Episode
from src.core.transcription_job_application import LocalTranscriptionJobApplication, LocalTranscriptionJobError
from tests.test_agent_workspace import AgentWorkspaceTests
from tests.test_live_recording import until


class ExpansionTests(unittest.TestCase):
    setUp = AgentWorkspaceTests.setUp

    def runtime(self, runner=None):
        application = Mock(spec=LocalTranscriptionJobApplication)
        runtime = LocalTranscriptionCliRuntime(application, job_control=SimpleNamespace(list=lambda: []))
        if runner:
            service = BatchService(runtime, runner=runner)
            runtime.expansion_batches = lambda: service
        return runtime

    def invoke(self, name, arguments, runtime=None):
        path = self.root/'request.json'
        path.write_text(json.dumps(arguments, ensure_ascii=False))
        out, err = StringIO(), StringIO()
        code = main(['workspace', *name.split('.'), '--request', str(path), '--output', 'json'],
                    local_transcription_runtime=runtime or self.runtime(), stdout=out, stderr=err)
        self.assertEqual(err.getvalue(), '')
        return code, json.loads(out.getvalue())

    def call(self, name, arguments, runtime=None):
        code, result = self.invoke(name, arguments, runtime)
        self.assertEqual(code, 0, result)
        return result['result']

    def test_folder_move_soft_delete_restore_and_discovery(self):
        folder = self.call('folders.create', dict(name='待整理'))['id']
        self.call('folders.rename', dict(folder_id=folder, name='学习'))
        self.call('folders.order', dict(ordered_ids=[folder]))
        moved = self.call('documents.move', dict(document_ids=[self.eid], folder_id=folder))
        self.assertTrue(moved[0]['ok'])
        self.call('documents.delete', dict(document_id=self.eid))
        trash = self.call('trash.list', {})
        token = trash[0]['token']
        self.call('trash.restore', dict(token=token))
        self.assertIn('第一段', library.load(self.eid).text)
        tools = self.call('tools.catalog', {})
        self.assertTrue({'documents.export','rss.fetch','batches.start','models.action','live.control'} <= {c['command'] for c in tools['commands']})

    def test_complete_exports_replay_and_conflict_no_overwrite(self):
        target = self.root/'全文.md'
        args = dict(document_id=self.eid, format='md', path=str(target), request_id='export1')
        result = self.call('documents.export', args)
        self.assertEqual(target.read_text(), library.load_markdown(self.eid))
        self.assertEqual(result['sha256'], digest(target.read_text()))
        self.assertTrue(self.call('documents.export', args)['reused'])
        target.write_text('另一个应用的新内容')
        code, _ = self.invoke('documents.export', args)
        self.assertNotEqual(code, 0)
        self.assertEqual(target.read_text(), '另一个应用的新内容')
        code, _ = self.invoke('documents.export', dict(args, format='srt', path=str(self.root/'no.srt'), request_id='srt1'))
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root/'no.srt').exists())

    def test_real_subtitle_export_uses_existing_timestamps(self):
        eid = library.save(Transcript('一句完整口述。', [Segment('一句完整口述。', .2, 1.3)], 'zh', 'test'), '字幕')
        target = self.root/'speech.srt'
        self.call('documents.export', dict(document_id=eid, format='srt', path=str(target), request_id='subtitle'))
        self.assertIn('00:00:00,200 --> 00:00:01,300', target.read_text())

    def test_obsidian_complete_source_and_dedup_across_request_ids(self):
        result = self.call('documents.obsidian', dict(document_id=self.eid, request_id='obs1', folder=str(self.root)))
        text = Path(result['path']).read_text()
        self.assertIn(library.load_markdown(self.eid), text)
        second = self.call('documents.obsidian', dict(document_id=self.eid, request_id='obs2', folder=str(self.root)))
        self.assertEqual(result['path'], second['path'])
        self.assertTrue(second['reused'])

    def test_batch_sequential_unique_results_replay_and_reopen(self):
        calls, active, peak = [], [0], [0]
        def runner(item, batch, progress, cancel):
            calls.append(item['source'])
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(.02)
            progress('转录中', .5)
            active[0] -= 1
            return Transcript('完整结果 ' + item['source'], [], 'zh', batch['engine'], source=item['source'])
        runtime = self.runtime(runner)
        items = [dict(source='https://example.com/a.mp3'), dict(source='https://example.com/b.mp3'), dict(source='https://example.com/a.mp3')]
        plan = self.call('batches.prepare', dict(request_id='set1', items=items), runtime)
        code, _ = self.invoke('batches.start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash']), runtime)
        self.assertNotEqual(code, 0)
        self.call('batches.start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'], network_consent=True), runtime)
        service = runtime.expansion_batches()
        until(lambda: not service.workers)
        result = self.call('batches.show', dict(batch_id=plan['batch_id']), runtime)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(result['counts']['succeeded'], 2)
        self.assertEqual(result['counts']['duplicate'], 1)
        self.assertEqual(peak[0], 1)
        self.call('batches.start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'], network_consent=True), runtime)
        self.assertEqual(len(calls), 2)
        reopened = BatchService(runtime, runner=runner)
        self.assertEqual(len(reopened.list()), 1)
        self.assertEqual(reopened.show(plan['batch_id'])['state'], 'succeeded')

    def test_cancel_preserves_completed_and_resume_does_not_repeat(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def runner(item, batch, progress, cancel):
            calls.append(item['source'])
            if item['index'] == 1:
                entered.set()
                release.wait(3)
            return Transcript('已生成 ' + str(item['index']), [], 'zh', batch['engine'])
        runtime = self.runtime(runner)
        service = runtime.expansion_batches()
        plan = service.prepare('cancel', [dict(source='https://example.com/1.mp3'), dict(source='https://example.com/2.mp3')], 'local_sensevoice_q8')
        service.start(plan['batch_id'], plan['plan_hash'], True)
        self.assertTrue(entered.wait(2))
        service.cancel(plan['batch_id'])
        release.set()
        until(lambda: not service.workers)
        self.assertEqual(service.show(plan['batch_id'])['counts']['succeeded'], 1)
        service.start(plan['batch_id'], plan['plan_hash'], True, resume=True)
        until(lambda: not service.workers)
        self.assertEqual(service.show(plan['batch_id'])['state'], 'succeeded')
        self.assertEqual(len(calls), 2)  # Saved second inference result is published, not recomputed.

    def test_saved_rss_snapshot_explicit_selection_and_no_refetch(self):
        runtime = self.runtime()
        with patch('src.core.podcast.fetch_feed', return_value=Feed('节目', [Episode('第一集', 'https://example.com/ep1.mp3'), Episode('第二集', 'https://example.com/ep2.mp3')])) as fetch:
            feed = self.call('rss.fetch', dict(source='https://example.com/rss.xml'), runtime)
            self.assertEqual(self.call('rss.list', {}, runtime)['feeds'][0]['title'], '节目')
            plan = self.call('rss.select', dict(snapshot_id=feed['snapshot_id'], episode_ids=[feed['episodes'][1]['episode_id']], request_id='rss-choice'), runtime)
            self.assertEqual(plan['items'][0]['source'], 'https://example.com/ep2.mp3')
            self.assertEqual(fetch.call_count, 1)

    def test_unavailable_feed_has_actionable_error_and_keeps_saved_feeds(self):
        runtime = self.runtime()
        with patch('src.core.podcast.fetch_feed', return_value=Feed('节目', [Episode('第一集', 'https://example.com/ep1.mp3')])):
            before = self.call('rss.fetch', dict(source='https://example.com/rss.xml'), runtime)
        with patch('src.core.podcast.fetch_feed', return_value=None):
            code, result = self.invoke('rss.fetch', dict(source='https://example.com/unavailable.xml'), runtime)
        self.assertEqual(code, 41)
        self.assertEqual(result['error']['details']['reason_code'], 'RSS_FEED_UNAVAILABLE')
        self.assertIn('订阅链接', result['error']['message'])
        self.assertNotIn('JSON', result['error']['human_action'])
        self.assertEqual(self.call('rss.list', dict(feed_id=before['feed_id']), runtime), before)

    def test_control_real_gui_claim_result_replay_and_no_cli_heartbeat(self):
        service = ControlService()
        with self.assertRaises(LocalTranscriptionJobError):
            service.submit('first', 'updates', 'check', {})
        service.poll('real-window', [], {})
        request = service.submit('first', 'updates', 'check', {})
        self.assertEqual(request['state'], 'pending')
        actions = service.poll('real-window', [], {})['actions']
        self.assertEqual(len(actions), 1)
        self.assertEqual(service.poll('real-window', [], {})['actions'], [])
        service.poll('real-window', [dict(request_id='first', state='succeeded', result={'current_version':'test'})], {})
        self.assertEqual(service.submit('first', 'updates', 'check', {})['state'], 'succeeded')
        with self.assertRaises(library.RevisionConflict):
            service.submit('first', 'updates', 'install', {})
        code, _ = self.invoke('control_poll.invalid', {})
        self.assertNotEqual(code, 0)

    def test_settings_secret_fields_rejected(self):
        for changes in ({'api_key':'NEVER_SAVE'}, {'cookie':'NEVER_SAVE'}, {'engine_name':'groq'}):
            code, _ = self.invoke('settings.apply', dict(request_id='settings1', changes=changes, expected_hash='none'))
            self.assertNotEqual(code, 0)

    def test_voice_import_rename_reference_update_replay_and_delete(self):
        import wave
        from src.core.synthesis import voices
        source = self.root/'voice.wav'
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 16000)
        with patch.object(voices, 'VOICES_DIR', self.root/'voices'), patch.object(voices, 'INDEX', self.root/'voices/voices.json'), \
                patch('src.config.AppConfig.load', return_value=SimpleNamespace(voice_library_dir=str(self.root/'voices'))):
            args = dict(request_id='voice1', name='参考音色', source_path=str(source), reference_text='原文字')
            result = self.call('voices.create', args)
            self.assertNotEqual(result['reference_audio_path'], str(source))
            self.assertEqual(self.call('voices.create', args)['id'], result['id'])
            updated = self.call('voices.update', dict(voice_id=result['id'], name='新名称', reference_text='修正参考文字'))
            self.assertEqual(updated['reference_text'], '修正参考文字')
            self.assertEqual(len(self.call('voices.list', {})['voices']), 1)
            code, _ = self.invoke('voices.delete', dict(voice_id=result['id']))
            self.assertNotEqual(code, 0)
            self.call('voices.delete', dict(voice_id=result['id'], confirmed=True))
            self.assertEqual(self.call('voices.list', {})['voices'], [])
            self.assertTrue(source.exists())

    def test_partial_voice_delete_is_not_success_and_retry_finishes(self):
        from src.core.synthesis import voices
        root=self.root/'voices'
        sample=self.root/'reference.wav';sample.write_bytes(b'isolated reference')
        with patch.object(voices,'VOICES_DIR',root),patch.object(voices,'INDEX',root/'voices.json'), \
                patch('src.config.AppConfig.load',return_value=SimpleNamespace(voice_library_dir=str(root))):
            voice=voices.add_voice('test',str(sample));before=voices.INDEX.read_bytes()
            folder=root/voice.id
            def fail(path, **kwargs):
                Path(voice.reference_audio_path).unlink()
                if not kwargs.get('ignore_errors'):raise PermissionError('injected partial removal')
            with patch.object(voices.shutil,'rmtree',side_effect=fail):
                code,envelope=self.invoke('voices.delete',dict(voice_id=voice.id,confirmed=True))
            self.assertNotEqual(code,0)
            self.assertFalse(envelope.get('ok'))
            self.assertEqual(envelope['error']['details']['reason_code'],'VOICE_DELETE_INCOMPLETE')
            self.assertIn('部分参考文件可能已删除',envelope['error']['message'])
            self.assertNotIn('原资料保留',envelope['error']['message'])
            self.assertIn('重试删除',envelope['error']['human_action'])
            self.assertEqual(voices.INDEX.read_bytes(),before)
            self.assertTrue(folder.exists())
            self.assertTrue(self.call('voices.delete',dict(voice_id=voice.id,confirmed=True))['deleted'])
            self.assertFalse(folder.exists())
            self.assertTrue(sample.exists())

    def test_voice_delete_cleans_residue_even_when_old_index_entry_is_absent(self):
        from src.core.synthesis import voices
        root=self.root/'voices';root.mkdir()
        (root/'voices.json').write_text('[]')
        folder=root/('a'*12);folder.mkdir();(folder/'remaining.txt').write_text('residue')
        with patch.object(voices,'VOICES_DIR',root),patch.object(voices,'INDEX',root/'voices.json'), \
                patch('src.config.AppConfig.load',return_value=SimpleNamespace(voice_library_dir=str(root))):
            result=self.call('voices.delete',dict(voice_id='a'*12,confirmed=True))
            self.assertTrue(result['deleted'])
            self.assertFalse(folder.exists())

    def test_unreadable_voice_index_reports_recovery_and_preserves_bytes(self):
        from src.core.synthesis import voices
        root=self.root/'voices';root.mkdir();index=root/'voices.json';index.write_bytes(b'{broken')
        with patch.object(voices,'VOICES_DIR',root),patch.object(voices,'INDEX',index), \
                patch('src.config.AppConfig.load',return_value=SimpleNamespace(voice_library_dir=str(root))):
            code,envelope=self.invoke('voices.list',{})
            self.assertEqual(code,21)
            self.assertEqual(envelope['error']['details']['reason_code'],'VOICE_LIBRARY_UNREADABLE')
            self.assertIn('原资料保留',envelope['error']['message'])
            self.assertIn('不要重复添加或删除',envelope['error']['human_action'])
            self.assertEqual(index.read_bytes(),b'{broken')

    def test_collection_actual_service_injected_history_selection_and_exports(self):
        from src.core.collect.application import CollectionApplicationService
        from src.core.collect.contracts import CollectResult
        from src.core.collect.models import CollectedItem
        from src.core import models_manager
        items = [CollectedItem('123', title='口述视频', work_url='https://www.douyin.com/video/123'),
                 CollectedItem('456', title='图文', aweme_type=68, content_kind='image')]
        collector = CollectionApplicationService()
        collector.collect = lambda *args, **kwargs: CollectResult.complete(items)
        runtime = self.runtime()
        batch_service = BatchService(runtime, collector=collector)
        runtime.expansion_batches = lambda: batch_service
        with patch.object(models_manager, 'app_data_dir', return_value=self.root/'app-data'):
            plan = self.call('collect.prepare', dict(request_id='collection1', source='https://www.douyin.com/user/MS4w-test'), runtime)
            self.call('batches.start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'], network_consent=True), runtime)
            until(lambda: not batch_service.workers)
            result = batch_service.show(plan['batch_id'])
            self.assertEqual(result['state'], 'succeeded', result)
            record_id = result['record_id']
            history = self.call('collect.history', dict(record_id=record_id), runtime)
            self.assertEqual(len(history['items']), 2)
            selected = self.call('collect.select', dict(record_id=record_id,
                item_keys=[i.stable_key for i in items], request_id='selected-videos'), runtime)
            self.assertEqual(len(selected['items']), 1)
            self.assertEqual(len(selected['skipped']), 1)
            for fmt in ('csv', 'xlsx'):
                output = self.root / ('table.' + fmt)
                self.call('collect.export', dict(record_id=record_id, format=fmt, path=str(output), request_id='table-' + fmt), runtime)
                self.assertGreater(output.stat().st_size, 100)

    def test_changed_frozen_source_cannot_be_restored_or_started(self):
        runtime = self.runtime()
        service = runtime.expansion_batches()
        plan = service.prepare('frozen', [dict(source='https://example.com/speech.mp3')], 'local_sensevoice_q8')
        path = service._path(plan['batch_id'])
        raw = json.loads(path.read_text())
        raw['items'][0]['source'] = 'https://example.com/other.mp3'
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, '成员已变化'):
            service.start(plan['batch_id'], plan['plan_hash'], True)
        self.assertFalse(service.workers)

    def test_batch_download_uses_existing_extraction_jobs_and_keeps_file_receipt(self):
        from tests.test_cli_link_extraction import LinkExtractionTests
        fixture = LinkExtractionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runtime = fixture.runtime
        service = BatchService(runtime)
        runtime.expansion_batches = lambda: service
        plan = self.call('batches.download', dict(request_id='downloads', destination=str(fixture.destination),
            items=[dict(source=fixture.url)]), runtime)
        self.call('batches.start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'], network_consent=True), runtime)
        until(lambda: not service.workers)
        result = service.show(plan['batch_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        receipt = result['items'][0]['files']['video']
        self.assertTrue(Path(receipt['path']).is_file())
        self.assertEqual(fixture.probes, [])
        self.assertEqual(len(fixture.calls), 1)
        service.start(plan['batch_id'], plan['plan_hash'], True, resume=True)
        self.assertEqual(len(fixture.calls), 1)



    def test_collected_download_transcribe_reuses_managed_video_and_saves_status(self):
        import subprocess
        from src.core import models_manager
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        from src.core.live_recording.media import bundled_ffmpeg
        item = CollectedItem('12345', title='已采集视频', work_url='https://www.douyin.com/video/12345')
        sample = self.root/'sample.mp4'
        subprocess.run([bundled_ffmpeg(), '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '1', '-c:v', 'libx264', '-c:a', 'aac', str(sample)], check=True)
        fetched = []
        def fetch(selected, mode, destination, cancelled):
            fetched.append(selected.stable_key)
            output = Path(destination)/'video.mp4'
            output.write_bytes(sample.read_bytes())
            return output
        engine = SimpleNamespace(display_name='隔离模型', transcribe=lambda *args, **kwargs: Transcript('这是一段完整口述', [], 'zh', 'local_sensevoice_q8'))
        runtime = self.runtime()
        with patch.object(models_manager, 'app_data_dir', return_value=self.root/'data'), \
                patch('src.core.collect.media_fetcher.fetch_managed_media', side_effect=fetch), \
                patch('src.core.pipeline.get_engine', return_value=engine), \
                patch('src.core.pipeline.resolve_source', wraps=__import__('src.core.audio.extractor', fromlist=['resolve_source']).resolve_source) as resolver:
            repository = CollectionRepository()
            repository.upsert_collected([item])
            service = runtime.expansion_batches()
            plan = service.prepare('reuse-collected', [dict(source=item.work_url, stable_key=item.stable_key)], 'local_sensevoice_q8',
                save_media=True, media_directory=str(self.root))
            service.start(plan['batch_id'], plan['plan_hash'], True)
            until(lambda:not service.workers)
            result = service.show(plan['batch_id'])
            self.assertEqual(result['state'], 'succeeded', result)
            member = result['items'][0]
            self.assertTrue(Path(member['files']['video']['path']).is_file())
            self.assertIn('这是一段完整口述', library.load(member['document_id']).text)
            self.assertEqual(fetched, [item.stable_key])
            self.assertFalse(any(str(call.args[0]).startswith('https:') for call in resolver.call_args_list))
            self.assertEqual(repository.load().items[0].transcript_status, 'completed')

    def test_cloud_large_intake_and_last_page_use_formal_cli_without_upload(self):
        runtime=self.runtime()
        sources=[dict(source=f'https://example.com/large-{i}.wav') for i in range(10001)]
        with patch('src.core.secrets.get_config',side_effect=AssertionError('intake must not read credentials')), patch('src.core.pipeline.transcribe_source',side_effect=AssertionError('intake must not upload')):
            plan=self.call('batches.prepare',dict(request_id='large-cli-intake',items=sources,engine='dashscope'),runtime)
            last=self.call('batches.show',dict(batch_id=plan['batch_id'],offset=10000,limit=1),runtime)
        self.assertEqual(plan['total'],10001)
        self.assertEqual(plan['cloud_concurrency'],None)
        self.assertEqual(last['items'][0]['source'],sources[-1]['source'])
        self.assertEqual(last['items'][0]['index'],10000)
        self.assertFalse(runtime.expansion_batches().workers)
        self.assertIn('第一段',library.load(self.eid).text)
