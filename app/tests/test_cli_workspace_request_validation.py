"""Workspace request regressions: temporary documents, no models or real storage."""
import errno
from io import StringIO
import json
import os
import unittest
from unittest.mock import Mock, patch

from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.agent_cli.main import main
from src.core import library
from src.core.agent_workspace import documents
from src.core.transcription_job_application import LocalTranscriptionJobApplication
from tests import test_agent_workspace


class WorkspaceRequestValidationTests(unittest.TestCase):
    setUp = test_agent_workspace.AgentWorkspaceTests.setUp

    def invoke(self, path, command='documents.list'):
        output, errors = StringIO(), StringIO()
        runtime = LocalTranscriptionCliRuntime(Mock(spec=LocalTranscriptionJobApplication))
        code = main(['workspace', *command.split('.'), '--request', str(path), '--output', 'json'],
                    local_transcription_runtime=runtime, stdout=output, stderr=errors)
        self.assertEqual(errors.getvalue(), '')
        return code, json.loads(output.getvalue())

    def rejected(self, path, code, reason, command='documents.list'):
        with patch('src.agent_cli.workspace_commands.handle', return_value={}) as handler:
            exit_code, value = self.invoke(path, command)
        handler.assert_not_called()
        self.assertEqual(value['schema_id'], 'lulu.agent/v1')
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], code)
        self.assertEqual(value['error']['details']['reason_code'], reason)
        self.assertFalse(value['error']['retryable'])
        return exit_code, value

    def test_duplicate_document_target_never_changes_either_document(self):
        second = library.save_document('第二篇原文', '第二篇文稿')
        first_before = documents.snapshot(self.eid)
        second_before = documents.snapshot(second)
        request = dict(document_id=second, markdown='不能保存的内容', title=second_before['title'],
                       expected_revision=second_before['revision'], expected_hash=second_before['content_hash'],
                       request_id='duplicate-target')
        path = self.root/'request.json'
        path.write_text('{"document_id":'+json.dumps(self.eid)+','+json.dumps(request, ensure_ascii=False)[1:])
        before = {str(p): p.read_bytes() for p in library.LIBRARY_DIR.rglob('*') if p.is_file()}
        code, value = self.invoke(path, 'documents.save')
        self.assertEqual(code, 2)
        self.assertEqual(value['error']['details']['reason_code'], 'WORKSPACE_REQUEST_DUPLICATE_FIELD')
        self.assertEqual(documents.snapshot(self.eid), first_before)
        self.assertEqual(documents.snapshot(second), second_before)
        self.assertEqual(before, {str(p): p.read_bytes() for p in library.LIBRARY_DIR.rglob('*') if p.is_file()})

    def test_duplicate_fields_rejected_at_every_level_including_escaped_names(self):
        for raw in ('{"limit":1,"limit":1}', '{"limit":1,"\\u006cimit":2}',
                    '{"request_id":"a","request_id":"b"}',
                    '{"expected_revision":1,"expected_revision":2}',
                    '{"operations":[{"action":"append","action":"replace"}]}',
                    '{"config":{"language":"中文","language":"日语"}}'):
            with self.subTest(raw=raw):
                path = self.root/'duplicate.json'
                path.write_text(raw)
                code, value = self.rejected(path, 'INVALID_ARGUMENT', 'WORKSPACE_REQUEST_DUPLICATE_FIELD')
                self.assertEqual(code, 2)
                self.assertIn('重复字段', value['error']['message'])

    def test_deep_json_is_an_input_error_without_dispatch(self):
        path = self.root/'nested.json'
        path.write_text('{"search":'+'['*1200+'0'+']'*1200+'}')
        code, value = self.rejected(path, 'INVALID_ARGUMENT', 'WORKSPACE_REQUEST_TOO_DEEP')
        self.assertEqual(code, 2)
        self.assertIn('嵌套', value['error']['message'])

    def test_symlink_is_an_unsafe_request_without_reflecting_its_name(self):
        path = self.root/'PRIVATE_INPUT_SHOULD_NOT_LEAK.json'
        target = self.root/'target.json'
        target.write_text('{}')
        path.symlink_to(target)
        code, value = self.rejected(path, 'UNSAFE_PATH', 'WORKSPACE_REQUEST_UNSAFE')
        self.assertEqual(code, 60)
        self.assertIn('符号链接', value['error']['message'])
        self.assertNotIn('PRIVATE_INPUT_SHOULD_NOT_LEAK', json.dumps(value))
        self.assertEqual(target.read_text(), '{}')

    def test_directory_fifo_and_invalid_json_are_input_errors(self):
        fifo = self.root/'request.fifo'
        os.mkfifo(fifo)
        malformed = self.root/'malformed.json'
        malformed.write_bytes(b'{')
        invalid_utf8 = self.root/'invalid-utf8.json'
        invalid_utf8.write_bytes(b'\xff')
        large = self.root/'large.json'
        large.write_bytes(b' '*(8*1024*1024+1))
        for path in (self.root, fifo, malformed, invalid_utf8, large):
            with self.subTest(path=path.name):
                code, _ = self.rejected(path, 'INVALID_ARGUMENT', 'WORKSPACE_REQUEST_INVALID')
                self.assertEqual(code, 2)

    def test_storage_and_business_failures_keep_their_existing_classification(self):
        missing = self.root/'missing.json'
        code, _ = self.rejected(missing, 'NOT_FOUND', 'WORKSPACE_ITEM_NOT_FOUND')
        self.assertEqual(code, 20)
        path = self.root/'valid.json'
        path.write_text('{}')
        with patch('src.agent_cli.workspace_commands.os.open', side_effect=PermissionError(errno.EACCES, 'private detail')):
            code, value = self.invoke(path)
        self.assertEqual((code, value['error']['details']['reason_code']), (1, 'WORKSPACE_STORAGE_UNAVAILABLE'))
        for error, reason in ((RecursionError('business recursion'), 'WORKSPACE_REQUEST_FAILED'),
                              (OSError(errno.ELOOP, 'business symlink'), 'WORKSPACE_STORAGE_UNAVAILABLE')):
            with self.subTest(error=type(error).__name__), patch('src.agent_cli.workspace_commands.handle', side_effect=error):
                code, value = self.invoke(path)
                self.assertEqual((code, value['error']['code'], value['error']['details']['reason_code']),
                                 (1, 'INTERNAL_ERROR', reason))

    def test_rejected_request_files_release_open_descriptors(self):
        duplicate = self.root/'duplicate.json'
        duplicate.write_text('{"limit":1,"limit":2}')
        malformed = self.root/'malformed.json'
        malformed.write_text('{')
        fifo = self.root/'request.fifo'
        os.mkfifo(fifo)
        original_open = os.open
        for path in (self.root, fifo, duplicate, malformed):
            opened = []
            def record_open(*args, **kwargs):
                fd = original_open(*args, **kwargs)
                opened.append(fd)
                return fd
            with self.subTest(path=path.name), patch('src.agent_cli.workspace_commands.os.open', side_effect=record_open):
                code, value = self.invoke(path)
                self.assertNotEqual(code, 0, value)
                self.assertEqual(len(opened), 1)
                for fd in opened:
                    try:
                        os.fstat(fd)
                    except OSError as exc:
                        self.assertEqual(exc.errno, errno.EBADF)
                    else:
                        os.close(fd)
                        self.fail('Rejected request left its descriptor open')

    def test_valid_unicode_save_and_same_request_replay_still_work(self):
        before = documents.snapshot(self.eid)
        request = dict(document_id=self.eid, markdown='中文🙂正文\n\n第二段', title='临时文稿',
                       expected_revision=before['revision'], expected_hash=before['content_hash'], request_id='valid-save')
        path = self.root/'valid-save.json'
        path.write_text(json.dumps(request, ensure_ascii=False))
        code, value = self.invoke(path, 'documents.save')
        self.assertEqual(code, 0, value)
        code, replay = self.invoke(path, 'documents.save')
        self.assertEqual(code, 0, replay)
        self.assertEqual(value, replay)
        saved = documents.snapshot(self.eid)
        self.assertEqual(saved['text'], request['markdown'])
        self.assertEqual(saved['revision'], before['revision']+1)
        request.update(request_id='stale-save', markdown='旧版本请求不能覆盖新稿')
        path.write_text(json.dumps(request, ensure_ascii=False))
        code, conflict = self.invoke(path, 'documents.save')
        self.assertEqual((code, conflict['error']['code']), (21, 'CONFLICT'))
        self.assertEqual(documents.snapshot(self.eid), saved)
        path.write_text('{"search":"中文🙂"}')
        code, value = self.invoke(path)
        self.assertEqual(code, 0, value)
        self.assertEqual(value['result']['entries'][0]['id'], self.eid)
