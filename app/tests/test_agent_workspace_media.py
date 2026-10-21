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
        self.assertEqual(engine_name, 'local_sensevoice_q8')
        self.assertNotEqual(Path(source), self.source)
        progress('识别中', .6)
        return Transcript('转录结果。', [Segment('转录结果。', 0, 1)], 'zh', engine_name, source, meta={})

    def plan(self, request='fixture'):
        return self.service.prepare_media(self.chat['id'], request, str(self.source), 'local_sensevoice_q8')

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
