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

    def test_old_reference_id_is_not_an_actionable_target_in_a_later_turn(self):
        runner,chat=self.runner(lambda *a,**kw:dict(text='',calls=[],usage={}))
        doc=documents.snapshot(self.eid)
        stale=dict(id='ref-from-first-turn',document_id=self.eid,revision=doc['revision'],
                   content_hash=doc['content_hash'],start=0,end=len('第一段🙂。'.encode()),
                   before_hash=documents.digest('第一段🙂。'),markdown='第一段🙂。',label_number=1)
        store=WorkspaceStore()
        store.append(chat['id'],'user','片段一：精简',{'references':[stale]})

        current=context.projection(store,chat['id'],references=[])
        self.assertNotIn(stale['id'],json.dumps(current['messages'],ensure_ascii=False))

        request=dict(conversation_id=chat['id'],document=doc,references=[],options={})
        with self.assertRaisesRegex(ValueError,'引用范围以外'):
            runner._prepare_reply(store,request,dict(text='',calls=[dict(name='propose_edits',arguments={
                'explanation':'建议','edits':[dict(target=stale['id'],action='replace',markdown='改过的内容')]} )],usage={}))

    def test_applied_selection_then_full_document_date_edit_preserves_document_fields(self):
        from src.core import library
        original='---\ntitle: YAML 标题\ncreated_at: 2026-09-01\n---\n\n> 2026-09-19 · 04:38\n\n片段一：旧内容'
        doc=documents.snapshot(self.eid)
        self.service.save(self.eid,original,doc['title'],doc['revision'],doc['content_hash'],'seed-markdown')
        doc=documents.snapshot(self.eid)
        meta_before=library._load_raw(self.eid)
        start=original.encode().index('片段一：旧内容'.encode())
        ref=self.service.reference(self.eid,original,start,start+len('片段一：旧内容'.encode()),
                                   doc['revision'],doc['content_hash'])
        ref['label_number']=1
        ref_id=ref['id']

        def generate(config,system,messages,**kwargs):
            if '片段一：精简' in messages[-1]['content']:
                return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='已整理片段',edits=[
                    dict(target=ref_id,action='replace',markdown='片段一：整理后')]))],usage={})
            serialized=json.dumps(messages,ensure_ascii=False)
            self.assertNotIn(ref_id,serialized)
            self.assertIn('历史轮次曾选中的片段',serialized)
            current=documents.snapshot(self.eid)['text']
            edited=current.replace('2026-09-19','2026-09-20')
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='日期已调整',edits=[
                dict(target='current_document',action='replace',markdown=edited)]))],usage={})

        runner,chat=self.runner(generate)
        first=runner.start(chat['id'],'date-turn-one','片段一：精简',[ref])['job_id']
        self.assertEqual(self.wait(runner,first)['state'],'succeeded')
        proposal=self.service.store.proposals(chat['id'])[-1]
        self.service.proposal_apply(proposal['id'],[proposal['operations'][0]['id']],'apply-first-edit')
        second=runner.start(chat['id'],'date-turn-two','这个日期改成9月20号')['job_id']
        self.assertEqual(self.wait(runner,second)['state'],'succeeded')
        proposal=self.service.store.proposals(chat['id'])[-1]
        self.service.proposal_apply(proposal['id'],[proposal['operations'][0]['id']],'apply-date-edit')

        final=documents.snapshot(self.eid)['text']
        self.assertEqual(final,'---\ntitle: YAML 标题\ncreated_at: 2026-09-01\n---\n\n> 2026-09-20 · 04:38\n\n片段一：整理后')
        meta_after=library._load_raw(self.eid)
        for key in ('created_at','title','source','meta'):
            self.assertEqual(meta_after.get(key),meta_before.get(key),key)

    def test_run_status_tracks_waiting_streaming_and_completion(self):
        waiting,allow_text,streaming,finish=(threading.Event() for _ in range(4))
        self.addCleanup(finish.set);self.addCleanup(allow_text.set)
        def generate(config,system,messages,**kwargs):
            kwargs['on_phase']('connecting')
            kwargs['on_phase']('waiting_first_token')
            waiting.set();allow_text.wait(3)
            kwargs['on_text']('收到的真实片段')
            streaming.set();finish.wait(3)
            return dict(text='收到的真实片段',calls=[],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'phase-status','继续改稿')['job_id']
        self.assertTrue(waiting.wait(2))
        self.assertEqual(runner.show(jid)['phase'],'waiting_first_token')
        allow_text.set()
        self.assertTrue(streaming.wait(2))
        current=runner.show(jid)
        self.assertEqual(current['phase'],'streaming')
        self.assertEqual(current['partial'],'收到的真实片段')
        finish.set()
        self.assertEqual(self.wait(runner,jid)['phase'],'complete')

    def test_run_status_distinguishes_stopping_stopped_and_error(self):
        waiting,release=(threading.Event() for _ in range(2))
        self.addCleanup(release.set)
        def generate(config,system,messages,**kwargs):
            kwargs['on_phase']('waiting_first_token')
            waiting.set();release.wait(3)
            return dict(text='本轮已停止',calls=[],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'stop-phase','先等待')["job_id"]
        self.assertTrue(waiting.wait(2))
        self.assertEqual(runner.cancel(jid)['phase'],'stopping')
        release.set()
        self.assertEqual(self.wait(runner,jid)['phase'],'stopped')

        def fail(*args,**kwargs):
            raise ValueError('隔离错误')
        runner.generate=fail
        failed=runner.start(chat['id'],'error-phase','触发错误')["job_id"]
        self.assertEqual(self.wait(runner,failed)['phase'],'error')

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

    def test_compression_preserves_full_log_and_failure_preserves_projection(self):
        runner,chat=self.runner(lambda *a,**kw:dict(text=json.dumps(dict(goals=['自然口语'],constraints=['保留专名数字'],decisions=[],pending=[]),ensure_ascii=False),calls=[],usage={}))
        store=self.service.store
        for n in range(8):store.append(chat['id'],'user' if n%2==0 else 'assistant','这是早期对话，需要保留信息。'*20)
        before=store.messages(chat['id'])
        jid=runner.start(chat['id'],'compact1',mode='compact')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        self.assertEqual(store.messages(chat['id'])[:8],before)
        checkpoint=store.get(chat['id'])['checkpoint']
        self.assertEqual(checkpoint['through_sequence'],before[3]['sequence'])
        runner.generate=lambda *a,**kw:dict(text='',calls=[],usage={})
        for n in range(8):store.append(chat['id'],'user','新的一段话。'*20)
        jid=runner.start(chat['id'],'compact2',mode='compact')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'failed')
        self.assertEqual(store.get(chat['id'])['checkpoint'],checkpoint)

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

    def test_manual_compaction_without_old_turns_leaves_visible_feedback(self):
        runner,chat=self.runner(lambda *a,**kw:self.fail('no summary request is needed'))
        jid=runner.start(chat['id'],'no-compact',mode='compact')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        self.assertIn('暂时无需压缩',self.service.store.messages(chat['id'])[-1]['content'])

    def test_unknown_capacity_and_disallowed_coding_plan_are_not_assumed(self):
        with self.assertRaises(ValueError):
            providers.save_connection(dict(provider='kimi',mode='coding',base='https://api.kimi.com/coding',protocol='anthropic',model='k3',window=262144,max_output=4096,threshold=.8,credential_id='workspace-key-fixture',label='Kimi'))

    def test_multiple_edits_are_all_published_and_appended_in_order(self):
        def generate(*a,**kw):
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='建议',edits=[
                dict(target='current_document',action='replace',markdown='正文'),
                dict(target='current_document',action='append',markdown='摘要'),
                dict(target='current_document',action='append',markdown='拆解')]))],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'send-all','改稿并总结')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        p=self.service.store.proposals(chat['id'])[0]
        self.assertEqual(len(p['operations']),3)
        self.service.proposal_apply(p['id'],[o['id'] for o in p['operations']],'apply-all')
        self.assertEqual(documents.snapshot(self.eid)['text'],'正文\n\n摘要\n\n拆解')

    def test_invalid_second_call_does_not_publish_first_proposal(self):
        def generate(*a,**kw):
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='建议',edits=[
                dict(target='current_document',action='replace',markdown='正文')])),dict(name='unknown',arguments={})],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'send-bad','改稿')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'failed')
        self.assertEqual(self.service.store.proposals(chat['id']),[])
        self.assertTrue(runner.path(jid,'response').exists())

    def test_deleted_target_keeps_generated_text_without_recreating_document(self):
        started,finish=threading.Event(),threading.Event()
        self.addCleanup(finish.set)
        def generate(*a,**kw):
            started.set();finish.wait(3)
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='建议',edits=[
                dict(target='current_document',action='replace',markdown='已经生成的建议')]))],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'deleted-target','润色')['job_id']
        self.assertTrue(started.wait(2))
        self.service.delete(self.eid);finish.set()
        result=self.wait(runner,jid)
        self.assertEqual(result['state'],'succeeded')
        self.assertIn('已经生成的建议',result['result']['text'])
        self.assertIn('最近删除',result['result']['text'])
        self.assertEqual(self.service.store.proposals(chat['id']),[])
        with self.assertRaises(FileNotFoundError):documents.snapshot(self.eid)
