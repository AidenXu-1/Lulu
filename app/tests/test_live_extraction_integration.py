"""Live-to-extraction seams; temporary media, real Qt/FFmpeg, fake inference only."""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import wave
from unittest.mock import Mock, patch
import unittest

from tests import test_extraction_main_window as extraction_fixture
from tests import test_live_recording_ui as live_fixture
from src.core import extraction, library, models_manager
from src.core.models import Transcript
from src.core.engines import asr_resource, registry
from src.core.output.writer import render
from src.core.task import TaskState
from src.core.task_queue_store import TaskQueueStore
from src.core.gui_task_recovery import GuiTaskRecoveryStore
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.live_recording.contracts import Observation
from src.core.live_recording.service import LiveService
from src.core.live_recording.comments import CommentLog, log_path
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class LiveExtractionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        extraction_fixture.ExtractionMainWindowTests.setUpClass()

    def setUp(self):
        self.fixture = extraction_fixture.ExtractionMainWindowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.main = self.fixture.main
        self.widget = self.fixture.widget
        self.root = self.fixture.root
        self.service = None
        self.original_files = {}
        self.addCleanup(self.stop_work)

    def stop_work(self):
        if self.main.current:
            self.main.current[1].request_cancel()
            self.fixture.until(lambda: self.main.current is None)
        asr_resource.finish_batch(getattr(self.main, '_asr_batch_owner', None))
        self.main.live_controller.request_stop()
        self.fixture.until(lambda: self.main.live_controller.settled)
        if self.service:
            self.service.shutdown()
        for path, original in self.original_files.items():
            self.assertEqual(path.read_bytes(), original)

    def completed_segment(self):
        if self.service is None:
            control = JobControlService(JobLedgerStore(self.root / 'ledger', FakeIntegrityAnchorStore()))
            self.service = LiveService(self.root / 'live', control, 'fixture',
                provider=SimpleNamespace(check=lambda *_: Observation('offline')),
                secret_read=lambda: self.fail('credentials accessed'), secret_write=lambda _: None,
                disk_free=lambda _: 100 * 1024**3)
            self.output = self.root / 'live-recordings'
            self.output.mkdir()
            self.service.index.set_setting('directory', str(self.output))
            self.main.live_controller.dispatcher = self.service.handle
            self.main.live_controller.start()
            self.fixture.until(lambda: self.main.live_controller.connected)
        return live_fixture.NativeLiveTests.completed_segment(self)

    def handoff(self):
        rid, media = self.completed_segment()
        record = self.service._recording(rid)
        CommentLog(record).append([dict(kind='chat', received_at='2026-10-17T15:00:01+08:00',
            elapsed_seconds=1, nickname='隔离观众🙂', text='原弹幕\n第二行', message_id='fixture-1')])
        for path in (media, log_path(media.parent)):
            self.original_files[path] = path.read_bytes()
        self.main._switch_page(9)
        page = self.main.live_recording_page
        self.fixture.until(lambda: bool(page.data))
        self.main.live_controller.call('detail', recording_id=rid, callback=page._detail)
        self.fixture.until(lambda: page.detail_dialog and page.detail_dialog.extract_button.isEnabled())
        page.detail_dialog.extract_button.click()
        self.fixture.until(lambda: self.main.stack.currentIndex() == 0)
        task = self.main.tasks[-1]
        self.assertEqual(task.source, str(media))
        self.assertEqual(task.state, TaskState.PENDING)
        self.assertIsNone(self.main.current)
        return task, media

    def start_task(self, task):
        self.widget.publish(force=True)
        self.widget.dispatch('start', {'ids': [self.widget._ids[task.id]]})

    def fake_inference(self):
        """Use real engine adapters/locks, replacing only weights and inference calls."""
        stack = self.fixture.stack
        self.inference_calls = []
        stack.enter_context(patch.object(registry, '_local_instances', {}))
        stack.enter_context(patch.object(registry, 'runtime_issue', return_value=None))
        stack.enter_context(patch.object(registry, 'is_ready', return_value=True))
        stack.enter_context(patch.object(models_manager, 'is_downloaded', return_value=True))
        stack.enter_context(patch.object(models_manager, 'model_path', side_effect=lambda name: self.root / 'models' / name))
        stack.enter_context(patch('src.core.engines.local_native.moss_preflight'))

        def native(tool, args, *, is_cancelled, progress):
            work = self.root / 'native-work'
            work.mkdir(exist_ok=True)
            arguments = args(work)
            wav = Path(arguments[arguments.index('-a') + 1]) if tool == 'sensevoice-q8' else Path((work / 'input.txt').read_text().strip())
            with wave.open(str(wav)) as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels()), (16000, 1))
            self.inference_calls.append(tool)
            if tool == 'sensevoice-q8':
                return '你好。再见。', 'fixture'
            return json.dumps(dict(file=str(wav), text='你好。再见。', segments=[
                dict(text='你好。', t0_ms=0, t1_ms=900, speaker_id=1),
                dict(text='再见。', t0_ms=1000, t1_ms=1800, speaker_id=2)],
                raw_text='[0.00][S01]你好。[0.90][1.00][S02]再见。[1.80]')), 'fixture'

        def whisper(audio, **kwargs):
            self.assertGreater(len(audio), 16000)
            self.inference_calls.append(Path(kwargs['path_or_hf_repo']).name)
            return dict(text='你好。再见。', language='zh', segments=[
                dict(text='你好。', start=0, end=.9), dict(text='再见。', start=1, end=1.8)])

        stack.enter_context(patch('src.core.engines.local_native.run_native', side_effect=native))
        stack.enter_context(patch('src.core.engines.local_whisper_mlx.transcribe_with_audio_progress', side_effect=whisper))

    def test_live_retry_cannot_rebind_to_cloud_after_failure_or_cancel(self):
        _, media = self.completed_segment()
        for state, legacy in ((state, legacy) for state in (TaskState.FAILED, TaskState.CANCELLED) for legacy in (False, True)):
            with self.subTest(state=state, legacy=legacy):
                self.main.cfg.engine_name = 'local_moss_q5'
                self.assertTrue(self.main._live_transcribe(str(media)))
                task = self.main.tasks[-1]
                if legacy:
                    from src.core.task_queue_store import _policy_from_dict, _policy_to_dict, SCHEMA_VERSION
                    old = _policy_to_dict(task.execution_policy)
                    old.pop('local_only')
                    task.execution_policy = _policy_from_dict(old, schema_version=SCHEMA_VERSION)
                    self.assertFalse(task.execution_policy.local_only)
                task.state = state
                self.main.cfg.engine_name = 'openai'
                self.widget.publish(force=True)
                with patch('src.core.pipeline.get_engine') as engine, \
                     patch('src.core.pipeline.to_wav_16k_mono') as convert:
                    self.start_task(task)
                    self.fixture.until(lambda: self.main.current is None)
                    engine.assert_not_called()
                    convert.assert_not_called()
                self.assertEqual(task.state, TaskState.FAILED)
                self.assertIn('仅支持本地', task.error)
                self.assertTrue(task.execution_policy.local_only)

    def test_four_local_models_from_live_button_through_save_reopen_and_export(self):
        self.fake_inference()
        for engine in models_manager.LOCAL_MODELS:
            with self.subTest(engine=engine):
                self.main.cfg.engine_name = engine
                task, media = self.handoff()
                before = media.read_bytes()
                # A later global change cannot replace this admitted local choice.
                self.main.cfg.engine_name = 'openai'
                self.start_task(task)
                self.fixture.until(lambda: self.main.current is None)
                self.assertEqual(task.state, TaskState.DONE, task.error)
                loaded = extraction.load_result(str(task.id))
                reopened = library.load(task.library_id)
                self.assertEqual(loaded.engine, engine)
                self.assertEqual(reopened.engine, engine)
                self.assertEqual(reopened.segments, loaded.segments)
                self.assertEqual(loaded.meta['asr']['model_id'], engine)
                self.assertEqual(loaded.source, str(media))
                if engine == 'local_sensevoice_q8':
                    with self.assertRaises(ValueError): render(reopened, 'srt')
                else:
                    self.assertIn('-->', render(reopened, 'srt'))
                if engine == 'local_moss_q5':
                    self.assertEqual([s.speaker_id for s in reopened.segments], [1, 2])
                self.assertEqual(media.read_bytes(), before)
                self.assertEqual(self.main.stack.currentIndex(), 0)
        self.assertEqual(len(self.inference_calls), 4)
        with asr_resource.maintenance(): pass

    def test_regular_file_retry_keeps_explicit_global_model_choice(self):
        _, media = self.completed_segment()
        task = self.main._add_task(str(media), 'file', '普通导入文件')
        task.execution_policy = replace(task.execution_policy, engine_name='local_whisper_mlx')
        task.state = TaskState.FAILED
        self.main.cfg.engine_name = 'openai'
        def transcribe(wav, progress, is_cancelled):
            return Transcript(text='模拟云端结果', engine='openai')
        engine = SimpleNamespace(display_name='模拟云端', transcribe=transcribe)
        with patch('src.core.pipeline.get_engine', return_value=engine) as selected:
            self.start_task(task)
            self.fixture.until(lambda: self.main.current is None)
        selected.assert_called_once_with('openai')
        self.assertEqual(task.state, TaskState.DONE, task.error)
        self.assertFalse(task.execution_policy.local_only)

    def test_live_local_limit_survives_queue_and_recovery_and_local_model_retry(self):
        self.fake_inference()
        task, media = self.handoff()
        store = TaskQueueStore(self.root / 'queue.json')
        self.assertTrue(store.save([task], []))
        restored = TaskQueueStore(store.path).load().tasks[0]
        self.assertTrue(restored.execution_policy.local_only)
        restored.id = 'gui-job-joint-review'
        recovery = GuiTaskRecoveryStore(self.root / 'recovery.json')
        recovery.save([restored], [])
        from src.core.task_queue_store import TaskQueueSnapshot
        merged, found = recovery.merge(TaskQueueSnapshot((), ()))
        self.assertTrue(found)
        restored = merged.tasks[0]
        self.assertTrue(restored.execution_policy.local_only)
        restored.state = TaskState.FAILED
        self.main.tasks = [restored]
        self.main.cfg.engine_name = 'local_whisper_mlx_4bit'
        self.start_task(restored)
        self.fixture.until(lambda: self.main.current is None)
        self.assertEqual(restored.state, TaskState.DONE, restored.error)
        self.assertEqual(restored.transcript.engine, 'local_whisper_mlx_4bit')
        self.assertTrue(media.is_file())

    def test_shared_asr_busy_fails_live_task_then_retry_keeps_original(self):
        self.fake_inference()
        task, media = self.handoff()
        before = media.read_bytes()
        with asr_resource.batch_context('other-batch'):
            with asr_resource.model_use('fixture', retain=True): pass
        try:
            self.start_task(task)
            self.fixture.until(lambda: self.main.current is None)
            self.assertEqual(task.state, TaskState.FAILED)
            self.assertIn('正在使用', task.error)
            self.assertEqual(self.inference_calls, [])
            # The independent live control channel remains usable while ASR is held.
            paused = Mock()
            self.main.live_controller.call('pause_all', callback=paused)
            self.fixture.until(lambda: paused.called)
        finally:
            asr_resource.finish_batch('other-batch')
        self.start_task(task)
        self.fixture.until(lambda: self.main.current is None)
        self.assertEqual(task.state, TaskState.DONE, task.error)
        self.assertEqual(media.read_bytes(), before)

    def test_cancel_discards_late_native_result_and_advances_next_task(self):
        self.fake_inference()
        first, media = self.handoff()
        second, _ = self.handoff()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return '迟到的结果', 'fixture'
        with patch('src.core.engines.local_native.run_native', side_effect=delayed):
            self.start_task(first)
            self.fixture.until(entered.is_set)
            self.start_task(second)
            self.main._cancel_task(first.id)
            self.main._switch_page(9)
            release.set()
            self.fixture.until(lambda: self.main.current is None)
        self.assertEqual(first.state, TaskState.CANCELLED)
        self.assertIsNone(first.library_id)
        self.assertEqual(second.state, TaskState.DONE, second.error)
        self.assertIsNone(first.transcript)
        self.assertTrue(media.is_file())
        with asr_resource.maintenance(): pass

    def test_close_settles_live_read_and_native_task_without_publishing_late_text(self):
        self.fake_inference()
        task, media = self.handoff()
        entered, cancelled = threading.Event(), threading.Event()
        read_entered, read_release = threading.Event(), threading.Event()
        original = self.main.live_controller.dispatcher
        def slow_read(operation, arguments):
            if operation == 'data':
                read_entered.set()
                read_release.wait(5)
            return original(operation, arguments)
        def inference(*args, is_cancelled, **kwargs):
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if is_cancelled():
                    cancelled.set()
                    break
                time.sleep(.01)
            return '退出时的迟到结果', 'fixture'
        self.main.live_controller.dispatcher = slow_read
        rid = self.service.index.one('SELECT id FROM recordings')['id']
        replies = Mock()
        with patch('src.core.engines.local_native.run_native', side_effect=inference), \
             patch('src.ui.main_window.dialogs.confirm', return_value=True), \
             patch.object(self.main, '_choose_xhs_exit_action', return_value='keep'), \
             patch.object(self.main, '_settle_xhs_exit', return_value=True):
            self.start_task(task)
            self.fixture.until(entered.is_set)
            self.main.live_controller.call('data', recording_id=rid, callback=replies)
            self.fixture.until(read_entered.is_set)
            try:
                self.assertFalse(self.main.close())
                self.assertTrue(self.main.isVisible())
                self.fixture.until(lambda: self.service.closing)
                self.assertFalse(self.main.live_controller.settled)
            finally:
                read_release.set()
            self.fixture.until(lambda: not self.main.isVisible())
            self.fixture.until(lambda: self.main.current is None)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.main.live_controller.settled)
        replies.assert_not_called()
        self.assertIsNone(task.library_id)
        self.assertIsNone(task.transcript)
        self.assertTrue(media.is_file())
        with asr_resource.maintenance(): pass


