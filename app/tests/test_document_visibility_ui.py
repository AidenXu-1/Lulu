"""Real offline workspace controls against temporary fixtures; no installed Core."""
import unittest
from unittest.mock import Mock

from tests import test_agent_workspace_ui as fixtures
from src.core import library
from src.core.agent_workspace import client
from src.core.agent_workspace.service import WorkspaceService


class DocumentVisibilityUiTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AgentWorkspaceUiTests.setUpClass.__func__)
    setUp = fixtures.AgentWorkspaceUiTests.setUp
    tearDown = fixtures.AgentWorkspaceUiTests.tearDown
    tick = fixtures.AgentWorkspaceUiTests.tick
    until = fixtures.AgentWorkspaceUiTests.until
    js = fixtures.AgentWorkspaceUiTests.js

    def test_bad_selected_document_and_recovery_copy_are_visible(self):
        md = library.markdown_path(self.eid)
        original_text = md.read_text()
        WorkspaceService().view_state('selected_document', self.eid)
        md.write_bytes(b'\xffbroken')
        self.js('window.location.reload()')
        self.until(lambda: self.js('Boolean(document.querySelector("#read-only-body"))'))
        self.assertEqual(self.js('window.lulu.state.rows.length'), 1)
        self.assertIn('我们每天', self.js('document.querySelector("#read-only-body").textContent'))
        self.assertEqual(self.js('document.querySelectorAll("[contenteditable=true]").length'), 0)
        self.js('document.querySelector("#recover-document").click()')
        self.until(lambda: self.js('Boolean(document.querySelector("#editor"))'))
        copied = self.js('window.lulu.state.doc.id')
        self.assertNotEqual(copied, self.eid)
        self.assertEqual(library.load_markdown(copied), original_text)
        self.assertEqual(md.read_bytes(), b'\xffbroken')

    def test_initial_request_waits_for_preparation_and_then_loads_automatically(self):
        self.widget.preparation_pending = True
        dispatch = self.widget.dispatch = Mock(wraps=client.dispatch)
        self.js('window.location.reload()')
        self.until(lambda: bool(self.widget.preparation_requests))
        self.assertFalse(any(call.args[0] == 'bootstrap' for call in dispatch.call_args_list))
        self.widget.finish_install_preparation(True)
        self.until(lambda: self.js('Boolean(window.lulu?.ready && window.lulu.state.rows.length === 1)'))
        self.assertTrue(any(call.args[0] == 'bootstrap' for call in dispatch.call_args_list))

    def test_transient_reader_failure_has_working_retry(self):
        def failed(operation, **arguments):
            if operation == 'bootstrap': raise RuntimeError('fixture temporarily unavailable')
            return client.dispatch(operation, **arguments)
        self.widget.dispatch = failed
        self.js('window.location.reload()')
        self.until(lambda: self.js('Boolean(document.querySelector("#retry-workspace"))'))
        self.widget.dispatch = client.dispatch
        self.js('document.querySelector("#retry-workspace").click()')
        self.until(lambda: self.js('Boolean(window.lulu?.ready && window.lulu.state.rows.length === 1)'))


if __name__ == '__main__': unittest.main()
