import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_agent_workspace
from src.agent_cli.workspace_commands import execute
from src.core.transcription_job_application import LocalTranscriptionJobError
from src.core.agent_workspace.contracts import COMMANDS, CAPABILITIES, discovery
from src.core.agent_workspace import documents


class WorkspaceCliTests(unittest.TestCase):
    setUp=test_agent_workspace.AgentWorkspaceTests.setUp

    def invoke(self,name,request,runtime=None):
        path=self.root/'request.json';path.write_text(json.dumps(request))
        self.runtime=runtime or SimpleNamespace(_application=SimpleNamespace(assert_workspace_allowed=Mock()))
        return execute(['workspace',*name.split('.'),'--request',str(path),'--output','json'],self.runtime)


    def test_current_cli_catalog_direct_edit_undo_and_conversation_archive(self):
        catalog=self.invoke('tools.catalog',{})
        self.assertEqual({c['command'] for c in catalog['commands']},set(COMMANDS))
        command=next(c for c in catalog['commands'] if c['command']=='documents.edit')
        self.assertEqual({f['name'] for f in command['fields'] if f['required']},
                         {'document_id','expected_revision','expected_hash','operations','request_id'})
        doc=documents.snapshot(self.eid)
        args=dict(document_id=self.eid,expected_revision=doc['revision'],expected_hash=doc['content_hash'],request_id='cli-edit',
                  operations=[dict(action='append',start=len(doc['text'].encode()),end=len(doc['text'].encode()),
                                   before_hash=documents.digest(''),markdown='## 学习笔记\n\n- 完整要点')])
        result=self.invoke('documents.edit',args)
        self.assertEqual(result,self.invoke('documents.edit',args))
        self.assertTrue(documents.snapshot(self.eid)['text'].endswith('- 完整要点'))
        self.invoke('documents.undo',dict(request_id='cli-edit',expected_revision=result['revision'],
                    expected_hash=result['content_hash'],undo_request_id='cli-undo'))
        self.assertEqual(documents.snapshot(self.eid)['text'],doc['text'])
        chat=self.service.new_conversation(self.eid,'cli-conversation')
        self.invoke('conversations.rename',dict(conversation_id=chat['id'],title='内容复盘'))
        self.invoke('conversations.archive',dict(conversation_id=chat['id'],archived=True))
        self.assertEqual(self.invoke('conversations.list',dict(document_id=self.eid)),[])
        self.assertEqual(self.invoke('conversations.list',dict(document_id=self.eid,archived=True))[0]['title'],'内容复盘')


    def test_skill_management_cli_and_chat_share_state(self):
        rows = self.invoke('skills.manage', {})['entries']
        self.assertEqual(len(rows), 6)
        self.invoke('skills.configure', dict(name='lulu-summary', enabled=False))
        self.assertNotIn('lulu-summary', [r['name'] for r in self.service.skills()['entries']])
        self.invoke('skills.uninstall', dict(name='lulu-summary'))
        self.assertFalse(next(r for r in self.service.skill_list()['entries'] if r['name']=='lulu-summary')['installed'])
        self.invoke('skills.install', dict(name='lulu-summary'))
        self.assertIn('lulu-summary', [r['name'] for r in self.invoke('skills.list', {})['entries']])
        self.assertIn('SKILL.md', self.invoke('skills.show', {'name':'lulu-summary'})['files'])
        with self.assertRaises(LocalTranscriptionJobError):
            self.invoke('skills.install', {'name':'lulu-summary', 'approved':True})
