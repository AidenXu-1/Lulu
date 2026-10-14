import json
import unittest
from unittest.mock import patch

from tests import test_agent_workspace_conversations as conversations
from src.core.agent_workspace import context, documents, tools
from src.core.agent_workspace.service import WorkspaceService


def reply(name, arguments, cid='call1', protocol='openai'):
    call=dict(id=cid,name=name,arguments=arguments)
    if protocol=='anthropic':
        assistant=dict(role='assistant',content=[dict(type='thinking',thinking='private',signature='signed'),
            dict(type='tool_use',id=cid,name=name,input=arguments)])
    else:
        assistant=dict(role='assistant',content=None,reasoning_content='private',
            tool_calls=[dict(id=cid,type='function',function=dict(name=name,arguments=json.dumps(arguments)))])
    return dict(text='',calls=[call],usage={},assistant=assistant)


class ToolsTests(unittest.TestCase):
    setUp=conversations.ConversationsTests.setUp
    runner=conversations.ConversationsTests.runner
    wait=conversations.ConversationsTests.wait

    def test_read_then_edit_uses_complete_exchange_and_waits_for_apply(self):
        requests=[]
        def generate(config,system,messages,**kw):
            requests.append(list(messages))
            if len(requests)==1:
                return reply('read_targets',{})
            self.assertEqual(messages[-1]['role'],'tool')
            self.assertEqual(messages[-1]['tool_call_id'],'call1')
            self.assertEqual(messages[-2]['reasoning_content'],'private')
            return dict(text='',calls=[dict(name='propose_edits',arguments=dict(explanation='建议',edits=[
                dict(target='current_document',action='replace',markdown='新稿')]))],usage={})
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'read-edit','润色')['job_id']
        self.assertEqual(self.wait(runner,jid)['state'],'succeeded')
        self.assertEqual(len(requests),2)
        self.assertNotEqual(documents.snapshot(self.eid)['text'],'新稿')
        self.assertEqual([m['role'] for m in self.service.messages(chat['id'])],['user','tool','assistant'])
        projected=context.projection(self.service.store,chat['id'])
        self.assertNotIn('private',json.dumps(projected['messages']))
        self.assertTrue(all(m['role'] in {'user','assistant'} for m in projected['messages']))

    def test_anthropic_pair_preserves_signature(self):
        response=reply('read_targets',{},protocol='anthropic')
        messages=tools.exchange({'protocol':'anthropic'},response,[dict(state='completed',text='资料')])
        self.assertEqual(messages[0]['content'][0]['signature'],'signed')
        self.assertEqual(messages[1]['content'][0]['tool_use_id'],'call1')
        self.assertEqual(messages[1]['role'],'user')

    def test_voice_handoff_freezes_only_selected_fragments_in_reference_order(self):
        runner,chat=self.runner(lambda *a,**k:reply('prepare_voice',{}))
        doc=documents.snapshot(self.eid)
        text='第一段🙂。';size=len(text.encode())
        refs=[dict(id=f'ref{i}',label_number=i,document_id=self.eid,revision=doc['revision'],content_hash=doc['content_hash'],
                   start=start,end=start+size,before_hash=documents.digest(text),markdown=text)
              for i,start in [(1,size+2)]]
        jid=runner.start(chat['id'],'voice','生成配音',refs)['job_id']
        self.assertEqual(self.wait(runner,jid)['result']['state'],'needs_input')
        self.service.save(self.eid,'后来的整篇稿',doc['title'],doc['revision'],doc['content_hash'],'later')
        handoff=self.service.tool_handoff(chat['id'],jid+'-tools-0','call1')
        self.assertEqual(handoff['target']['text'],text)
        self.assertEqual(handoff['target']['reference_ids'],['ref1'])
        other=self.service.new_conversation(self.eid,'other')
        with self.assertRaises(FileNotFoundError):self.service.tool_handoff(other['id'],jid+'-tools-0','call1')
        with self.assertRaises(Exception):self.service.voice_target(self.eid,refs)

    def test_two_identical_failures_pause_and_keep_results(self):
        count=[]
        def generate(*a,**k):
            count.append(1)
            return reply('inspect_link',{'url':'file:///private/data'},cid='call'+str(len(count)))
        runner,chat=self.runner(generate)
        jid=runner.start(chat['id'],'bad-link','识别链接')['job_id']
        result=self.wait(runner,jid)
        self.assertEqual(result['result']['state'],'paused')
        self.assertEqual(len(count),2)
        self.assertEqual(len([m for m in self.service.messages(chat['id']) if m['role']=='tool']),2)

    def test_tool_loop_is_bounded_and_does_not_repeat_after_reopening(self):
        count=[]
        def generate(*a,**k):
            count.append(1)
            return reply('read_targets',{},cid='call'+str(len(count)))
        runner,chat=self.runner(generate)
        rows=self.service.store.preference('connections'); rows[0]['window']=131072; rows[0]['max_input']=100000
        self.service.store.preference('connections',rows)
        jid=runner.start(chat['id'],'loop','看看文稿')['job_id']
        result=self.wait(runner,jid)
        self.assertEqual(result['state'],'failed')
        self.assertIn('8 个',result['message'])
        self.assertEqual(len([m for m in self.service.messages(chat['id']) if m['role']=='tool']),8)
        runner.start(chat['id'],'loop','看看文稿')
        self.assertEqual(len(count),9)

    def test_batch_validation_precedes_every_read(self):
        response=reply('read_targets',{})
        response['calls'].append(dict(id='call2',name='arbitrary_file',arguments={'path':'/private/data'}))
        runner,chat=self.runner(lambda *a,**k:response)
        with patch.object(tools,'execute',side_effect=AssertionError('must not execute')):
            jid=runner.start(chat['id'],'invalid-batch','读取')['job_id']
            self.assertEqual(self.wait(runner,jid)['state'],'failed')
        self.assertFalse(any(m['role']=='tool' for m in self.service.messages(chat['id'])))

    def test_link_identification_and_release_gates_use_existing_application(self):
        douyin=tools.execute(dict(name='inspect_link',arguments={'url':'https://www.douyin.com/user/fixture'}),{})
        self.assertEqual(douyin['kind'],'homepage')
        bili=tools.execute(dict(name='prepare_collection',arguments={'url':'https://space.bilibili.com/123','limit':3}),{})
        self.assertEqual(bili['state'],'unavailable')
        with self.assertRaises(ValueError):tools.execute(dict(name='prepare_collection',arguments={'url':'https://example.com','limit':True}),{})


if __name__=='__main__':unittest.main()
