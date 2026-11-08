"""A slow Core reply must survive navigation without duplicate reads or false failure."""
from threading import Event
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.ui.approved_settings import ApprovedSettings


class SettingsSkillNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, condition):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail('settings navigation did not settle')

    def js(self, script):
        replies = []
        self.page.approved.page().runJavaScript(script, replies.append)
        self.until(lambda: bool(replies))
        return replies[0]

    def navigate(self, category):
        self.js(f"document.querySelector('[data-action=navigate][data-id={category}]').click();void 0")
        self.until(lambda: self.page.current_category == category)

    def exercise(self, *, return_while_reading=False, programmatic=False):
        started, release = Event(), Event()
        calls = []

        def read(operation, **arguments):
            self.assertEqual(operation, 'skill_list')
            calls.append(operation)
            started.set()
            release.wait(10)
            return {'entries': [dict(name='qa-skill', title='Delayed skill result', version='1',
                                     origin='system', installed=True, enabled=True)]}

        with patch('src.core.agent_workspace.client.dispatch', side_effect=read):
            self.page = ApprovedSettings(AppConfig(auto_update_enabled=False, notification_policy='never'),
                                         agent_skill_status_provider=lambda: 'ready', lazy_categories=True)
            try:
                self.page.resize(960, 760)
                self.page.show()
                self.until(lambda: self.page.approved.bridge.is_ready)
                self.until(lambda: self.js("document.querySelector('#page-title').textContent") == '通用')
                if programmatic:
                    self.page.set_category('skills')
                else:
                    self.navigate('skills')
                self.until(started.is_set)
                if return_while_reading:
                    self.navigate('about')
                    self.navigate('skills')
                # Flush bridge events while the first real worker remains blocked.
                for _ in range(8):
                    self.js('document.readyState')
                self.assertNotIn('暂时没读到技能', self.js("document.querySelector('#skill-results').textContent"))
                self.assertEqual(calls, ['skill_list'])
                release.set()
                self.until(lambda: self.page._skill_worker is None)
                self.until(lambda: 'Delayed skill result' in self.js("document.querySelector('#skill-results').textContent"))
                self.assertEqual(calls, ['skill_list'])
            finally:
                release.set()
                if self.page._skill_worker is not None:
                    self.page._skill_worker.wait(10000)
                self.page.shutdown_workers()
                self.page.close()
                self.page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.app.processEvents()

    def test_click_skill_category_reads_once_and_displays_result(self):
        self.exercise()

    def test_return_to_skill_category_preserves_pending_read(self):
        self.exercise(return_while_reading=True)

    def test_programmatic_category_navigation_still_loads_skills(self):
        self.exercise(programmatic=True)
