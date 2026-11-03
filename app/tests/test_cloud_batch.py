"""Isolated billing-consent, scheduling, persistence and process cancellation checks."""
import json
import multiprocessing
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import wave

from src.core import library
from src.core.agent_workspace.batches import BatchService
from src.core.agent_workspace.service import WorkspaceService
from src.core.engines.dashscope_engine import DashScopeEngine
from src.core.models import Transcript, Segment
from tests.test_agent_workspace import AgentWorkspaceTests
from tests.test_cli_expansion import ExpansionTests
from tests.test_live_recording import until


def fake_recognition(connection):
    args = connection.recv()
    time.sleep(.1)
    connection.send(dict(ok=True, transcript=dict(text='完整结果', segments=[dict(text='完整结果', start=0., end=1.)], language='zh', engine=args['engine_id'])))
    connection.close()


def waiting_recognition(connection):
    connection.recv()
    time.sleep(30)


class CloudBatchTests(unittest.TestCase):
    setUp = AgentWorkspaceTests.setUp
    runtime = ExpansionTests.runtime
    invoke = ExpansionTests.invoke
    call = ExpansionTests.call

    def test_upload_consent_is_separate_and_parallelism_bounded_with_exact_results(self):
        active, peak, calls, lock = [0], [0], [], threading.Lock()
        def runner(item, batch, progress, cancel):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                calls.append(item['source'])
            time.sleep(.08)
            with lock:
                active[0] -= 1
            return Transcript(item['title'], [Segment(item['title'], 0., 1.)], 'zh', 'dashscope')
        runtime = self.runtime(runner)
        plan = self.call('batches.prepare', dict(request_id='cloud', engine='dashscope', cloud_concurrency=2,
            items=[dict(source=f'https://example.com/{n}.mp3', title=f'正文{n}') for n in range(5)]), runtime)
        args = dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'], network_consent=True)
        code, reply = self.invoke('batches.start', args, runtime)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.call('batches.start', dict(args, cloud_upload_consent=True), runtime)
        service = runtime.expansion_batches()
        until(lambda:not service.workers)
        result = service.show(plan['batch_id'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(peak[0], 2)
        for item in result['items']:
            saved = library.load(item['document_id'])
            self.assertIn(item['title'], saved.text)
            self.assertEqual([s.text for s in saved.segments], [item['title']])
        self.call('batches.start', dict(args, cloud_upload_consent=True, resume=True), runtime)
        self.assertEqual(len(calls), 5)
        self.assertEqual(BatchService(runtime).show(plan['batch_id'])['state'], 'succeeded')

    def test_cancel_stops_new_uploads_and_resume_reuses_completed_result(self):
        entered, calls = threading.Event(), []
        def runner(item, batch, progress, cancel):
            calls.append(item['index'])
            entered.set()
            until(cancel)
            return Transcript('已返回结果', [], 'zh', 'dashscope')
        runtime = self.runtime(runner)
        service = runtime.expansion_batches()
        plan = service.prepare('cancel-cloud', [dict(source=f'https://example.com/{n}.mp3') for n in range(4)], 'dashscope', cloud_concurrency=2)
        service.start(plan['batch_id'], plan['plan_hash'], True, cloud_upload_consent=True)
        self.assertTrue(entered.wait(2))
        service.cancel(plan['batch_id'])
        until(lambda:not service.workers)
        self.assertLessEqual(len(calls), 2)
        service.runner = lambda *args: self.fail('已返回的结果不应再次上传')
        # Cancelled before upload items are removed from this explicit test plan;
        # replay only the completed saved result through the per-item seam.
        batch = service._read(plan['batch_id'])
        for item in batch['items']:
            if service._result_path(plan['batch_id'], item['index']).exists():
                service._run_one(batch, item, threading.Event())
                self.assertEqual(item['state'], 'succeeded')

    def audio(self):
        path = self.root/'speech.wav'
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0'*16000)
        return path

    def test_owned_cloud_process_returns_and_cancellation_reaps_child(self):
        engine = DashScopeEngine(engine_id='dashscope', display_name='测试', model='paraformer-realtime-v2', api_key='fixture-secret')
        before = {p.pid for p in multiprocessing.active_children()}
        with patch('src.core.engines.dashscope_engine._isolated_recognition', fake_recognition):
            self.assertEqual(engine.transcribe_bounded(self.audio()).text, '完整结果')
        start = time.monotonic()
        with patch('src.core.engines.dashscope_engine._isolated_recognition', waiting_recognition):
            with self.assertRaises(InterruptedError):
                engine.transcribe_bounded(self.audio(), is_cancelled=lambda:time.monotonic()-start>.4)
        self.assertLess(time.monotonic()-start, 4)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        self.assertFalse(any('fixture-secret' in p.read_text(errors='ignore') for p in self.root.rglob('*.json')))

    def test_gui_same_service_upload_start_poll_and_reopen(self):
        from PySide6.QtWidgets import QApplication, QWidget, QMessageBox
        from src.ui.cloud_batch import CloudBatchDialog
        app = QApplication.instance() or QApplication([])
        runtime = self.runtime(lambda item, *args: Transcript('界面保存的正文', [], 'zh', 'dashscope'))
        service = WorkspaceService(runtime=runtime)
        def dispatch(operation, **arguments):
            return getattr(service, operation)(**arguments)
        main = QWidget()
        main.open_material_entry = lambda *args:None
        with patch('src.core.agent_workspace.client.dispatch', side_effect=dispatch), patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            dialog = CloudBatchDialog(main)
            def settled(test):
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    app.processEvents()
                    if test():return
                    time.sleep(.01)
                self.fail('界面批次未完成')
            settled(lambda:dialog.worker is None)
            dialog.sources.setPlainText('https://example.com/one.mp3\nhttps://example.com/two.mp3')
            dialog.create()
            settled(lambda:dialog.selection.count()==1 and getattr(dialog,'batch',{}).get('state')=='succeeded')
            bid=dialog.batch['batch_id']
            self.assertEqual(dialog.batch['counts']['succeeded'],2)
            dialog.close()
            dialog.timer.start()
            dialog.refresh()
            settled(lambda:dialog.worker is None)
            self.assertEqual(dialog.selection.currentData(),bid)
            self.assertTrue(all('界面保存的正文' in library.load(i['document_id']).text for i in dialog.batch['items']))
            dialog.timer.stop()
            settled(lambda:dialog.worker is None)
        dialog.deleteLater();main.deleteLater();app.processEvents()

    def test_denied_credentials_stop_whole_batch_without_second_read_or_upload(self):
        runtime = self.runtime()
        service = runtime.expansion_batches()
        plan = service.prepare('deny-cloud', [dict(source=f'https://example.com/{n}.mp3') for n in range(4)], 'dashscope', cloud_concurrency=2)
        with patch('src.core.secrets.get_config', side_effect=RuntimeError('CREDENTIAL_READ_UNAVAILABLE')) as config, \
                patch('src.core.pipeline.transcribe_source') as upload:
            service.start(plan['batch_id'], plan['plan_hash'], True, cloud_upload_consent=True)
            until(lambda:not service.workers)
        config.assert_called_once_with('dashscope', require_access=True, refresh=True)
        upload.assert_not_called()
        result = service.show(plan['batch_id'])
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['reason_code'], 'CLOUD_CONFIGURATION_UNAVAILABLE')
        self.assertEqual(result['counts']['pending'], 4)
        self.assertIn('未再次尝试读取', result['message'])

    def test_cloud_engine_frozen_once_in_memory_and_saved_results_need_no_credentials(self):
        runtime = self.runtime()
        service = runtime.expansion_batches()
        plan = service.prepare('freeze-cloud', [dict(source=f'https://example.com/{n}.mp3') for n in range(3)], 'dashscope', cloud_concurrency=2)
        engines=[]
        def transcribe(source, **kwargs):
            engines.append(kwargs['engine_override'])
            self.assertTrue(kwargs['bounded_cloud'])
            return Transcript('完整正文 '+source, [], 'zh', 'dashscope')
        with patch('src.core.secrets.get_config', return_value={'api_key':'fixture-memory-only'}) as config, \
                patch('src.core.pipeline.transcribe_source', side_effect=transcribe):
            service.start(plan['batch_id'], plan['plan_hash'], True, cloud_upload_consent=True)
            until(lambda:not service.workers)
        config.assert_called_once()
        self.assertEqual(len(engines), 3)
        self.assertTrue(all(engine is engines[0] for engine in engines))
        self.assertEqual(service.show(plan['batch_id'])['state'], 'succeeded')
        self.assertFalse(any('fixture-memory-only' in p.read_text() for p in service.root.glob('*.json')))
        # Simulate a saved result awaiting document reconciliation after restart.
        batch=service._read(plan['batch_id']);batch['state']='interrupted';batch['items'][0]['state']='recovery_required';service._save(batch)
        with patch('src.core.secrets.get_config', side_effect=AssertionError('saved result must not read credentials')):
            service.start(plan['batch_id'], plan['plan_hash'], True, resume=True, cloud_upload_consent=True)
            until(lambda:not service.workers)
        self.assertEqual(service.show(plan['batch_id'])['state'],'succeeded')

    def test_missing_cloud_key_is_distinguished_from_denied_access_without_upload(self):
        service=self.runtime().expansion_batches()
        plan=service.prepare('missing-cloud-key',[dict(source='https://example.com/one.mp3')],'volcengine')
        with patch('src.core.secrets.get_config',return_value={}) as config, patch('src.core.pipeline.transcribe_source') as upload:
            service.start(plan['batch_id'],plan['plan_hash'],True,cloud_upload_consent=True)
            until(lambda:not service.workers)
        config.assert_called_once_with('volcengine',require_access=True,refresh=True);upload.assert_not_called()
        result=service.show(plan['batch_id'])
        self.assertEqual(result['reason_code'],'CLOUD_CONFIGURATION_MISSING')
        self.assertIn('内容提取',result['message'])
        self.assertIn('尚未上传',result['message'])

    def test_new_batch_uses_credential_saved_outside_core_and_denial_never_reuses_old_key(self):
        from unittest.mock import Mock
        from src.core import secrets
        service = self.runtime().expansion_batches()
        backend = Mock()
        backend.get_password.return_value = '{"api_key":"first-fixture-only"}'
        with patch.dict(secrets._cache, {}, clear=True), patch.dict(secrets._memory, {}, clear=True), \
                patch.object(secrets, '_backend', return_value=backend):
            first = service._prepare_cloud_engine('siliconflow')
            self.addCleanup(first.close)
            # The GUI has a different in-memory cache. Its durable save changes
            # the shared backend while this long-lived Core still has the old key.
            backend.get_password.return_value = '{"api_key":"second-fixture-only"}'
            second = service._prepare_cloud_engine('siliconflow')
            self.addCleanup(second.close)
            self.assertEqual(first.config['api_key'], 'first-fixture-only')
            self.assertEqual(second.config['api_key'], 'second-fixture-only')
            secrets._memory['siliconflow'] = '{"api_key":"legacy-fixture-only"}'
            backend.get_password.return_value = None
            with self.assertRaisesRegex(RuntimeError, '^CLOUD_CONFIGURATION_MISSING$'):
                service._prepare_cloud_engine('siliconflow')
            backend.get_password.side_effect = RuntimeError('fixture denied')
            with self.assertRaisesRegex(RuntimeError, '^CREDENTIAL_READ_UNAVAILABLE$'):
                service._prepare_cloud_engine('siliconflow')
            self.assertEqual(backend.get_password.call_count, 4)

    def test_local_failure_never_claims_cloud_billing(self):
        def failing(*args):raise RuntimeError('isolated local model failure')
        service=self.runtime(failing).expansion_batches()
        plan=service.prepare('local-error-copy',[dict(source='https://example.com/local.wav')],'local_sensevoice_q8')
        service.start(plan['batch_id'],plan['plan_hash'],True)
        until(lambda:not service.workers)
        self.assertNotIn('计费',service.show(plan['batch_id'])['items'][0]['message'])
