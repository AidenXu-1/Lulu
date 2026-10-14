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

    def test_trash_command_shift_and_one_click_select_all(self):
        self.trash_rows()
        self.until(lambda:self.js('document.getElementById("trash-text").textContent')=="正文")
        output=Path(__file__).resolve().parents[2]/"scratch/trash-actions-20260926"
        output.mkdir(exist_ok=True);self.widget.grab().save(str(output/"trash.png"))
        self.assertFalse(self.js('document.getElementById("trash-select-visible").hidden'))
        self.js('document.querySelectorAll("[data-trash]")[0].click()')
        self.js('document.querySelectorAll("[data-trash]")[2].dispatchEvent(new MouseEvent("click",{shiftKey:true,bubbles:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'),3)
        self.js('document.querySelectorAll("[data-trash]")[1].dispatchEvent(new MouseEvent("click",{metaKey:true,bubbles:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'),2)
        self.js('document.getElementById("trash-select-visible").click()')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'),4)
        self.js('(()=>{const e=document.getElementById("trash-search");e.value="文稿0";e.dispatchEvent(new Event("input"));})()')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][aria-selected=true]").length'),1)
        self.assertIn('1 项',self.js('document.getElementById("trash-count").textContent'))

    def test_purge_cancel_then_partial_failure_retains_retry_selection(self):
        self.trash_rows(2)
        self.js('document.getElementById("trash-select-visible").click();document.getElementById("trash-purge").click()')
        self.until(lambda:self.js('Boolean(document.getElementById("purge-cancel"))'))
        self.assertEqual(len(library.list_deleted()),2)
        self.js('document.getElementById("purge-cancel").click()')
        self.assertEqual(len(library.list_deleted()),2)
        token=library.list_deleted()[0]['token']; original=WorkspaceService.purge_deleted
        def partial(service,token,confirmed=False):
            if token==failed: raise ValueError('模拟文件冲突')
            return original(service,token,confirmed)
        failed=token
        with patch.object(WorkspaceService,'purge_deleted',partial):
            self.js('document.getElementById("trash-purge").click()')
            self.until(lambda:self.js('document.getElementById("dialog").open'))
            self.js('document.getElementById("purge-confirm").click()')
            self.until(lambda:self.js('document.querySelectorAll("[data-trash]").length')==1)
            self.assertEqual(self.js('document.querySelector("[data-trash][aria-selected=true]").dataset.trash'),failed)
        self.js('document.getElementById("trash-purge").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.js('document.getElementById("purge-confirm").click()')
        self.until(lambda:self.js('Boolean(document.querySelector(".trash-empty-state"))'))
        self.assertEqual(library.list_deleted(),[])

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

    def test_select_all_spans_pages_respects_folder_and_search(self):
        folder=library.create_folder('transcript','目标文件夹')
        for i in range(105):
            eid=library.save_document('内容',f'分页文稿{i:03}')
            library.set_entry_folder(eid,folder)
        library.save_document('其他','别的文件夹')
        self.widget.refresh()
        self.until(lambda:self.js('window.lulu.state.total')==107)
        self.js('document.getElementById("select-all").click()')
        self.until(lambda:self.js('window.lulu.state.selected.size')==107,seconds=30)
        self.js('(()=>{const e=document.getElementById("folder");e.value='+json.dumps(folder)+';e.dispatchEvent(new Event("change"));})()')
        self.until(lambda:self.js('window.lulu.state.total')==105)
        self.assertEqual(self.js('window.lulu.state.selected.size'),0)
        self.js('document.getElementById("select-all").click()')
        self.until(lambda:self.js('window.lulu.state.selected.size')==105)
        self.js('(()=>{const e=document.getElementById("search");e.value="分页文稿00";e.dispatchEvent(new Event("input"));})()')
        self.until(lambda:self.js('window.lulu.state.total')==10)
        self.js('document.getElementById("select-all").click()')
        self.until(lambda:self.js('window.lulu.state.selected.size')==10)
        output=Path(__file__).resolve().parents[2]/'scratch/trash-actions-20260926'
        output.mkdir(exist_ok=True);self.widget.grab().save(str(output/'select-all.png'))

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

    def test_opening_workspace_cleans_expired_only_when_enabled(self):
        from src.core.trash_cleanup import policy
        old=library.save_document('到期','待清理');library.delete(old)
        row=library.list_deleted()[0];receipt=library.LIBRARY_DIR/'.trash'/(row['token']+'.json')
        data=json.loads(receipt.read_text());data['deleted_at']='2020-01-01T00:00:00';receipt.write_text(json.dumps(data))
        self.assertEqual(len(library.list_deleted()),1)
        policy(True)
        self.widget.view.reload()
        self.until(lambda:not library.list_deleted())
        self.assertEqual(library.load_markdown(self.eid).splitlines()[0],'我们每天都在收集内容：一段访谈、一条视频。')

    def test_narrow_trash_controls_stay_visible_and_text_shortcut_is_preserved(self):
        self.widget.resize(800,700);self.trash_rows(3)
        self.js('document.getElementById("trash-search").focus()')
        self.assertFalse(self.js('(()=>{const e=new KeyboardEvent("keydown",{key:"a",metaKey:true,bubbles:true,cancelable:true});document.getElementById("trash-search").dispatchEvent(e);return e.defaultPrevented;})()'))
        for target in ['trash-select-visible','trash-auto','trash-purge','trash-restore']:
            self.assertTrue(self.js('(()=>{const r=document.getElementById('+json.dumps(target)+').getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;})()'))
        output=Path(__file__).resolve().parents[2]/'scratch/trash-actions-20260926'
        output.mkdir(exist_ok=True);self.widget.grab().save(str(output/'trash-narrow.png'))
        self.js('document.getElementById("trash-auto").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        expected_width = self.widget.view.width()/self.widget.view.zoomFactor()
        self.until(lambda:abs(self.js('innerWidth')-expected_width)<=2)
        self.assertTrue(self.js('(()=>{const r=document.getElementById("dialog").getBoundingClientRect();return r.width<=480&&r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;})()'))
