"""Exercise existing page action boundaries without network, widgets or user config."""
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from src.ui.collection_page import CollectionPage


class HandoffTests(unittest.TestCase):
    def page(self):
        text=['']
        page=SimpleNamespace(input=SimpleNamespace(text=lambda:text[0],setText=lambda value:text.__setitem__(0,value)),
            _collect_running=lambda:False,_worker=None,_selected_mode='full',_render_collection_mode_state=Mock(),
            quick_summary=Mock(),grab_btn=Mock(),status=Mock(),_on_input_changed=Mock(),
            _current_platform=lambda:'douyin',_cover_snapshot_enabled=lambda:False,
            _start_collect=Mock(),_on_full_primary=Mock())
        page._workspace_request_limit=MethodType(CollectionPage._workspace_request_limit,page)
        page._on_grab=MethodType(CollectionPage._on_grab,page)
        return page

    def test_staging_does_not_collect_and_start_honors_exact_limit(self):
        page=self.page()
        CollectionPage.accept_workspace_request(page,'https://www.douyin.com/user/fixture',3)
        self.assertFalse(page._start_collect.called)
        self.assertEqual(page._selected_mode,'partial')
        # Even stale/full mode state cannot silently enlarge a staged request.
        page._selected_mode='full'
        CollectionPage._on_primary_action(page)
        self.assertFalse(page._on_full_primary.called)
        command=page._start_collect.call_args.args[0]
        self.assertEqual(command.limit,3)
        self.assertEqual(command.mode,'anonymous_latest')
        self.assertEqual(command.raw_input,'https://www.douyin.com/user/fixture')

    def test_changed_source_drops_stale_scope_and_busy_page_is_not_overwritten(self):
        page=self.page()
        CollectionPage.accept_workspace_request(page,'https://www.douyin.com/user/fixture',3)
        page.input.setText('https://www.douyin.com/user/other')
        self.assertIsNone(page._workspace_request_limit())
        page._collect_running=lambda:True
        with self.assertRaises(RuntimeError):CollectionPage.accept_workspace_request(page,'https://example.com',7)
        self.assertEqual(page.input.text(),'https://www.douyin.com/user/other')


if __name__=='__main__':unittest.main()
