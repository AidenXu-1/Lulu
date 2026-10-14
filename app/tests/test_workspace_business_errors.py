"""Trusted workspace failures cross the same measured boundary without retry."""
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from src.core.agent_workspace import client
from src.core.agent_entry.entrypoints import CoreCommandDispatcher
from src.core.library import RevisionConflict


class WorkspaceBusinessErrorsTests(unittest.TestCase):
    def test_business_error_survives_installed_adapter(self):
        with TemporaryDirectory() as folder:
            root=Path(folder).resolve(); state=root/'state';state.mkdir(mode=0o700)
            dispatcher=CoreCommandDispatcher(product_identity={}, resource_root=root/'Resources', runtime_factory=lambda:object())
            installation=SimpleNamespace(paths=SimpleNamespace(state_root=state))
            def transport(command,args):
                return dispatcher(dict(command=command,arguments=args),_installation=installation)
            for exception in (ValueError('这段内容已变化，请重新选择'), FileNotFoundError('文稿不存在'), RevisionConflict('文稿已更新')):
                mocked=Mock(side_effect=transport)
                with self.subTest(exception=type(exception).__name__), \
                     patch.object(client.sys,'frozen',True,create=True), \
                     patch.object(client.sys,'executable','/Applications/Lulu.app/Contents/MacOS/Lulu'), \
                     patch('src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher',return_value=mocked), \
                     patch('src.core.agent_workspace.service.handle',side_effect=exception):
                    with self.assertRaisesRegex(type(exception), str(exception)):
                        client.dispatch('folders')
                    mocked.assert_called_once()

    def test_unrecognized_error_and_trust_failure_still_fail_closed(self):
        for payload in ({'error':{'code':'Unknown','message':'anything'}}, {'error':{'code':'ValueError','message':12}}, {'error':{}}, {'error':{'code':{},'message':'bad'}}):
            with patch.object(client.sys,'frozen',True,create=True), \
                 patch.object(client.sys,'executable','/Applications/Lulu.app/Contents/MacOS/Lulu'), \
                 patch('src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher',return_value=Mock(return_value=payload)):
                with self.assertRaisesRegex(RuntimeError,'回复格式无效'): client.dispatch('folders')
        with TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            dispatcher=CoreCommandDispatcher(product_identity={},resource_root=root/'Resources',runtime_factory=lambda:object())
            installation=SimpleNamespace(paths=SimpleNamespace(state_root=root))
            with patch('src.core.agent_entry.product_transaction.assert_business_available',side_effect=ValueError('安装验证拒绝')):
                with self.assertRaisesRegex(ValueError,'安装验证拒绝'):
                    dispatcher(dict(command='gui.workspace',arguments=dict(operation='folders',arguments={})),_installation=installation)
