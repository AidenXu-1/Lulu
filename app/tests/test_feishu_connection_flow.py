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
    def test_steps_stay_open_and_footer_remains_reachable_at_small_size(self):
        self.panel.loaded_config({'app_id':'cli_test','app_secret':'fake','refresh_token':'fake'})
        for width,height in ((740,760),(480,560)):
            self.panel.resize(width,height);APP.processEvents()
            self.assertTrue(self.panel.details.isVisible())
            self.assertFalse(any('收起' in b.text() or '展开' in b.text() for b in self.panel.findChildren(QPushButton)))
            self.assertGreater(self.panel.scroll.verticalScrollBar().maximum(),0)
            self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(),0)
            self.assertLess(self.panel.login.mapTo(self.panel,self.panel.login.rect().bottomRight()).y(),height)
            for field,button in zip(self.panel.callback_fields,self.panel.callback_buttons):
                self.assertGreater(field.width(),220);self.assertGreaterEqual(button.width(),55)
    def test_panel_colors_follow_current_theme(self):
        from src.ui import theme as T
        original=T.ACTIVE.name
        try:
            for name in ('warm',):
                T.apply_theme(name);panel=FeishuConnectionPanel(self.cfg)
                self.assertIn('background:'+T.consumer_theme(name).surface,panel.styleSheet())
                self.assertIn('color:'+T.consumer_theme(name).ink,panel.styleSheet())
                panel.shutdown();panel.deleteLater()
        finally:T.apply_theme(original)

    def test_actions_have_hand_cursor_keyboard_focus_and_visible_state_feedback(self):
        for button in self.panel.findChildren(QPushButton):
            self.assertEqual(button.cursor().shape(),Qt.PointingHandCursor)
            self.assertEqual(button.focusPolicy(),Qt.StrongFocus)
            self.assertGreaterEqual(button.minimumHeight(),38)
            self.assertTrue(button.accessibleName())
        with patch.object(self.panel,'open_url') as opened:
            button=self.panel.console_button
            def pixel():
                APP.processEvents()
                return button.grab().toImage().pixelColor(8,button.height()//2).rgba()
            button.clearFocus();QTest.mouseMove(self.panel,QPoint(2,2));normal=pixel()
            QTest.mouseMove(button,button.rect().center());hover=pixel()
            QTest.mousePress(button,Qt.LeftButton);pressed=pixel()
            QTest.mouseRelease(button,Qt.LeftButton);opened.assert_called_once()
            self.assertNotEqual(normal,hover)
            self.assertNotEqual(hover,pressed)
            opened.reset_mock();button.setFocus();QTest.keyClick(button,Qt.Key_Space)
            opened.assert_called_once()
            opened.reset_mock();button.setEnabled(False)
            QTest.mouseClick(button,Qt.LeftButton);opened.assert_not_called()
            disabled=pixel();QTest.mouseMove(self.panel,QPoint(2,2));QTest.mouseMove(button,button.rect().center())
            self.assertEqual(disabled,pixel())

    def test_shortcuts_and_each_copy_have_exact_payload_and_feedback(self):
        self.panel.app_id.setText('cli_test')
        with patch.object(self.panel,'open_url') as opened,patch('src.ui.feishu_connection.QGuiApplication.clipboard') as clipboard:
            self.panel.console_button.click();self.panel.permissions_button.click();self.panel.security_button.click()
            self.assertEqual([c.args[0] for c in opened.call_args_list],[feishu.DEV_CONSOLE_URL,feishu.app_auth_url('cli_test'),feishu.safe_url('cli_test')])
            self.panel.scopes_button.click();self.assertEqual(clipboard().setText.call_args.args[0],'\n'.join(feishu.SCOPE_BUNDLE))
            for button,port in zip(self.panel.callback_buttons,feishu.OAUTH_PORTS):
                button.click();self.assertEqual(clipboard().setText.call_args.args[0],feishu.redirect_uri(port));self.assertEqual(button.text(),'已复制')
            self.panel.app_id.setText('../wrong');self.panel.permissions_button.click();self.assertEqual(opened.call_count,3)
    def test_first_connection_reads_then_uses_typed_credentials_without_second_click(self):
        self.panel.app_id.setText('cli_new');self.panel.app_id.setModified(True)
        self.panel.secret.setText('fake_new');self.panel.secret.setModified(True)
        with patch('src.ui.feishu_connection.secrets.get_config',return_value={'app_id':'cli_old','app_secret':'fake_old'}),patch('src.ui.feishu_connection.secrets.set_key_verified') as save,patch.object(self.panel,'authorize') as authorize:
            self.panel.start_login();settle(self.panel)
            self.assertEqual(authorize.call_args.args[0]['app_id'],'cli_new')
            self.assertEqual(authorize.call_args.args[0]['app_secret'],'fake_new');save.assert_called_once()
    def test_read_failure_does_not_start_authorization_or_overwrite_inputs(self):
        self.panel.app_id.setText('cli_new');self.panel.app_id.setModified(True)
        with patch('src.ui.feishu_connection.secrets.get_config',side_effect=RuntimeError('fake failure')),patch.object(self.panel,'authorize') as authorize:
            self.panel.start_login();settle(self.panel);authorize.assert_not_called()
            self.assertEqual(self.panel.app_id.text(),'cli_new');self.assertTrue(self.panel.login.isEnabled());self.assertIn('未完成',self.panel.notice.text())
    def test_busy_fields_and_return_disabled_logout_keeps_steps_open(self):
        self.panel.loaded_config({'app_id':'cli_test','app_secret':'fake','refresh_token':'fake'})
        with patch('src.ui.feishu_connection.dialogs.confirm',return_value=True),patch.object(feishu,'logout') as logout,patch('src.ui.feishu_connection.secrets.get_config',return_value={'app_id':'cli_test','app_secret':'fake'}):
            self.panel.sign_out();self.assertFalse(self.panel.app_id.isEnabled());settle(self.panel);logout.assert_called_once()
            self.assertTrue(self.panel.details.isVisible());self.assertFalse(self.panel.logout.isVisible());self.assertTrue(self.panel.app_id.isEnabled())

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
    def test_connection_goes_to_main_page_and_return_keeps_same_account_draft(self):
        d=self.dialog;d.name.setText('未保存的名称');d.url.setText('https://my.feishu.cn/base/DRAFT')
        d.connect_button.click()
        self.assertEqual((self.host.index,self.host.category),(6,'feishu'))
        self.assertFalse(d.isVisible());self.assertTrue(self.session.isRunning());self.assertIs(self.panel._return_dialog,d)
        self.panel.return_button.click();settle(d)
        self.assertTrue(d.isVisible());self.assertEqual(d.name.text(),'未保存的名称');self.assertEqual(d.url.text(),'https://my.feishu.cn/base/DRAFT')
        self.assertFalse(d._connection_suspended)
    def test_new_account_clears_previous_target_and_failure_disables_save(self):
        d=self.dialog;d.url.setText('https://my.feishu.cn/base/OLD');d.connect_button.click()
        with patch.object(saving,'account_info',return_value={'account_id':'new','name':'新账号'}):
            self.panel.return_button.click();settle(d)
        self.assertEqual(d.url.text(),'');self.assertEqual(d.account['account_id'],'new')
        d.connect_button.click()
        with patch.object(saving,'account_info',side_effect=ValueError('已退出登录')):
            self.panel.return_button.click();settle(d)
        self.assertFalse(d.save_button.isEnabled());self.assertEqual(d.account,{})

    def test_second_draft_cannot_replace_pending_return_and_cancel_clears_return(self):
        self.dialog.connect_button.click()
        other=FeishuSaveSession(self.host,self.cfg,(), '另一个草稿','single')
        try:
            other.start();settle(other.dialog);other.dialog.connect_button.click()
            self.assertIs(self.panel._return_dialog,self.dialog)
            self.assertTrue(other.dialog.isVisible())
            self.session.request_cancel()
            self.assertIsNone(self.panel._return_dialog);self.assertFalse(self.dialog._connection_suspended)
        finally:
            settle(other.dialog);other.dialog.reject()
