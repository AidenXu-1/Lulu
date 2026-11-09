"""Connection UI with memory credentials, fake web/account calls and no cloud writes."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication, QWidget, QPushButton
from PySide6.QtTest import QTest
from src.ui.feishu_connection import FeishuConnectionPanel
from src.ui.feishu_save_dialog import FeishuSaveSession
from src.ui.main_window import MainWindow
from src.core import feishu, feishu_save as saving
from src.core.feishu_storage import WorkArchiveRecord
from tests.test_feishu_unified_ui import settle

APP=QApplication.instance() or QApplication([])

class ConnectionFlowTests(unittest.TestCase):
    def setUp(self):
        self.loaded=patch('src.ui.feishu_connection.secrets.configuration_loaded',return_value=False)
        self.loaded.start();self.addCleanup(self.loaded.stop)
        self.cfg=SimpleNamespace(feishu_last_save={},save=lambda:True)
        self.panel=FeishuConnectionPanel(self.cfg);self.panel.resize(740,760);self.panel.show();APP.processEvents()
        self.addCleanup(self.close_panel)
    def close_panel(self):
        settle(self.panel);self.panel.shutdown();self.panel.close();self.panel.deleteLater();APP.processEvents()

    def test_expired_authorization_is_bound_to_the_rejected_saved_token(self):
        config={'app_id':'cli_test','app_secret':'fixture','refresh_token':'saved-fixture'}
        stored=dict(config)
        def save(_key, value):
            stored.clear();stored.update(value)
        failed=SimpleNamespace(status_code=400,json=lambda:{'error':'server_error'})
        rejected=SimpleNamespace(status_code=400,json=lambda:{'error':'invalid_grant'})
        with patch.object(feishu,'_cfg',return_value=config), patch.dict(feishu._user_cache,{},clear=True), \
                patch.object(feishu.secrets,'get_config',side_effect=lambda *a,**k:dict(stored)), \
                patch.object(feishu.secrets,'set_config',side_effect=save):
            with patch('requests.post',return_value=failed):
                with self.assertRaises(RuntimeError):
                    feishu._refresh()
            self.assertFalse(feishu.needs_reauthorization(config))
            with patch('requests.post',return_value=rejected):
                with self.assertRaisesRegex(feishu.FeishuApiError,'授权已失效'):
                    feishu._refresh()
            self.assertTrue(feishu.needs_reauthorization(config))
            self.assertFalse(feishu.needs_reauthorization(dict(config,refresh_token='new-fixture')))
            feishu._store({'access_token':'access-fixture'})
            self.assertFalse(feishu.needs_reauthorization(config))




class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg=SimpleNamespace(feishu_last_save={},save=lambda:True)
        self.account=patch.object(saving,'account_info',return_value={'account_id':'same','name':'测试账号'});self.account.start();self.addCleanup(self.account.stop)
        self.cached=patch('src.ui.feishu_connection.secrets.configuration_loaded',return_value=False);self.cached.start();self.addCleanup(self.cached.stop)
        class Host(QWidget):
            open_feishu_connection=MainWindow.open_feishu_connection
            def _switch_page(self,index):self.index=index
        self.host=Host();self.panel=FeishuConnectionPanel(self.cfg,self.host)
        self.host.settings_page=SimpleNamespace(feishu_connection=self.panel,set_category=lambda name:setattr(self.host,'category',name))
        self.session=FeishuSaveSession(self.host,self.cfg,(WorkArchiveRecord(title='作品',transcript_text='正文'),),'作品','single')
        self.session.start();self.dialog=self.session.dialog;settle(self.dialog)
    def tearDown(self):
        settle(self.dialog);self.dialog.reject();self.panel.shutdown();self.host.close();self.host.deleteLater();APP.processEvents()

