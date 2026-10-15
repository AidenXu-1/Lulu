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
