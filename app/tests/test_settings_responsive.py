import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from src.ui.settings_page import SettingsPage
from tests.test_settings_page import _Cfg

class SettingsResponsiveTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _Cfg(Path(self.tmp.name))

    def wait(self, predicate):
        limit = time.monotonic() + 3
        while not predicate() and time.monotonic() < limit:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_skill_status_does_not_block_page_construction(self):
        calls = []
        def slow():
            calls.append(threading.get_ident())
            time.sleep(.3)
            return 'ready'
        start = time.monotonic()
        page = SettingsPage(self.cfg, agent_skill_status_provider=slow)
        try:
            self.assertLess(time.monotonic()-start, .25)
            self.wait(lambda: page.agent_skill_state == 'ready')
            self.assertNotEqual(calls[0], threading.get_ident())
        finally:
            page.shutdown_workers()
            page.close()

    def test_feishu_read_runs_once_in_background_and_preserves_user_edits(self):
        calls = []
        def slow(*args, **kwargs):
            calls.append(threading.get_ident())
            time.sleep(.3)
            return {'app_id':'saved', 'app_secret':'secret', 'refresh_token':'token'}
        with patch('src.core.secrets.get_config', side_effect=slow), patch('src.core.secrets.configuration_loaded', return_value=False):
            page = SettingsPage(self.cfg, agent_skill_status_provider=lambda:'not_installed')
            try:
                start=time.monotonic()
                page.set_category('feishu')
                panel=page.feishu_connection;panel.load_if_needed(force=True)
                self.assertLess(time.monotonic()-start,.1)
                page.feishu_connection.app_id.setText('正在编辑')
                page.feishu_connection.app_id.setModified(True)
                for _ in range(3):page.set_category('feishu')
                self.wait(lambda: page.feishu_connection.loaded)
                self.assertEqual(page.feishu_connection.app_id.text(),'正在编辑')
                self.assertEqual(page.feishu_connection.status.text(),'已保存飞书授权')
                page.set_category('general');page.set_category('feishu')
                self.assertEqual(len(calls),1)
                self.assertNotEqual(calls[0],threading.get_ident())
            finally:
                page.shutdown_workers();page.close()

    def test_failed_read_does_not_retry_on_navigation_but_explicit_retry_works(self):
        with patch('src.core.secrets.get_config', side_effect=RuntimeError('denied')) as read, patch('src.core.secrets.configuration_loaded',return_value=False):
            page = SettingsPage(self.cfg, agent_skill_status_provider=lambda:'not_installed')
            try:
                page.set_category('feishu')
                page.feishu_connection.load_if_needed(force=True)
                self.wait(lambda: not page.feishu_connection.busy)
                for _ in range(4):
                    page.set_category('general');page.set_category('feishu')
                self.assertEqual(read.call_count, 1)
                self.assertIn('连接未完成',page.feishu_connection.notice.text())
                read.side_effect = None
                read.return_value = {'app_id':'ok','app_secret':'ok'}
                page.feishu_connection.start_login()
                self.wait(lambda: page.feishu_connection.loaded)
                self.assertEqual(read.call_count, 2)
                self.assertEqual(page.feishu_connection.status.text(),'连接飞书账号')
            finally:
                page.shutdown_workers();page.close()

    def test_closing_while_reading_discards_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set();release.wait(2)
            return {'app_id':'late','app_secret':'late'}
        with patch('src.core.secrets.get_config', side_effect=slow), patch('src.core.secrets.configuration_loaded', return_value=False):
            page = SettingsPage(self.cfg, agent_skill_status_provider=lambda:'not_installed')
            page.set_category('feishu')
            page.feishu_connection.load_if_needed(force=True)
            self.wait(entered.is_set)
            page.shutdown_workers()
            release.set()
            self.wait(lambda: not page.feishu_connection.busy)
            self.app.processEvents()
            self.assertFalse(page.feishu_connection.loaded)
            self.assertEqual(page.feishu_connection.app_id.text(),'')
            page.close()
