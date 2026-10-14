"""Isolated content-agent product contracts, with no model service or user data."""
import json
import unittest
from tests import test_agent_workspace_conversations as fixture
from tests.test_agent_workspace_tools import reply
from src.core.agent_workspace import documents, tools
from src.core import library


class ProductTests(unittest.TestCase):
    setUp = fixture.ConversationsTests.setUp
    runner = fixture.ConversationsTests.runner
    wait = fixture.ConversationsTests.wait

    def edit(self, text='新正文\n\n1. 第一点\n2. 第二点', target='current_document', action='replace'):
        return reply('edit_document', dict(explanation='调整表达', edits=[dict(target=target, action=action, markdown=text)]),'edit1')

    def test_direct_edit_then_read_uses_saved_content_and_one_history(self):
        calls=[]
        def generate(config,system,messages,**kwargs):
            calls.append(messages)
            if len(calls)==1:
                self.assertIn('edit_document',[x['name'] for x in kwargs['tools']])
                return self.edit()
            if len(calls)==2:
                result=json.loads(messages[-1]['content'])
                self.assertTrue(result['receipt']['changed'])
                self.assertIn('第二点',result['target']['text'])
                return reply('read_targets',{},'read2')
            self.assertIn('第二点',json.loads(messages[-1]['content'])['text'])
            return dict(text='已修改并保存。',calls=[],usage={})
        runner,chat=self.runner(generate)
        result=self.wait(runner,runner.start(chat['id'],'direct','直接改好')['job_id'])
        self.assertEqual(result['state'],'succeeded',result)
        self.assertIn('2. 第二点',documents.snapshot(self.eid)['text'])
        self.assertEqual(self.service.store.proposals(chat['id']),[])
        self.assertEqual(len(self.service.history(self.eid)),1)
        runner.start(chat['id'],'direct','直接改好')
        self.assertEqual(len(calls),3)

    def test_selected_append_stays_inside_selected_range(self):
        doc=documents.snapshot(self.eid);end=len('第一段🙂。'.encode())
        ref=self.service.reference(self.eid,doc['text'],0,end,doc['revision'],doc['content_hash']);ref['label_number']=1
        replies=iter([self.edit('补充',ref['id'],'append'),reply('read_targets',{},'read2'),dict(text='完成',calls=[],usage={})])
        runner,chat=self.runner(lambda *a,**k:next(replies))
        result=self.wait(runner,runner.start(chat['id'],'append','在选段后加一句',[ref])['job_id'])
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(documents.snapshot(self.eid)['text'],'第一段🙂。\n\n补充\n\n第一段🙂。')
        self.assertEqual(self.service.messages(chat['id'])[-2]['payload']['results'][0]['text'],'第一段🙂。\n\n补充')

    def test_read_only_rejects_model_write(self):
        original=documents.snapshot(self.eid)
        def generate(config,system,messages,**kwargs):
            self.assertNotIn('edit_document',[t['name'] for t in kwargs['tools']])
            return self.edit()
        runner,chat=self.runner(generate)
        result=self.wait(runner,runner.start(chat['id'],'readonly','解释',options={'read_only':True})['job_id'])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(documents.snapshot(self.eid),original)

    def test_atomic_edit_replay_and_undo_never_overwrite_later_changes(self):
        doc=documents.snapshot(self.eid)
        ops=[dict(action='replace',start=0,end=len(doc['text'].encode()),before_hash=doc['content_hash'],markdown='修订稿')]
        result=self.service.edit_document(self.eid,doc['revision'],doc['content_hash'],ops,'edit')
        self.assertEqual(result,self.service.edit_document(self.eid,doc['revision'],doc['content_hash'],ops,'edit'))
        restored=self.service.undo_edit('edit',result['revision'],result['content_hash'],'undo')
        self.assertEqual(documents.snapshot(self.eid)['text'],doc['text'])
        self.assertEqual(restored,self.service.undo_edit('edit',result['revision'],result['content_hash'],'undo'))
        with self.assertRaises(library.RevisionConflict):self.service.undo_edit('edit',result['revision'],result['content_hash'],'undo-again')
        with self.assertRaises(library.RevisionConflict):self.service.edit_document(self.eid,doc['revision'],doc['content_hash'],[dict(ops[0],markdown='不同')],'edit')

    def test_malformed_batch_is_atomic(self):
        doc=documents.snapshot(self.eid)
        ops=[dict(action='replace',start=0,end=len(doc['text'].encode()),before_hash=doc['content_hash'],markdown='不得保存'),dict(action='replace',start=0,end=1,before_hash='wrong',markdown='x')]
        with self.assertRaises((ValueError,library.RevisionConflict)):self.service.edit_document(self.eid,doc['revision'],doc['content_hash'],ops,'bad')
        self.assertEqual(documents.snapshot(self.eid),doc)

    def test_search_read_create_are_real_and_keep_current_document(self):
        doc=documents.snapshot(self.eid);request=dict(document=doc,references=[],request_id='notes',options={})
        call=dict(name='create_document',id='create1',arguments=dict(title='学习笔记',markdown='机会成本'))
        created=tools.execute(call,request)
        found=tools.execute(dict(name='search_documents',arguments=dict(query='学习笔记')),request)
        self.assertIn(created['document_id'],[d['id'] for d in found['documents']])
        read=tools.execute(dict(name='read_document',arguments=dict(document_id=created['document_id'],offset=0)),request)
        self.assertEqual(read['text'],'机会成本');self.assertEqual(documents.snapshot(self.eid),doc)
        self.assertEqual(created,tools.execute(call,request))

    def test_archive_retains_history_and_default_open_uses_active(self):
        chat=self.service.new_conversation(self.eid,'old');self.service.store.append(chat['id'],'user','保留内容')
        self.service.archive_conversation(chat['id'])
        self.assertEqual(self.service.conversations(self.eid),[])
        self.assertEqual(self.service.conversations(self.eid,archived=True)[0]['id'],chat['id'])
        opened=self.service.open(self.eid)
        self.assertNotEqual(opened['conversation']['id'],chat['id'])
        self.service.archive_conversation(chat['id'],False)
        self.assertEqual(self.service.messages(chat['id'])[0]['content'],'保留内容')

    def test_skill_creation_loads_tools_on_demand_and_preserves_document(self):
        from tests.test_skill_distribution import install_fixture
        install_fixture('skill-creator')
        from src.core.agent_workspace import skill_manager
        before=documents.snapshot(self.eid)
        calls=[]
        def generate(config,system,messages,**kwargs):
            names={t['name'] for t in kwargs['tools']};calls.append(names)
            if len(calls)==1:
                self.assertNotIn('create_skill_draft',names)
                return reply('load_skill',{'name':'skill-creator'},'load-creator')
            self.assertIn('create_skill_draft',names)
            if len(calls)>2:
                return dict(text='草稿已保存，可继续修改或安装。',calls=[],usage={})
            return reply('create_skill_draft',dict(name='learning-review',description='复盘一段学习材料',instructions='先解释概念，再请用户举例。'),'draft')
        runner,chat=self.runner(generate)
        result=self.wait(runner,runner.start(chat['id'],'make-skill','把我的学习方法做成技能')['job_id'])
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(documents.snapshot(self.eid),before)
        pending=skill_manager.drafts()
        self.assertEqual([p['name'] for p in pending],['learning-review'])
        self.assertFalse((skill_manager.roots()[0]/'learning-review').exists())

    def test_disabled_loaded_skill_cannot_write_late_model_response(self):
        from src.core.agent_workspace import skill_manager
        before=documents.snapshot(self.eid)
        calls=[]
        def generate(config,system,messages,**kwargs):
            calls.append(1)
            if len(calls)==1:return reply('load_skill',{'name':'lulu-polish'},'load-polish')
            if len(calls)==2:
                skill_manager.configure('lulu-polish',enabled=False)
                return self.edit('不得写入')
            return dict(text='已停用，未修改。',calls=[],usage={})
        runner,chat=self.runner(generate)
        result=self.wait(runner,runner.start(chat['id'],'disable-late','润色这段')['job_id'])
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(documents.snapshot(self.eid),before)

    def test_explicit_create_and_install_finishes_in_normal_conversation(self):
        from tests.test_skill_distribution import install_fixture
        install_fixture('skill-creator')
        from src.core.agent_workspace import skill_manager
        replies=[]
        def generate(config,system,messages,**kwargs):
            replies.append(1)
            if len(replies)==1:return reply('load_skill',{'name':'skill-creator'},'load-maker')
            if len(replies)==2:return reply('create_skill_draft',dict(name='my-learning',description='我的学习方法',instructions='用一个具体例子解释概念。'),'make')
            if len(replies)==3:
                created=json.loads(messages[-1]['content'])
                return reply('install_skill',dict(name='',token=created['entries'][0]['token']),'install')
            return dict(text='已安装，可在设置中停用或卸载。',calls=[],usage={})
        runner,chat=self.runner(generate)
        result=self.wait(runner,runner.start(chat['id'],'make-and-install','把这个方法做成技能并安装')['job_id'])
        self.assertEqual(result['state'],'succeeded',result)
        self.assertTrue(next(r for r in skill_manager.listing()['entries'] if r['name']=='my-learning')['enabled'])
