"""Existing credentials are loaded explicitly; only public metadata enters the page."""
import json
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from src.config import AppConfig
from src.ui.approved_settings import ApprovedSettings


class SettingsFeishuSavedConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Feishu settings did not settle')

    def js(self, script):
        values = []
        self.page.approved.page().runJavaScript(script, values.append)
        self.until(lambda: bool(values))
        return values[0]

    def setUp(self):
        self.cached = patch('src.ui.feishu_connection.secrets.configuration_loaded', return_value=False)
        self.cached.start()
        self.addCleanup(self.cached.stop)
        reader = patch('src.ui.feishu_connection.secrets.get_config', return_value={})
        self.read = reader.start()
        self.addCleanup(reader.stop)
        self.page = ApprovedSettings(AppConfig(auto_update_enabled=False, notification_policy='never'),
                                     agent_skill_status_provider=lambda: 'ready', lazy_categories=True)
        self.page.resize(960, 760)
        self.page.show()
        self.until(lambda: self.page.approved.bridge.is_ready)
        self.page.set_category('feishu')
        self.until(lambda: self.js("!!document.querySelector('#feishu-app-id')"))

    def tearDown(self):
        if self.page.feishu_connection.worker is not None:
            self.page.feishu_connection.worker.wait(10000)
        self.page.shutdown_workers()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def click_read(self):
        self.assertTrue(self.js("!!document.querySelector('[data-action=feishu-load]')"))
        self.js("document.querySelector('[data-action=feishu-load]').click();void 0")
        self.until(lambda: self.page.feishu_connection.loaded or self.page.feishu_connection.attempted)
        self.until(lambda: not self.page.feishu_connection.busy)

    def test_existing_app_connection_loads_without_exposing_secret(self):
        self.read.return_value = {'app_id':'cli_saved_fixture', 'app_secret':'fixture-secret-never-web'}
        self.read.assert_not_called()
        self.assertIn('待检查', self.js("document.querySelector('#page-content').innerText"))
        self.click_read()
        self.until(lambda: self.js("document.querySelector('#feishu-app-id').value") == 'cli_saved_fixture')
        self.assertEqual(self.js("document.querySelector('#feishu-secret').value"), '')
        self.assertEqual(self.js("document.querySelector('#feishu-secret').placeholder"), '已保存在本机')
        self.assertNotIn('fixture-secret-never-web', self.js('JSON.stringify(state)'))
        self.js("document.querySelector('[data-action=feishu-next]').click();void 0")
        self.assertEqual(self.js('state.feishu.step'), 1)

    def test_pending_read_preserves_typed_app_id_and_secret(self):
        self.read.return_value = {'app_id':'cli_saved_fixture', 'app_secret':'saved-fixture'}
        for element,value in [('feishu-app-id','cli_typed_fixture'),('feishu-secret','typed-fixture')]:
            self.js(f"document.getElementById({json.dumps(element)}).value={json.dumps(value)};document.getElementById({json.dumps(element)}).dispatchEvent(new Event('input',{{bubbles:true}}));void 0")
        self.click_read()
        self.until(lambda: self.js('state.feishu.loaded') is True)
        self.assertEqual(self.js("document.querySelector('#feishu-app-id').value"), 'cli_typed_fixture')
        self.assertEqual(self.js("document.querySelector('#feishu-secret').value"), 'typed-fixture')

    def test_read_failure_offers_retry_without_oauth_or_overwrite(self):
        self.read.side_effect = RuntimeError('CREDENTIAL_READ_UNAVAILABLE')
        with patch.object(self.page.feishu_connection,'authorize') as authorize:
            self.click_read()
            self.until(lambda: self.js("document.querySelector('[data-action=feishu-load]').disabled") is False)
            self.assertFalse(self.page.feishu_connection.loaded)
            self.assertFalse(self.js("document.querySelector('[data-action=feishu-load]').disabled"))
            authorize.assert_not_called()
