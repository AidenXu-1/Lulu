"""Conversation decisions and Skills, isolated files and fake model responses."""
import json
import threading
import unittest
from unittest.mock import patch
from tests import test_agent_workspace_conversations as fixture
from src.core.agent_workspace import documents, skills, context, tools


class DialogueTests(unittest.TestCase):
    setUp = fixture.ConversationsTests.setUp
    runner = fixture.ConversationsTests.runner
    wait = fixture.ConversationsTests.wait

    def propose(self, runner, chat, text='分析这篇文稿', key='proposal'):
        runner.generate = lambda *a,**k: dict(text='已整理分析。',usage={},calls=[dict(name='propose_edits',arguments=dict(
            explanation='保留原文，在末尾追加。', edits=[dict(target='current_document',action='append',markdown='## 逐段拆解\n\n1. 第一项完整分析\n2. 第二项完整分析\n\n- 可复用要点')]))])
        result = self.wait(runner,runner.start(chat['id'],key,text)['job_id'])
        self.assertEqual(result['state'],'succeeded',result)
        return self.service.store.proposals(chat['id'])[-1]

    def decide(self, runner, p, decision='apply', ids=None, text_mode=False):
        value=dict(proposal_id=p['id'],decision=decision,operation_ids=ids or [o['id'] for o in p['operations'] if o['state']=='pending'])
        runner.generate=lambda *a,**k: dict(text=json.dumps({'decision':value}) if text_mode else '',usage={},
            calls=[] if text_mode else [dict(name='resolve_proposal',arguments=value)])

    def test_natural_confirmation_commits_exact_preview_and_replay_is_once(self):
        runner,chat=self.runner(None)
        before=documents.snapshot(self.eid)['text']
        p=self.propose(runner,chat)
        self.assertEqual(documents.snapshot(self.eid)['text'],before)
        self.decide(runner,p)
        r=self.wait(runner,runner.start(chat['id'],'yes','可以，按这个来')['job_id'])
        self.assertEqual(r['state'],'succeeded',r)
        expected=before+'\n\n'+p['operations'][0]['markdown']
        self.assertEqual(documents.snapshot(self.eid)['text'],expected)
        self.assertEqual(r['result']['decision_receipt']['receipt']['content_hash'],documents.digest(expected))
        runner.generate=lambda *a,**k:self.fail('must not regenerate a duplicate request')
        runner.start(chat['id'],'yes','可以，按这个来')
        self.assertEqual(documents.snapshot(self.eid)['text'],expected)
        self.assertIn('已按你的回复写入',self.service.store.messages(chat['id'])[-1]['content'])

    def test_discussion_leaves_proposal_pending_and_revision_supersedes_it(self):
        runner,chat=self.runner(None); before=documents.snapshot(self.eid)
        p=self.propose(runner,chat)
        runner.generate=lambda *a,**k:dict(text='第二项用于解释原因。',calls=[],usage={})
        self.wait(runner,runner.start(chat['id'],'question','可以解释一下第二项吗，先别写入')['job_id'])
        self.assertEqual(documents.snapshot(self.eid),before)
        self.assertEqual(self.service.store.proposal(p['id'])['state'],'pending')
        newer=self.propose(runner,chat,'再简短一点','revised')
        self.assertEqual(self.service.store.proposal(p['id'])['state'],'superseded')
        self.decide(runner,newer)
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'approve-revised','这版可以')['job_id'])['state'],'succeeded')
        self.assertEqual(len(self.service.store.messages(chat['id'])),8)

    def test_partial_approval_and_discard_preserve_remaining_content(self):
        runner,chat=self.runner(None); doc=documents.snapshot(self.eid)
        p=self.service.proposal_plan(chat['id'],'parts',doc['revision'],doc['content_hash'],[
            dict(id='one',action='append',start=len(doc['text'].encode()),end=len(doc['text'].encode()),before_hash=documents.digest(''),markdown='第一段'),
            dict(id='two',action='append',start=len(doc['text'].encode()),end=len(doc['text'].encode()),before_hash=documents.digest(''),markdown='第二段')])
        self.decide(runner,p,ids=['two'])
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'part','只用第二段')['job_id'])['state'],'succeeded')
        self.assertEqual(documents.snapshot(self.eid)['text'],doc['text']+'\n\n第二段')
        p=self.service.store.proposal(p['id']);self.decide(runner,p,'discard',ids=['one'])
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'drop','第一段不要了')['job_id'])['state'],'succeeded')
        self.assertEqual(documents.snapshot(self.eid)['text'],doc['text']+'\n\n第二段')

    def test_stale_cross_conversation_and_same_turn_self_approval_rejected(self):
        runner,chat=self.runner(None); p=self.propose(runner,chat)
        other=self.service.new_conversation(self.eid,'other')
        self.service.select_connection(other['id'],self.service.store.get(chat['id'])['connection_id'])
        self.decide(runner,p)
        self.assertEqual(self.wait(runner,runner.start(other['id'],'cross','可以')['job_id'])['state'],'failed')
        doc=documents.snapshot(self.eid)
        documents.save(self.eid,doc['text']+'手写',doc['title'],doc['revision'],doc['content_hash'],'manual')
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'stale','可以')['job_id'])['state'],'failed')
        self.assertTrue(documents.snapshot(self.eid)['text'].endswith('手写'))
        runner.generate=lambda *a,**k:dict(text='',usage={},calls=[
            dict(name='propose_edits',arguments={}),dict(name='resolve_proposal',arguments={})])
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'self','改好就给我看')['job_id'])['state'],'failed')

    def test_cancelled_confirmation_cannot_write(self):
        runner,chat=self.runner(None);p=self.propose(runner,chat);self.decide(runner,p)
        original=runner.generate;started,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def blocked(*a,**k):
            started.set();release.wait(3);return original(*a,**k)
        runner.generate=blocked
        doc=documents.snapshot(self.eid)
        jid=runner.start(chat['id'],'cancel-confirm','可以')['job_id']
        self.assertTrue(started.wait(2));runner.cancel(jid);release.set()
        self.assertEqual(self.wait(runner,jid)['state'],'cancelled')
        self.assertEqual(documents.snapshot(self.eid),doc)

    def test_followup_keeps_fragment_scope_and_old_preview_in_context(self):
        runner,chat=self.runner(None);doc=documents.snapshot(self.eid)
        end=len('第一段🙂。'.encode())
        ref=self.service.reference(self.eid,doc['text'],0,end,doc['revision'],doc['content_hash'])
        ref['label_number']=1
        seen=[]
        def generate(config,system,messages,**kwargs):
            seen.append((system,messages))
            return dict(text='',usage={},calls=[dict(name='propose_edits',arguments=dict(explanation='只改选段。',
                edits=[dict(target=ref['id'],action='replace',markdown='初稿' if len(seen)==1 else '修订稿')]))])
        runner.generate=generate
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'fragment1','片段一：精简',[ref])['job_id'])['state'],'succeeded')
        r=self.wait(runner,runner.start(chat['id'],'fragment2','片段一：再自然一点')['job_id'])
        self.assertEqual(r['state'],'succeeded',r)
        self.assertIn('初稿',json.dumps(seen[1][1],ensure_ascii=False))
        revised=self.service.store.proposals(chat['id'])[-1]
        self.assertEqual(revised['operations'][0]['end'],end)
        self.decide(runner,revised)
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'fragment-yes','可以')['job_id'])['state'],'succeeded')
        self.assertEqual(documents.snapshot(self.eid)['text'],'修订稿\n\n第一段🙂。')

    def test_text_only_confirmation_cannot_execute_json(self):
        runner,chat=self.runner(None);p=self.propose(runner,chat)
        rows=self.service.store.preference('connections');rows[0]['tools_supported']=False
        self.service.store.preference('connections',rows)
        self.decide(runner,p,text_mode=True)
        r=self.wait(runner,runner.start(chat['id'],'plain-yes','可以')['job_id'])
        self.assertEqual(r['state'],'succeeded',r)
        self.assertNotIn('第一项完整分析',documents.snapshot(self.eid)['text'])

    def make_skill(self, disabled=False):
        root=skills.skill_root()/'analysis';root.mkdir(parents=True)
        (root/'SKILL.md').write_text('---\nname: analysis\ndescription: 拆解文稿结构\ndisable-model-invocation: '+str(disabled).lower()+'\n---\n先核对每一项的原句依据。\n参阅 [规则](references/rules.md)。')
        (root/'references').mkdir();(root/'references/rules.md').write_text('区分事实和推断。')
        return root

    def test_skill_discovery_on_demand_and_explicit_text_model(self):
        root=self.make_skill();runner,chat=self.runner(None)
        projection=context.projection(self.service.store,chat['id'])
        self.assertIn('拆解文稿结构',projection['system']);self.assertNotIn('先核对每一项',projection['system'])
        calls=[]
        def generate(config,system,messages,**kwargs):
            calls.append(messages)
            if len(calls)==1:
                call=dict(id='skill-load',name='load_skill',arguments={'name':'analysis'})
                return dict(text='',usage={},calls=[call],assistant=dict(role='assistant',content=None,tool_calls=[dict(id=call['id'],type='function',function=dict(name=call['name'],arguments=json.dumps(call['arguments'])))]))
            self.assertIn('先核对每一项',json.dumps(messages,ensure_ascii=False))
            return dict(text='我会依据原句分析。',calls=[],usage={})
        runner.generate=generate
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'auto-skill','拆解文稿')['job_id'])['state'],'succeeded')
        self.assertTrue(any(m['content']=='已读取 Skill：analysis' for m in self.service.store.messages(chat['id'])))
        rows=self.service.store.preference('connections');rows[0]['tools_supported']=False;self.service.store.preference('connections',rows)
        runner.generate=lambda c,s,m,**k:dict(text='按指定技能处理。' if '先核对每一项' in json.dumps(m,ensure_ascii=False) else 'missing',calls=[],usage={})
        self.assertEqual(self.wait(runner,runner.start(chat['id'],'manual-skill','/analysis 再拆解')['job_id'])['result']['text'],'按指定技能处理。')

    def test_skill_reference_changed_file_manual_only_and_path_boundaries(self):
        root=self.make_skill(disabled=True);entries=skills.catalog()['entries']
        with self.assertRaises(ValueError):skills.load('analysis',entries)
        skills.load('analysis',entries,explicit_name='analysis')
        for rel in ['../outside.md','/tmp/outside.md','script.py','references/rules.md']:
            with self.assertRaises(ValueError):skills.load('analysis',entries,explicit_name='analysis',reference=rel)
        loaded=skills.load('analysis',entries,explicit_name='analysis',reference='references/rules.md',loaded=['analysis'])
        self.assertEqual(loaded['skill_content'],'区分事实和推断。')
        (root/'references/link.md').symlink_to(root/'SKILL.md')
        with self.assertRaises(ValueError):skills.load('analysis',entries,explicit_name='analysis',reference='references/link.md',loaded=['analysis'])
        (root/'SKILL.md').write_text('changed')
        with self.assertRaises(ValueError):skills.load('analysis',entries,explicit_name='analysis')
        self.assertTrue(skills.catalog()['issues'])

    def test_thread_rename_and_draft_isolation(self):
        runner,chat=self.runner(None);other=self.service.new_conversation(self.eid,'separate')
        self.service.save_draft(chat['id'],{'text':'尚未发送','references':[]},0)
        self.service.rename_conversation(chat['id'],'结构分析')
        self.assertEqual(self.service.store.get(chat['id'])['title'],'结构分析')
        self.assertEqual(self.service.store.get(other['id'])['draft'],{})
        self.assertEqual(self.service.store.messages(other['id']),[])
