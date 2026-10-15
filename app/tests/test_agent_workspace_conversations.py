import json
import threading
import time
import unittest
from unittest.mock import patch
from src.core.agent_workspace.application import ConversationApplication
from src.core.agent_workspace import context, providers, documents
from src.core.agent_workspace.store import WorkspaceStore, now
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests import test_agent_workspace
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class ConversationsTests(unittest.TestCase):
    setUp = test_agent_workspace.AgentWorkspaceTests.setUp
    def runner(self,generate):
        store=self.service.store
        value=dict(provider='deepseek',mode='api',base='https://api.deepseek.com',protocol='openai',model='fixture',window=16384,
                   max_output=1024,threshold=.8,credential_id='workspace-key-fixture',label='fixture')
        connection=providers.save_connection(value,store)
        chat=self.service.new_conversation(self.eid,'conversation1')
        self.service.select_connection(chat['id'],connection['id'])
        control=JobControlService(JobLedgerStore(self.root/'core',FakeIntegrityAnchorStore()))
        return ConversationApplication(control,'fixture-install',now,generate=generate),chat

    def wait(self,runner,jid):
        end=time.monotonic()+5
        while time.monotonic()<end:
            r=runner.show(jid)
            if r['state'] not in {'running','queued','cancel_requested'}:return r
            time.sleep(.01)
        self.fail('conversation job did not finish')

    def test_stream_result_is_durable_and_duplicate_request_does_not_generate(self):
        calls=[]
        def generate(config,system,messages,**kwargs):
            calls.append(messages)
            kwargs['on_text']('保留原意')
            return dict(text='保留原意',calls=[],usage={'prompt_tokens':100})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'send1','润色')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        runner.start(chat['id'],'send1','润色')
        self.assertEqual(len(calls),1)
        messages=WorkspaceStore().messages(chat['id'])
        self.assertEqual([m['role'] for m in messages],['user','assistant'])
        self.assertIn('当前作用对象',context.projection(self.service.store,chat['id'])['system'])


    def test_generated_edits_wait_for_explicit_apply(self):
        def generate(*a,**kw):
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='已给出建议',edits=[dict(target='current_document',action='replace',markdown='新的正文')]))],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'send1','润色')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        self.assertNotEqual(documents.snapshot(self.eid)['text'],'新的正文')
        proposal=self.service.store.proposals(chat['id'])[0]
        self.service.proposal_apply(proposal['id'],[proposal['operations'][0]['id']],'apply1')
        self.assertEqual(documents.snapshot(self.eid)['text'],'新的正文')

    def test_cancellation_does_not_publish_late_result(self):
        started,finish=threading.Event(),threading.Event()
        def generate(*a,**kw):
            started.set();finish.wait(3)
            return dict(text='late',calls=[],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'send1','润色')['job_id']
        self.assertTrue(started.wait(2));runner.cancel(jid);finish.set()
        self.assertEqual(self.wait(runner,jid)['state'],'cancelled')
        self.assertFalse(any(m['role']=='assistant' for m in self.service.store.messages(chat['id'])))


    def test_core_restart_never_resends_unknown_cloud_call(self):
        runner,chat=self.runner(lambda *a,**kw:self.fail('must not generate'))
        with patch('threading.Thread.start'):
            jid=runner.start(chat['id'],'send1','润色')['job_id']
        runner.record_partial(jid, '已经收到的部分回复')
        reopened=ConversationApplication(runner.control,'fixture-install',now,generate=runner.generate)
        self.assertEqual(reopened.show(jid)['state'],'recovery_required')
        self.assertEqual(reopened.start(chat['id'],'send1','润色')['state'],'recovery_required')
        message=self.service.store.messages(chat['id'])[-1]
        self.assertEqual(message['payload']['partial'],'已经收到的部分回复')
        self.assertEqual(message['role'],'status')
