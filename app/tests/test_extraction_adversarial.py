"""Adversarial extraction timing and file checks, isolated from the installed App."""
from dataclasses import asdict
import threading
import unittest
from unittest.mock import patch

from src.core import library
from src.core.agent_workspace.batches import BatchService
from src.core.agent_workspace import documents
from src.core.agent_workspace.store import encode
from src.core.models import Transcript
from tests import test_extraction_batches as fixtures
from tests import test_cloud_auto as cloud_fixtures
from tests.test_live_recording import until


class ExtractionAdversarialTests(unittest.TestCase):
    setUp = fixtures.ExtractionBatchTests.setUp
    control = fixtures.ExtractionBatchTests.control

    def test_cancel_paused_items_and_dismiss_undo_do_not_restore_execution(self):
        service = BatchService(None, runner=lambda *a: self.fail('控制草稿不应执行'))
        plan = service.stage_extraction('cancel-paused', [dict(source=f'https://example.com/{n}.mp4') for n in range(3)])
        self.control(service, plan, 'pause', [0])
        self.control(service, plan, 'pause')
        paused = service.show(plan['batch_id'])['items']
        self.assertFalse(paused[0]['auto_paused'])
        self.assertTrue(paused[1]['auto_paused'])
        self.control(service, plan, 'cancel', [0,1])
        self.assertEqual([i['state'] for i in service.show(plan['batch_id'])['items']],
                         ['cancelled','cancelled','paused'])
        removed = self.control(service, plan, 'dismiss')
        restored = service.control_extraction('undo-paused', plan['batch_id'], plan['plan_hash'], 'restore',
                                              dismissal_id=removed['dismissal_id'])
        self.assertTrue(all(i['state']=='cancelled' for i in restored['items']))
        self.control(service, plan, 'resume', network_consent=True)
        self.assertFalse(service.workers)

    def test_lost_unselected_asset_does_not_fail_completed_text(self):
        video = self.root/'video.mp4'
        video.write_bytes(b'video')
        source = 'https://example.com/completed.mp4'
        library.save(Transcript('已完成文案', [], engine='local_sensevoice_q8',
            source=source, meta={'extraction_assets': {'video': str(video)}}), '文案')
        service = BatchService(None, runner=lambda *a: self.fail('不应执行'))
        plan = service.stage_extraction('unselected', [dict(source=source, outputs=['text'])])
        self.assertEqual(plan['items'][0]['assets']['video']['state'], 'ready')
        video.unlink()
        result = service.show(plan['batch_id'])
        self.assertEqual(result['items'][0]['assets']['video']['state'], 'failed')
        self.assertEqual(result['items'][0]['state'], 'succeeded')
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['counts']['succeeded'], 1)
        self.assertEqual(result['startable_count'], 0)

    def test_source_replaced_with_identical_symlink_is_refused_before_runner(self):
        calls = []
        source = self.root/'source.wav';source.write_bytes(b'local fixture')
        target = self.root/'target.wav';target.write_bytes(source.read_bytes())
        service = BatchService(None, runner=lambda item,batch,*a:
            (calls.append(item['source']) or Transcript('不应取得', [], engine=batch['engine'])))
        plan = service.stage_extraction('swap-link', [dict(source=str(source))])
        source.unlink();source.symlink_to(target)
        self.control(service, plan, 'start', [0])
        until(lambda: not service.workers)
        self.assertEqual(calls, [])
        self.assertEqual(service.show(plan['batch_id'])['items'][0]['state'], 'failed')

    def test_asset_parent_replaced_with_symlink_is_not_reused(self):
        real = self.root/'real';real.mkdir()
        file = real/'video.mp4';file.write_bytes(b'video')
        alias = self.root/'alias';alias.symlink_to(real, target_is_directory=True)
        self.assertFalse(BatchService._usable_asset(str(alias/'video.mp4')))

    def test_active_checkpoint_cannot_be_queued_as_a_second_save(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def runner(item, batch, *args):
            calls.append(item['index'])
            raw = asdict(Transcript('已识别，素材仍在收尾', [], engine=batch['engine']))
            path = service._extraction_result_path(batch['batch_id'], item)
            library._write_atomic_text(path, encode(dict(transcript=raw,
                sha256=documents.digest(encode(raw)), complete=False)))
            entered.set();release.wait(5)
            return Transcript('完成正文', [], engine=batch['engine'])
        service = BatchService(None, runner=runner)
        plan = service.stage_extraction('checkpoint', [dict(source='https://example.com/check.mp4')], engine='dashscope')
        try:
            self.control(service, plan, 'start', [0], network_consent=True, cloud_upload_consent=True)
            self.assertTrue(entered.wait(2))
            item = service.show(plan['batch_id'])['items'][0]
            with self.subTest('active checkpoint is not a recovery action'):
                self.assertFalse(item['save_pending'])
            with self.subTest('Core refuses premature recovery'):
                with self.assertRaisesRegex(ValueError, '处理'):
                    self.control(service, plan, 'check', [0])
            for action in ('start', 'retry'):
                self.control(service, plan, action, [0], network_consent=True, cloud_upload_consent=True)
            self.assertEqual(service.show(plan['batch_id'])['items'][0]['state'], 'running')
        finally:
            release.set();until(lambda: not service.workers)
        self.assertEqual(calls, [0])
        self.assertEqual(service.show(plan['batch_id'])['items'][0]['state'], 'succeeded')


class ExtractionCloudTransportTests(unittest.TestCase):
    """Real extraction preparation/HTTP, with a loopback-only fake provider."""
    setUp = fixtures.ExtractionBatchTests.setUp
    control = fixtures.ExtractionBatchTests.control
    audio = cloud_fixtures.CloudAutoTests.audio
    fixture = cloud_fixtures.CloudAutoTests.fixture
    environment = cloud_fixtures.CloudAutoTests.environment
    settled = cloud_fixtures.CloudAutoTests.settled

    def cloud(self, hold=False, status=200, slots=2):
        from src.core.engines.cloud_requests import CloudBatchEngine
        state, config = self.fixture(hold=hold, status=status)
        self.environment(config)
        service = BatchService(None)
        p = patch.object(service, '_prepare_cloud_engine', side_effect=lambda provider:
            CloudBatchEngine(provider, config, limits={'requests_per_second':1000, 'max_inflight':slots}))
        p.start();self.addCleanup(p.stop)
        return service, state

    def stage(self, service, name, first, count):
        return service.stage_extraction(name, [dict(source=str(self.audio(n)), title=f'本机模拟{n}')
            for n in range(first, first+count)], engine='custom_openai', group=True)

    def start(self, service, plan, action='start'):
        return self.control(service, plan, action, network_consent=True, cloud_upload_consent=True)

    def test_workspace_batches_share_slots_and_keep_exact_results(self):
        service, state = self.cloud(hold=True)
        first = self.stage(service, 'first', 0, 3)
        second = self.stage(service, 'second', 3, 3)
        try:
            self.start(service, first);self.start(service, second)
            until(lambda: state['calls'] >= 2)
            # A third request cannot pass while the shared two slots are held.
            threading.Event().wait(.2)
            self.assertEqual(state['calls'], 2)
        finally:
            state['release'].set();self.settled(service)
        self.assertLessEqual(state['peak'], 2)
        self.assertEqual(state['calls'], 6)
        for plan in (first, second):
            for item in service.show(plan['batch_id'])['items']:
                self.assertEqual(item['state'], 'succeeded', item)
                self.assertIn('结束标记', library.load(item['document_id']).text)
        self.start(service, first)
        self.assertEqual(state['calls'], 6)

    def test_workspace_429_retries_but_sent_500_never_replays(self):
        service, state = self.cloud(status=429)
        plan = self.stage(service, 'rate-refusal', 0, 1)
        self.start(service, plan)
        until(lambda: state['calls'] == 1)
        state['status'] = 200;self.settled(service)
        self.assertEqual(state['calls'], 2)
        self.assertGreaterEqual(state['times'][1]-state['times'][0], 1.9)
        self.assertEqual(service.show(plan['batch_id'])['counts']['succeeded'], 1)
        state['status'] = 500
        unknown = self.stage(service, 'sent-unknown', 1, 1)
        self.start(service, unknown);self.settled(service)
        self.assertEqual(service.show(unknown['batch_id'])['unconfirmed_count'], 1)
        before = state['calls']
        for action in ('start', 'retry', 'resume'):
            self.start(service, unknown, action);self.settled(service)
        self.assertEqual(state['calls'], before)

    def test_regular_cancel_preserves_a_late_cloud_success(self):
        service, state = self.cloud(hold=True)
        plan = self.stage(service, 'cancel-sent', 0, 1)
        try:
            self.start(service, plan);until(lambda: state['calls']==1)
            self.control(service, plan, 'cancel', [0])
            threading.Event().wait(.3)
        finally:
            state['release'].set();self.settled(service)
        item = service.show(plan['batch_id'])['items'][0]
        self.assertEqual(item['state'], 'succeeded', item)
        self.assertIn('结束标记', library.load(item['document_id']).text)
        self.assertEqual(state['calls'], 1)

    def test_account_refusal_stops_unsent_work_in_both_active_batches(self):
        service, state = self.cloud(hold=True, status=429, slots=1)
        state['error_code'] = 'insufficient_quota'
        first = self.stage(service, 'account-one', 0, 3)
        second = self.stage(service, 'account-two', 3, 3)
        try:
            self.start(service, first);self.start(service, second)
            until(lambda: state['calls']==1)
        finally:
            state['release'].set();self.settled(service)
        self.assertEqual(state['calls'], 1)
        for plan in (first, second):
            view = service.show(plan['batch_id'])
            self.assertEqual(view['unconfirmed_count'], 0, view)
            self.assertEqual(view['counts']['succeeded'], 0)
        # After both explicit runs have settled, a new user-started run can use
        # repaired account configuration instead of retaining a permanent halt.
        state['status'] = 200
        repaired = self.stage(service, 'account-repaired', 6, 1)
        self.start(service, repaired);self.settled(service)
        self.assertEqual(state['calls'], 2)
        self.assertEqual(service.show(repaired['batch_id'])['counts']['succeeded'], 1)


if __name__ == '__main__':
    unittest.main()
