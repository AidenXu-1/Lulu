"""Release review regressions: temporary library/ledger, fake model, no network."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests import test_agent_workspace_conversations as conversations
from tests.test_agent_workspace_tools import reply
from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core.agent_entry.task_handover import capture_terminal_tasks
from src.core.agent_workspace.application import ConversationApplication
from src.core.agent_workspace.store import now
from src.core.job_ledger.contracts import JobState, JobLedgerError
from tests.test_phase5_job_ledger import _create_arguments


class WorkspaceReviewTests(unittest.TestCase):
    setUp = conversations.ConversationsTests.setUp
    runner = conversations.ConversationsTests.runner
    wait = conversations.ConversationsTests.wait

    def capture(self, runner):
        def cli(args):
            if args[:2] == ['jobs', 'list']:
                return {'jobs': [LocalTranscriptionCliRuntime._job_summary(j) for j in runner.control.list()]}
            job = runner.control.show(args[args.index('--job-id') + 1])
            return dict(LocalTranscriptionCliRuntime._job_summary(job), partial_failures=list(job.partial_failures))
        return capture_terminal_tasks(cli)

    def test_prepared_handoffs_end_conversation_without_starting_media(self):
        runner, chat = self.runner(lambda *a, **k: reply('prepare_voice', {}))
        for n, (tool, args) in enumerate([
            ('prepare_voice', {}), ('prepare_transcription', {'url': ''}),
            ('prepare_collection', {'url': 'https://www.douyin.com/user/fixture', 'limit': 3}),
        ]):
            with self.subTest(tool=tool):
                runner.generate = lambda *a, **k: reply(tool, args)
                jid = runner.start(chat['id'], 'handoff'+str(n), '准备工作台')['job_id']
                result = self.wait(runner, jid)
                self.assertEqual(result['state'], 'succeeded')
                self.assertEqual(result['result']['state'], 'needs_input')
                self.assertEqual(result['phase'], 'awaiting_input')
                self.assertEqual(self.service.tool_handoff(chat['id'], jid+'-tools-0', 'call1')['state'], 'needs_input')
        self.assertEqual(len(self.capture(runner)), 3)
        self.assertTrue(all(j.job_kind == 'workspace_conversation' for j in runner.control.list()))

    def legacy_handoff(self):
        runner, chat = self.runner(lambda *a, **k: reply('prepare_voice', {}))
        def legacy_complete(jid, **kw):
            kw.pop('new_state')
            return runner.control.await_input_worker(jid, **kw)
        with patch.object(runner.control, 'complete_worker', side_effect=legacy_complete):
            jid = runner.start(chat['id'], 'legacy', '准备配音')['job_id']
            self.assertEqual(self.wait(runner, jid)['state'], 'needs_input')
            runner.workers[jid].join(2)
        return runner, chat, jid

    def test_restart_settles_verified_legacy_handoff_once_without_replaying(self):
        runner, chat, jid = self.legacy_handoff()
        before = runner.control.show(jid)
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'):
            self.capture(runner)
        for _ in range(2):
            reopened = ConversationApplication(runner.control, 'fixture-install', now,
                generate=lambda *a, **k: self.fail('must not resend'))
            self.assertEqual(reopened.show(jid)['state'], 'succeeded')
            self.assertEqual(reopened.show(jid)['phase'], 'awaiting_input')
        after = runner.control.show(jid)
        self.assertEqual(after.job_revision, before.job_revision + 1)
        self.assertEqual(after.result_ref, before.result_ref)
        self.assertEqual(len(self.capture(reopened)), 1)
        self.assertEqual(self.service.tool_handoff(chat['id'], jid+'-tools-0', 'call1')['action'], 'voice')

    def test_unverified_legacy_result_stays_blocked(self):
        runner, chat, jid = self.legacy_handoff()
        runner.path(jid, 'result').write_text('{}')
        before = runner.control.show(jid)
        ConversationApplication(runner.control, 'fixture-install', now,
            generate=lambda *a, **k: self.fail('must not resend'))
        self.assertEqual(runner.control.show(jid), before)
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'):
            self.capture(runner)

    def test_unpublished_legacy_handoff_stays_blocked(self):
        runner, chat, jid = self.legacy_handoff()
        with self.service.store.connect() as db:
            db.execute('DELETE FROM messages WHERE id=?', (jid+'-assistant',))
        ConversationApplication(runner.control, 'fixture-install', now)
        self.assertEqual(runner.control.show(jid).state, JobState.NEEDS_INPUT)

    def test_legacy_repair_requires_current_lease_and_original_result(self):
        runner, chat, jid = self.legacy_handoff()
        job = runner.control.show(jid)
        token = runner.control._store.current_lease_token(jid)
        for lease, result in [(replace(token, lease_epoch=token.lease_epoch+1), job.result_ref),
                              (token, 'workspace-result:sha256:'+'0'*64)]:
            with self.subTest(result=result), self.assertRaises(JobLedgerError):
                runner.control._store.transition(jid, expected_revision=job.job_revision,
                    new_state=JobState.SUCCEEDED, result_ref=result, changed_at=now(), lease_token=lease,
                    reason_code='WORKSPACE_HANDOFF_PREPARATION_COMPLETED')
        self.assertEqual(runner.control.show(jid), job)

    def test_real_pending_media_job_is_never_settled_by_workspace_repair(self):
        runner, chat = self.runner(lambda *a, **k: self.fail('must not generate'))
        control = runner.control
        job = control._store.create_job(**_create_arguments())
        queued = control._store.transition(job.job_id, expected_revision=job.job_revision,
            new_state=JobState.QUEUED, changed_at=now())
        running, token = control.claim_worker(job.job_id, expected_revision=queued.job_revision,
            worker_instance_id='fixture-media', operation_receipt=None, changed_at=now())
        waiting = control.await_input_worker(job.job_id, expected_revision=running.job_revision,
            lease_token=token, result_ref='workspace-result:sha256:'+'0'*64, changed_at=now())
        ConversationApplication(control, 'fixture-install', now)
        self.assertEqual(control.show(job.job_id), waiting)
        with self.assertRaises(JobLedgerError):
            control._store.transition(job.job_id, expected_revision=waiting.job_revision,
                new_state=JobState.SUCCEEDED, result_ref=waiting.result_ref, changed_at=now(), lease_token=token,
                reason_code='WORKSPACE_HANDOFF_PREPARATION_COMPLETED')
        with self.assertRaisesRegex(RuntimeError, 'PRODUCT_UNFINISHED_TASKS'):
            self.capture(runner)
