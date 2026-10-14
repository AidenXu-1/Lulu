"""Text-only models cannot impersonate structured tools, even with JSON output."""
import json
import unittest
from tests import test_agent_workspace_conversations as fixture
from src.core.agent_workspace import context, documents


class TextModelTests(unittest.TestCase):
    setUp = fixture.ConversationsTests.setUp
    wait = fixture.ConversationsTests.wait

    def runner(self, generate):
        runner, chat = fixture.ConversationsTests.runner(self, generate)
        rows = self.service.store.preference('connections')
        rows[0]['tools_supported'] = False
        self.service.store.preference('connections', rows)
        return runner, chat

    def test_json_edit_remains_text_without_execution(self):
        def generate(config, system, messages, **kwargs):
            self.assertFalse(config['tools_supported'])
            self.assertEqual(kwargs['tools'], [])
            self.assertIn('不能执行工具', system)
            self.assertIn('仅支持聊天文字', system)
            self.assertEqual(system, context.projection(self.service.store, chat['id'])['system'])
            return dict(text=json.dumps(dict(explanation='建议', edits=[dict(
                target='current_document', action='replace', markdown='改好的正文')]), ensure_ascii=False), calls=[], usage={})
        runner, chat = self.runner(generate)
        job = runner.start(chat['id'], 'text-edit', '润色')['job_id']
        self.assertEqual(self.wait(runner, job)['state'], 'succeeded')
        self.assertNotEqual(documents.snapshot(self.eid)['text'], '改好的正文')
        self.assertEqual(self.service.store.proposals(chat['id']), [])

    def test_normal_json_discussion_remains_text(self):
        reply = '{"主题":"如何表达","例子":["一句话"]}'
        runner, chat = self.runner(lambda *a, **k: dict(text=reply, calls=[], usage={}))
        job = runner.start(chat['id'], 'json-chat', '给我一个 JSON 数据例子')['job_id']
        self.assertEqual(self.wait(runner, job)['state'], 'succeeded')
        self.assertEqual(self.service.store.messages(chat['id'])[-1]['content'], reply)
        self.assertEqual(self.service.store.proposals(chat['id']), [])

    def test_unadvertised_tool_calls_cannot_bypass_text_mode(self):
        runner, chat = self.runner(lambda *a, **k: dict(text='', calls=[dict(
            name='propose_edits', arguments=dict(explanation='建议', edits=[dict(
                target='current_document', action='replace', markdown='不应发布')]))], usage={}))
        job = runner.start(chat['id'], 'unexpected-tool', '润色')['job_id']
        self.assertEqual(self.wait(runner, job)['state'], 'failed')
        self.assertEqual(self.service.store.proposals(chat['id']), [])

    def test_bad_target_in_text_edits_keeps_original_and_raw_reply(self):
        reply = json.dumps(dict(explanation='建议', edits=[dict(target='unknown-reference', action='replace', markdown='别处')]))
        original = documents.snapshot(self.eid)
        runner, chat = self.runner(lambda *a, **k: dict(text=reply, calls=[], usage={}))
        job = runner.start(chat['id'], 'wrong-target', '润色')['job_id']
        self.assertEqual(self.wait(runner, job)['state'], 'succeeded')
        self.assertEqual(documents.snapshot(self.eid), original)
        self.assertEqual(json.loads(runner.path(job, 'response').read_text())['text'], reply)
