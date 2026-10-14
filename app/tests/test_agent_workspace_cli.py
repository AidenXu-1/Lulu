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

    def test_cli_save_and_gui_open_share_revision_and_content(self):
        doc=documents.snapshot(self.eid)
        result=self.invoke('documents.save',dict(document_id=self.eid,markdown='CLI 写入',title='文稿',
            expected_revision=doc['revision'],expected_hash=doc['content_hash'],request_id='cli-save'))
        self.assertEqual(self.service.open(self.eid)['document']['text'],'CLI 写入')
        self.runtime._application.assert_workspace_allowed.assert_called_once_with(COMMANDS['documents.save'][0])
        self.assertGreater(result['revision'],doc['revision'])

    def test_stale_write_and_secret_fields_fail_closed(self):
        doc=documents.snapshot(self.eid)
        with self.assertRaises(LocalTranscriptionJobError) as fail:
            self.invoke('documents.save',dict(document_id=self.eid,markdown='覆盖',title='文稿',
                expected_revision=-1,expected_hash=doc['content_hash'],request_id='cli-save'))
        self.assertEqual(fail.exception.reason_code,'WORKSPACE_REVISION_CONFLICT')
        with self.assertRaises(LocalTranscriptionJobError):
            self.invoke('providers.list',{'api_key':'must-not-be-accepted'})
        self.assertEqual(documents.snapshot(self.eid)['text'],doc['text'])

    def test_authorization_precedes_storage_and_reads_do_not_create_chats(self):
        denied=SimpleNamespace(_application=SimpleNamespace(assert_workspace_allowed=Mock(side_effect=LocalTranscriptionJobError('AUTH_REQUIRED','fixture'))))
        with self.assertRaises(LocalTranscriptionJobError):self.invoke('documents.show',{'document_id':self.eid},denied)
        self.invoke('documents.show',{'document_id':self.eid})
        self.assertEqual(self.service.conversations(self.eid),[])
        self.assertEqual(len(CAPABILITIES),len(COMMANDS))
        self.assertTrue(all(r['executable'] for r in discovery(CAPABILITIES)))

    def test_cli_and_gui_share_older_message_cursor(self):
        chat=self.service.new_conversation(self.eid,'cli-history')
        for n in range(105):self.service.store.append(chat['id'],'user',str(n))
        latest=self.invoke('conversations.show',{'conversation_id':chat['id']})
        before=latest['messages'][0]['sequence']
        older=self.invoke('conversations.show',{'conversation_id':chat['id'],'before':before})
        self.assertEqual(older['messages'],self.service.messages(chat['id'],before=before))
        self.assertEqual([m['content'] for m in older['messages']],['0','1','2','3','4'])

    def test_cli_uses_the_same_verified_backup_and_restore(self):
        chat=self.service.new_conversation(self.eid,'cli-backup')
        self.service.store.append(chat['id'],'user','恢复这个记录')
        saved=self.invoke('storage.backup',{})
        self.service.store.append(chat['id'],'user','备份后的记录')
        current=self.invoke('storage.status',{})
        runtime=SimpleNamespace(_application=SimpleNamespace(assert_workspace_allowed=Mock()),
            _authorized_job_control=lambda _:SimpleNamespace(list=lambda:[]))
        result=self.invoke('storage.restore',dict(backup_id=saved['id'],expected_fingerprint=current['fingerprint'],
            expected_backup_hash=saved['sha256']),runtime)
        self.assertEqual(result['state'],'restored')
        self.assertEqual([m['content'] for m in self.service.messages(chat['id'])],['恢复这个记录'])

    def test_cli_reads_same_handoff_and_reference_snapshot_as_gui(self):
        from src.core.agent_workspace import tools
        chat=self.service.new_conversation(self.eid,'cli-tool')
        call=dict(name='prepare_voice',id='call1',arguments={})
        result=tools.execute(call,dict(document=documents.snapshot(self.eid),references=[]))
        self.service.store.append(chat['id'],'tool',result['message'],dict(calls=[call],results=[result]),message_id='tool1')
        request=dict(conversation_id=chat['id'],message_id='tool1',call_id='call1')
        self.assertEqual(self.invoke('tools.show',request),self.service.tool_handoff(**request))
        self.assertEqual(self.invoke('documents.voice-target',dict(document_id=self.eid))['text'],result['target']['text'])

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

    def test_direct_edit_conflict_is_shared_with_gui_and_unknown_fields_are_rejected(self):
        doc=documents.snapshot(self.eid)
        args=dict(document_id=self.eid,expected_revision=doc['revision'],expected_hash=doc['content_hash'],request_id='stale-cli',
                  operations=[dict(action='append',start=0,end=0,before_hash=documents.digest(''),markdown='旧修改')])
        self.service.save(self.eid,'手动新稿',doc['title'],doc['revision'],doc['content_hash'],'new-manual')
        with self.assertRaises(LocalTranscriptionJobError) as caught:self.invoke('documents.edit',args)
        self.assertEqual(caught.exception.code,'CONFLICT')
        self.assertEqual(documents.snapshot(self.eid)['text'],'手动新稿')
        with self.assertRaises(LocalTranscriptionJobError):self.invoke('documents.edit',dict(args,approved=True))

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

    def test_skill_remote_search_and_url_import_are_closed(self):
        from unittest.mock import patch
        with patch('socket.socket.connect', side_effect=AssertionError('unexpected network')):
            with self.assertRaises(LocalTranscriptionJobError) as failure:
                self.invoke('skills.search', {'query':'writing'})
            self.assertEqual(failure.exception.reason_code, 'WORKSPACE_COMMAND_UNAVAILABLE')
            with self.assertRaises(LocalTranscriptionJobError) as failure:
                self.invoke('skills.inspect', {'source':'https://github.com/author/skill'})
            self.assertEqual(failure.exception.reason_code, 'WORKSPACE_REQUEST_INVALID')
        self.assertNotIn('WS-49', {r['capability_id'] for r in discovery(CAPABILITIES)})
