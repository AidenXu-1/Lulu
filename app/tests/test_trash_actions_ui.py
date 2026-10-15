"""Isolated real Qt/WebEngine interactions, no user files or model calls."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_agent_workspace_ui as ui
from src.core import library
from src.core.agent_workspace.service import WorkspaceService


class TrashActionsUiTests(unittest.TestCase):
    setUpClass=ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    setUp=ui.AgentWorkspaceUiTests.setUp
    tearDown=ui.AgentWorkspaceUiTests.tearDown
    until=ui.AgentWorkspaceUiTests.until
    tick=ui.AgentWorkspaceUiTests.tick
    js=ui.AgentWorkspaceUiTests.js

    def trash_rows(self, count=4):
        for i in range(count): library.delete(library.save_document('正文',f'文稿{i}'))
        self.widget._set_kind('trash')
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]").length')==count)


    def test_auto_cleanup_control_exists_when_empty_and_can_toggle(self):
        from src.core.trash_cleanup import policy
        self.widget._set_kind('trash')
        self.until(lambda:self.js('Boolean(document.getElementById("trash-auto"))'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('Boolean(document.getElementById("auto-confirm"))'))
        self.assertTrue(self.js('document.getElementById("auto-confirm").disabled'))
        self.js('document.getElementById("auto-enabled").click()')
        self.assertIn('现有已到期',self.js('document.getElementById("dialog-body").textContent'))
        self.js('document.getElementById("auto-confirm").click()')
        self.until(lambda:policy()['enabled'])
        self.until(lambda:self.js('document.getElementById("trash-auto-state").textContent')=='7 天后')
        self.assertTrue(self.js('document.getElementById("trash-status").hidden'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.js('document.getElementById("auto-enabled").click()')
        self.js('document.getElementById("auto-confirm").click()')
        self.until(lambda:not policy()['enabled'])

    def test_auto_cleanup_cancel_and_failed_save_keep_policy_and_draft(self):
        from src.core.trash_cleanup import policy
        self.widget._set_kind('trash')
        self.until(lambda:self.js('Boolean(document.getElementById("trash-auto"))'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.js('document.getElementById("auto-enabled").click()')
        self.assertFalse(policy()['enabled'])
        self.js('document.getElementById("auto-cancel").click()')
        self.assertFalse(policy()['enabled'])
        self.until(lambda:not self.js('document.getElementById("dialog").classList.contains("auto-cleanup-modal")'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.js('document.getElementById("auto-enabled").click()')
        original = WorkspaceService.trash_policy
        def fail_save(service, enabled=None):
            if enabled is not None: raise ValueError('模拟保存失败')
            return original(service, enabled)
        with patch.object(WorkspaceService, 'trash_policy', fail_save):
            self.js('document.getElementById("auto-confirm").click()')
            self.until(lambda:not self.js('document.getElementById("auto-error").hidden'))
            self.assertFalse(policy()['enabled'])
            self.assertTrue(self.js('document.getElementById("auto-enabled").checked'))
            self.assertFalse(self.js('document.getElementById("auto-confirm").disabled'))
            self.assertFalse(self.js('document.getElementById("dialog-close").disabled'))
        self.js('document.getElementById("auto-confirm").click()')
        self.until(lambda:policy()['enabled'])
        self.until(lambda:self.js('document.getElementById("trash-auto-state").textContent')=='7 天后')


    def test_saved_cleanup_policy_survives_cleanup_failure_and_reports_it(self):
        from src.core.trash_cleanup import policy
        self.widget._set_kind('trash')
        self.until(lambda:self.js('Boolean(document.getElementById("trash-auto"))'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.js('document.getElementById("auto-enabled").click()')
        with patch.object(WorkspaceService, 'cleanup_trash', side_effect=ValueError('模拟清理失败')):
            self.js('document.getElementById("auto-confirm").click()')
            self.until(lambda:self.js('document.getElementById("trash-status").textContent')=='设置已保存，清理检查未完成，请稍后重试。')
            self.assertTrue(policy()['enabled'])
            self.assertFalse(self.js('document.getElementById("dialog").open'))
            self.assertFalse(self.js('document.getElementById("trash-status").hidden'))
            self.assertEqual(self.js('document.getElementById("trash-auto-state").textContent'),'7 天后')
            self.assertIn('清理检查未完成',self.js('document.getElementById("toast").textContent'))
