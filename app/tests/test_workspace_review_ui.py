"""Real WebEngine and isolated Core; no installed app, credentials or network."""
import json
import threading
import unittest

from tests import test_agent_workspace_ui as ui
from tests.test_agent_workspace_tools import reply
from src.core import library
from src.core.agent_workspace import documents


class WorkspaceReviewUiTests(unittest.TestCase):
    setUpClass = classmethod(ui.AgentWorkspaceUiTests.setUpClass.__func__)
    setUp = ui.AgentWorkspaceUiTests.setUp
    tearDown = ui.AgentWorkspaceUiTests.tearDown
    tick = ui.AgentWorkspaceUiTests.tick
    until = ui.AgentWorkspaceUiTests.until
    js = ui.AgentWorkspaceUiTests.js
    use_fake_dialogue = ui.AgentWorkspaceUiTests.use_fake_dialogue
    send_prompt = ui.AgentWorkspaceUiTests.send_prompt

    def queued_turn(self, *, handoff=False, external_edit=False, restore=False):
        entered, release = threading.Event(), threading.Event()
        calls = []
        reference = None
        def generate(config, system, messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('fixture was not released')
                if handoff:
                    return reply('prepare_voice', {})
                return reply('edit_document', dict(explanation='调整选段', edits=[dict(
                    target=reference['id'], action='replace', markdown='选段第一次修改。')]), 'edit-first')
            if len(calls) == 2 and external_edit:
                current = documents.snapshot(self.eid)
                service.save(self.eid, current['text']+'\n\n手工补充', current['title'],
                             current['revision'], current['content_hash'], 'manual-edit')
            if len(calls) == 3:
                return reply('edit_document', dict(explanation='继续调整选段', edits=[dict(
                    target=reference['id'], action='replace', markdown='选段第二次修改。')]), 'edit-second')
            return dict(text='本轮完成。', calls=[], usage={})
        service, connection, runner = self.use_fake_dialogue(generate)
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('Boolean(document.querySelector("#prompt"))'))
        cid = self.js('window.lulu.state.chat.id')
        service.select_connection(cid, connection['id'])
        doc = documents.snapshot(self.eid)
        size = len(doc['text'].split('。')[0].encode()) + len('。'.encode())
        reference = service.reference(self.eid, doc['text'], 0, size, doc['revision'], doc['content_hash'])
        reference['label_number'] = 1
        self.js('window.lulu.state.refs='+json.dumps([reference])+';')
        try:
            self.send_prompt('准备配音' if handoff else '改好选中片段')
            self.until(entered.is_set)
            self.until(lambda: self.js('document.querySelector("#send").getAttribute("aria-label")==="停止当前任务" && !document.querySelector("#prompt").readOnly'))
            self.js('document.querySelector("#prompt").value="再把这段写得自然些";'
                    'document.querySelector("#prompt").dispatchEvent(new Event("input"));'
                    'document.querySelector("#queue-followup").click()')
            self.until(lambda: self.js('Boolean(window.lulu.state.queuedFollowup)'))
            if restore:
                other = library.save_document('另一篇文稿', '切页检查')
                self.widget.open_entry(other)
                self.until(lambda: self.js('window.lulu.state.doc?.id') == other)
            release.set()
            if restore:
                self.until(lambda: runner.control.list()[0].state.value == 'succeeded')
                self.widget.open_entry(self.eid)
                self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
            self.until(lambda: self.js('!document.querySelector("[data-live-job]") && !document.querySelector("[data-sending]")'))
            if handoff:
                self.until(lambda: self.js('Boolean(document.querySelector("#send-queued-followup"))'))
                self.assertEqual(len(calls), 1)
                self.assertTrue(self.js('Boolean(window.lulu.state.queuedFollowup)'))
                self.assertIn('等待你完成', self.js('document.querySelector("#chat-scroll").textContent'))
                self.assertEqual(runner.control.list()[0].state.value, 'succeeded')
                # Reopening must also retain the queued prompt until explicit send.
                other = library.save_document('另一篇文稿', '交接切页检查')
                self.widget.open_entry(other)
                self.until(lambda: self.js('window.lulu.state.doc?.id') == other)
                self.widget.open_entry(self.eid)
                self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
                self.until(lambda: self.js('Boolean(document.querySelector("#send-queued-followup"))'))
                self.assertEqual(len(calls), 1)
            elif external_edit:
                self.until(lambda: self.js('document.querySelector("#toast").textContent.includes("正文已变化")'))
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.js('document.querySelector("#prompt").value'), '再把这段写得自然些')
                self.assertTrue(self.js('window.lulu.state.outbox === null'))
                self.assertTrue(documents.snapshot(self.eid)['text'].endswith('手工补充'))
            else:
                self.until(lambda: self.js('window.lulu.state.doc.text.includes("选段第二次修改。")'))
                self.until(lambda: len(runner.control.list()) == 2 and all(j.state.value == 'succeeded' for j in runner.control.list()))
                self.assertEqual(len(calls), 4)
                self.assertEqual(documents.snapshot(self.eid)['text'], '选段第二次修改。'+doc['text'].encode()[size:].decode())
                self.assertEqual(self.js('document.querySelector("#prompt").value'), '')
                sent = [m for m in service.messages(cid) if m['role'] == 'user'][-1]['payload']['references']
                self.assertEqual(sent[0]['id'], reference['id'])
                self.assertEqual(sent[0]['markdown'], '选段第一次修改。')
        finally:
            release.set()
            for worker in runner.workers.values():
                worker.join(3)

    def test_queued_selected_followup_uses_direct_edit_receipt(self):
        self.queued_turn()

    def test_queued_followup_keeps_manual_edit_conflict_and_text(self):
        self.queued_turn(external_edit=True)

    def test_handoff_keeps_queue_for_explicit_user_action(self):
        self.queued_turn(handoff=True)

    def test_restored_queued_selection_uses_completed_edit_receipt(self):
        self.queued_turn(restore=True)
