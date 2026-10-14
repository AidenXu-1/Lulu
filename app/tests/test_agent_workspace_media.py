import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core import library
from src.core.agent_workspace.media_tasks import MediaTasks
from src.core.agent_workspace.store import now
from src.core.auth_guard import OpenProductAccessGuard
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.models import Segment, Transcript
from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_application import LocalFileTranscriptionApplicationService
from src.core.transcription_job_application import LocalTranscriptionJobApplication
from src.core.transcription_result_store import TranscriptionResultStore
from tests import test_agent_workspace
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class MediaTests(unittest.TestCase):
    def setUp(self):
        test_agent_workspace.AgentWorkspaceTests.setUp(self)
        self.chat = self.service.new_conversation(self.eid, 'chat-media')
        self.source = self.root / '一段访谈.wav'
        self.source.write_bytes(b'RIFF isolated fixture')
        self.calls = []
        support = self.root / 'support'
        support.mkdir(mode=0o700)
        layout = SharedProductStorageLayout.from_application_support_root(support)
        layout.product_root.mkdir(mode=0o700)
        self.control = JobControlService(JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore()))
        guard = OpenProductAccessGuard()
        self.application = LocalTranscriptionJobApplication(runtime_verification=dict(profile='trusted_test',
            verified=True, signature_kind='developer_id', installation_id='fixture'), authorization_guard=guard,
            model_readiness_probe=lambda engine: True, transcription_service=LocalFileTranscriptionApplicationService(
                guard, pipeline_runner=lambda *a, **k: self.pipeline(*a, **k), model_readiness_probe=lambda engine: True),
            job_store=self.control._store, result_store=TranscriptionResultStore(layout), now=now)
        self.runtime = LocalTranscriptionCliRuntime(self.application, job_control=self.control, now=now)
        self.service.runtime = self.runtime
        self.media = self.runtime.workspace_media()
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for worker in list(self.media.workers.values()):
            worker.join(4)

    def pipeline(self, source, *, engine_name, progress, is_cancelled):
        self.calls.append(Path(source).read_bytes())
        self.assertEqual(engine_name, 'local_sensevoice')
        self.assertNotEqual(Path(source), self.source)
        progress('识别中', .6)
        return Transcript('转录结果。', [Segment('转录结果。', 0, 1)], 'zh', engine_name, source, meta={})

    def plan(self, request='fixture'):
        return self.service.prepare_media(self.chat['id'], request, str(self.source), 'local_sensevoice')

    def start(self, plan=None):
        plan = plan or self.plan()
        return self.service.start_media(plan['plan_id'], plan['plan_hash'])

    def wait(self, jid):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            value = self.media.show(jid)
            if value['state'] not in {'running', 'queued', 'cancel_requested'}:
                self.join_workers()
                return value
            time.sleep(.01)
        self.fail('media task did not finish')

    def test_plan_has_no_inference_start_is_async_and_result_returns_to_origin(self):
        plan = self.plan()
        self.assertEqual(self.calls, [])
        self.assertNotIn('source_path', plan)
        first = self.start(plan)
        value = self.wait(first['job_id'])
        self.assertEqual(value['state'], 'succeeded')
        doc = self.service.snapshot(value['document_id'])
        self.assertIn('转录结果', doc['text'])
        self.assertEqual(self.service.snapshot(self.eid)['title'], '文稿')
        self.assertEqual(self.calls, [b'RIFF isolated fixture'])
        self.assertEqual(self.start(plan)['job_id'], first['job_id'])
        self.assertEqual(len(self.calls), 1)
        messages = self.service.messages(self.chat['id'])
        self.assertEqual(messages[-1]['payload']['document_id'], value['document_id'])
        other = self.service.new_conversation(self.eid, 'other-chat')
        self.assertEqual(self.service.media_tasks(other['id']), [])

    def test_changed_file_and_symlink_never_infer(self):
        plan = self.plan()
        self.source.write_bytes(b'RIFF changed content')
        self.assertEqual(self.wait(self.start(plan)['job_id'])['state'], 'failed')
        self.assertEqual(self.calls, [])
        self.source.unlink()
        self.source.symlink_to(self.root / 'missing.wav')
        with self.assertRaises(ValueError): self.plan('symlink')

    def test_wrong_engine_and_changed_plan_never_infer(self):
        with self.assertRaises(ValueError):
            self.service.prepare_media(self.chat['id'], 'x', str(self.source), 'local_whisper')
        plan = self.plan()
        with self.assertRaises(library.RevisionConflict):
            self.service.start_media(plan['plan_id'], '0' * 64)
        self.assertEqual(self.calls, [])

    def test_cancel_via_common_jobs_entry_blocks_late_result_and_document(self):
        entered, release = threading.Event(), threading.Event()
        original = self.pipeline
        def delayed(*a, **k):
            entered.set(); release.wait(3)
            return original(*a, **k)
        self.pipeline = delayed
        jid = self.start()['job_id']
        self.assertTrue(entered.wait(2))
        with patch.object(library, 'LIBRARY_DIR', self.root / 'new-location'):
            self.assertEqual(self.runtime.jobs_cancel(jid)['state'], 'cancel_requested')
        release.set()
        self.assertEqual(self.wait(jid)['state'], 'cancelled')
        self.assertEqual(len(library.list_entries()), 1)

    def test_core_restart_does_not_repeat_inference(self):
        with patch('threading.Thread.start'):
            jid = self.start()['job_id']
        # Patched threads have never started, so do not join them.
        self.media.workers.clear()
        restarted = MediaTasks(self.control, self.application, now)
        self.assertEqual(restarted.show(jid)['state'], 'recovery_required')
        self.assertFalse(restarted.show(jid)['recoverable'])
        self.assertEqual(self.calls, [])

    def test_saved_result_recovery_does_not_infer_or_duplicate_even_after_edit(self):
        actual = self.control.complete_worker
        def fail_complete(jid, **kw):
            if kw['new_state'].value == 'succeeded':
                raise OSError('interrupted after library write')
            return actual(jid, **kw)
        with patch.object(self.control, 'complete_worker', side_effect=fail_complete):
            jid = self.start()['job_id']
            value = self.wait(jid)
        self.assertEqual(value['state'], 'recovery_required')
        eid = 'transcript_' + jid
        doc = self.service.snapshot(eid)
        self.service.save(eid, '用户修改后的结果', doc['title'], doc['revision'], doc['content_hash'], 'edit-result')
        restored = self.service.recover_media(jid)
        self.assertEqual(restored['state'], 'succeeded')
        self.assertEqual(self.service.snapshot(eid)['text'], '用户修改后的结果')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(library.list_entries()), 2)

    def test_restore_is_blocked_during_media_task(self):
        backup = self.service.backup()
        with patch('threading.Thread.start'):
            self.start()
        self.media.workers.clear()
        with self.assertRaisesRegex(RuntimeError, '转录任务'):
            self.service.restore_backup(backup['id'], 'unused', 'unused')

    def test_active_task_blocks_second_start_and_unknown_job_is_rejected(self):
        first, second = self.plan(), self.plan('second')
        with patch('threading.Thread.start'):
            self.start(first)
        self.media.workers.clear()
        with self.assertRaisesRegex(RuntimeError, '本地任务'):
            self.start(second)

    def test_cli_uses_same_plan_task_cancellation_and_document(self):
        from src.agent_cli.workspace_commands import execute
        def invoke(name, arguments):
            path = self.root / 'request.json'
            path.write_text(json.dumps(arguments))
            return execute(['workspace', 'tasks', name, '--request', str(path), '--output', 'json'], self.runtime)
        plan = invoke('prepare', dict(conversation_id=self.chat['id'], request_id='cli-task',
                                     source_path=str(self.source), engine='local_sensevoice'))
        task = invoke('start', dict(plan_id=plan['plan_id'], expected_hash=plan['plan_hash']))
        done = self.wait(task['job_id'])
        self.assertEqual(invoke('show', dict(job_id=task['job_id'])), done)
        self.assertEqual(self.control.show(task['job_id']).created_by, 'agent_cli')
        self.assertEqual(self.service.media_tasks(self.chat['id']), [done])
        self.assertIn('转录结果', self.service.open(done['document_id'])['document']['text'])

    def test_complete_result_survives_lost_write_response_without_reinference(self):
        results = self.application._result_store
        original = results.write
        def lost(*args):
            original(*args)
            raise OSError('lost response after fsync')
        with patch.object(results, 'write', side_effect=lost):
            jid = self.start()['job_id']
            value = self.wait(jid)
        self.assertEqual(value['state'], 'recovery_required')
        self.assertTrue(value['recoverable'])
        self.assertEqual(self.service.recover_media(jid)['state'], 'succeeded')
        self.assertEqual(len(self.calls), 1)

    def test_damaged_pending_result_is_not_imported(self):
        with patch.object(library, 'save', side_effect=OSError('full disk')):
            jid = self.start()['job_id']
            self.assertEqual(self.wait(jid)['state'], 'recovery_required')
        result = self.application._result_store.directory / (jid + '.json')
        result.write_text('{broken')
        with self.assertRaises(RuntimeError): self.service.recover_media(jid)
        self.assertEqual(len(library.list_entries()), 1)
        self.assertEqual(self.media.show(jid)['state'], 'recovery_required')

    def test_sensevoice_compute_guard_excludes_another_owner_and_releases_on_error(self):
        from src.core.engines.sensevoice_lock import sensevoice_resource
        with sensevoice_resource(self.root):
            with self.assertRaisesRegex(RuntimeError, '另一项任务'):
                with sensevoice_resource(self.root): self.fail('second owner was admitted')
        with self.assertRaisesRegex(ValueError, 'inference'):
            with sensevoice_resource(self.root): raise ValueError('inference')
        with sensevoice_resource(self.root): pass

    def test_task_reader_only_shares_current_chat_status_and_not_file_paths(self):
        from src.core.agent_workspace.tools import execute
        jid = self.start()['job_id']
        self.wait(jid)
        result = execute(dict(id='status', name='list_tasks', arguments={}),
            dict(conversation_id=self.chat['id']), task_reader=self.media.list)
        self.assertEqual(result['tasks'][0]['state'], 'succeeded')
        self.assertNotIn(str(self.root), json.dumps(result))
        other = self.service.new_conversation(self.eid, 'another-chat')
        self.assertEqual(execute(dict(id='status', name='list_tasks', arguments={}),
            dict(conversation_id=other['id']), task_reader=self.media.list)['tasks'], [])
        with self.assertRaises(ValueError):
            execute(dict(id='status', name='list_tasks', arguments={'conversation_id': self.chat['id']}),
                    dict(conversation_id=other['id']), task_reader=self.media.list)

    def test_start_failure_does_not_leave_a_prepared_task_blocking_other_work(self):
        from src.core.agent_workspace.store import WorkspaceStore
        plan = self.plan()
        with patch.object(WorkspaceStore, 'append', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.start(plan)
        self.assertEqual(self.media.show('workspace-' + plan['plan_id'])['state'], 'failed')
        second = self.start(self.plan('second'))
        self.assertEqual(self.wait(second['job_id'])['state'], 'succeeded')

    def test_library_relocation_during_inference_keeps_result_out_of_new_library(self):
        entered, release = threading.Event(), threading.Event()
        original = self.pipeline
        def delayed(*a, **k):
            entered.set(); release.wait(3)
            return original(*a, **k)
        self.pipeline = delayed
        jid = self.start()['job_id']
        self.assertTrue(entered.wait(2))
        with patch.object(library, 'LIBRARY_DIR', self.root / 'other-library'):
            release.set()
            self.join_workers()
            self.assertFalse((self.root/'other-library'/('transcript_'+jid+'.json')).exists())
            self.assertEqual(self.control.show(jid).state.value, 'recovery_required')
        self.assertTrue(self.media.show(jid)['recoverable'])
        self.assertEqual(self.media.recover(jid)['state'], 'succeeded')
        self.assertEqual(len(self.calls), 1)

    def test_shared_job_queries_keep_workspace_task_out_of_legacy_file_queue(self):
        jid = self.start()['job_id']
        self.wait(jid)
        self.assertEqual(self.runtime.gui_jobs_list()['jobs'], [])
        self.assertEqual(self.runtime.jobs_show(jid)['state'], 'succeeded')
        self.assertTrue(self.runtime.jobs_receipt(jid)['result_ref'].startswith('lulu-result:'))

    def test_core_restart_settles_prepared_job_before_any_worker_lease_exists(self):
        from tests.test_phase5_job_ledger import _create_arguments
        plan = self.plan()
        args = _create_arguments()
        args.update(job_kind='workspace_transcription', frozen_policy=json.loads(MediaTasks.path(plan['plan_id']).read_text()))
        job = self.control._store.create_job(**args)
        restarted = MediaTasks(self.control, self.application, now)
        self.assertEqual(restarted.show(job.job_id)['state'], 'failed')
        self.assertEqual(self.calls, [])

    def test_compute_guard_is_shared_with_another_process_and_os_releases_it(self):
        import subprocess
        import sys
        from src.core.engines.sensevoice_lock import sensevoice_resource
        code = "from pathlib import Path; import sys; from src.core.engines.sensevoice_lock import sensevoice_resource\nwith sensevoice_resource(Path(sys.argv[1])):\n print('locked',flush=True)\n sys.stdin.read()"
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(RuntimeError, '另一项任务'):
                with sensevoice_resource(self.root): self.fail('another process holds the model')
        finally:
            child.terminate()
            child.communicate(timeout=3)
        with sensevoice_resource(self.root): pass