class LivePolicyStorageTests(unittest.TestCase):
    def test_local_only_roundtrip_is_optional_and_strict(self):
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        from src.core.task import TaskExecutionPolicy
        from src.core.task_queue_store import _policy_to_dict, _policy_from_dict, SCHEMA_VERSION, TaskQueueStoreError
        from src.core.gui_task_ledger import _execution_policy
        policy = TaskExecutionPolicy('single_link', 'materialize', '', MaterialPreferenceSnapshot(), 'none')
        legacy = _policy_to_dict(policy)
        self.assertNotIn('local_only', legacy)
        self.assertFalse(_policy_from_dict(legacy, schema_version=SCHEMA_VERSION).local_only)
        current = replace(policy, local_only=True)
        self.assertEqual(_execution_policy(current), _policy_to_dict(current))
        self.assertTrue(_policy_from_dict(_execution_policy(current), schema_version=SCHEMA_VERSION).local_only)
        for value in (None, 1, 'true', []):
            with self.subTest(value=value), self.assertRaises(TaskQueueStoreError):
                _policy_from_dict(dict(legacy, local_only=value), schema_version=SCHEMA_VERSION)

    def test_core_keeps_local_limit_when_editing_or_retrying_policy(self):
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        from src.core.task import TaskExecutionPolicy, Task
        from src.core.task_queue_store import _policy_to_dict
        from src.core.gui_task_ledger import _task_payload
        from src.core.job_ledger.contracts import JobLedgerError
        with TemporaryDirectory() as directory:
            anchor = FakeIntegrityAnchorStore()
            root = Path(directory).resolve()
            store = JobLedgerStore(root / 'ledger', anchor)
            control = JobControlService(store)
            policy = TaskExecutionPolicy('single_link', 'materialize', '', MaterialPreferenceSnapshot(),
                'none', engine_name='local_moss_q5', requested_outputs=('text',), local_only=True)
            task = Task('gui-job-policy', '录像', str(root / 'saved.ts'), 'file', execution_policy=policy)
            job = control.create_gui_task(task=_task_payload(task), job_id=task.id,
                attempt_id='gui-attempt-policy', lineage_id='gui-lineage-policy',
                installation_id='installation-fixture', created_at='2026-10-17T08:00:00Z')
            kwargs = dict(expected_revision=job.job_revision, changed_at='2026-10-17T08:00:01Z')
            with self.assertRaises(JobLedgerError):
                store.freeze_gui_policy(job.job_id, policy=_policy_to_dict(replace(policy, local_only=False)), **kwargs)
            updated = store.freeze_gui_policy(job.job_id,
                policy=_policy_to_dict(replace(policy, engine_name=None)), **kwargs)
            from src.core.job_ledger.contracts import JobState
            queued = control.transition_gui_task(job.job_id, expected_revision=updated.job_revision,
                new_state=JobState.QUEUED, changed_at='2026-10-17T08:00:02Z')
            running = control.transition_gui_task(job.job_id, expected_revision=queued.job_revision,
                new_state=JobState.RUNNING, changed_at='2026-10-17T08:00:03Z',
                execution_policy=_policy_to_dict(replace(policy, engine_name='local_whisper_mlx')))
            reopened = JobControlService(JobLedgerStore(root / 'ledger', anchor)).show(job.job_id)
            self.assertEqual(reopened.job_revision, running.job_revision)
            self.assertEqual(reopened.state, JobState.RUNNING)
            self.assertTrue(reopened.frozen_policy['gui_execution_policy']['local_only'])

    def test_legacy_live_retry_can_tighten_policy_without_rewriting_admitted_input(self):
        from src.core.collect.preferences import MaterialPreferenceSnapshot
        from src.core.task import TaskExecutionPolicy, Task
        from src.core.task_queue_store import _policy_to_dict
        from src.core.gui_task_ledger import _task_payload
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = JobLedgerStore(root / 'ledger', FakeIntegrityAnchorStore())
            control = JobControlService(store)
            policy = TaskExecutionPolicy('single_link', 'materialize', '', MaterialPreferenceSnapshot('audio',False,False),
                'none', prefer_subtitles=False, engine_name='local_whisper_mlx', requested_outputs=('text',))
            task = Task('gui-job-old-live', '直播录像转录', str(root / 'saved.ts'), 'file', execution_policy=policy)
            admitted = _task_payload(task)
            job = control.create_gui_task(task=admitted, job_id=task.id,
                attempt_id='gui-attempt-old-live', lineage_id='gui-lineage-old-live',
                installation_id='installation-fixture', created_at='2026-10-17T08:00:00Z')
            updated = store.freeze_gui_policy(job.job_id, expected_revision=job.job_revision,
                changed_at='2026-10-17T08:00:01Z',
                policy=_policy_to_dict(replace(policy, engine_name=None, local_only=True)))
            self.assertEqual(updated.frozen_policy['gui_task'], admitted)
            self.assertTrue(updated.frozen_policy['gui_execution_policy']['local_only'])


class LiveHandoffFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        live_fixture.NativeLiveTests.setUpClass()

    def test_changed_segment_error_restores_controls_and_retry(self):
        fixture = live_fixture.NativeLiveTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        rid, media = fixture.completed_segment()
        original = media.read_bytes()
        fixture.page.transcribe = Mock(return_value=True)
        fixture.controller.call('detail', recording_id=rid, callback=fixture.page._detail)
        fixture.until(lambda: fixture.page.detail_dialog and fixture.page.detail_dialog.extract_button.isEnabled())
        dialog = fixture.page.detail_dialog
        media.write_bytes(original + b'changed')
        dialog.extract_button.click()
        fixture.until(lambda: fixture.controller.transcribe_worker is None and bool(fixture.errors))
        self.assertIn('已变化', fixture.errors[-1])
        self.assertFalse(dialog.busy)
        self.assertTrue(dialog.extract_button.isEnabled())
        fixture.page.transcribe.assert_not_called()
        media.write_bytes(original)
        dialog.extract_button.click()
        fixture.until(lambda: fixture.page.transcribe.called)
        fixture.page.transcribe.assert_called_once_with(str(media))
        self.assertEqual(media.read_bytes(), original)
