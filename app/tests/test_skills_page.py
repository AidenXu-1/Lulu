"""Real Qt interactions with a temporary Core, no model or production data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from src.ui.skills_page import SkillsPage
from src.ui.consumer_switch import ConsumerSwitch
from src.core.agent_workspace import skills
from src.core.agent_workspace.service import handle


class SkillsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        p = patch.object(skills, 'skill_root', return_value=self.root/'skills')
        p.start(); self.addCleanup(p.stop)
        self.page = SkillsPage(dispatch=lambda op,**kw:handle(op,kw))
        self.page.resize(800,700)
        self.page.show()
        self.addCleanup(self.page.close)
        from PySide6.QtCore import QThreadPool
        self.addCleanup(lambda: QThreadPool.globalInstance().waitForDone(10000))
        self.page.refresh();self.wait()

    def wait(self):
        deadline=time.monotonic()+5
        while self.page.busy and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.01)
        self.assertFalse(self.page.busy,self.page.status.text())
        self.assertNotIn('未完成：',self.page.status.text())


    def test_switch_preserves_files_and_uninstall_and_restore_are_explicit(self):
        self.page.search.setText('智能摘要')
        def toggle():
            return self.page.scroll.widget().findChildren(ConsumerSwitch)[0]
        def action(label):
            return next(button for button in self.page.scroll.widget().findChildren(QPushButton)
                        if button.text() == label)
        path = self.root/'skills/lulu-summary'
        original = (path/'SKILL.md').read_bytes()
        QTest.mouseClick(toggle(), Qt.LeftButton)
        self.assertTrue(toggle().isChecked())
        self.wait()
        self.assertFalse(toggle().isChecked())
        self.assertEqual((path/'SKILL.md').read_bytes(), original)
        with patch('src.ui.dialogs.confirm', return_value=False):
            action('卸载').click()
        self.assertTrue(path.is_dir())
        with patch('src.ui.dialogs.confirm', return_value=True):
            action('卸载').click()
        self.wait()
        self.assertFalse(path.exists())
        action('恢复').click()
        self.wait()
        self.assertTrue(path.is_dir())
        self.assertEqual((path/'SKILL.md').read_bytes(), original)


    def test_slow_install_waits_for_confirmed_state_and_survives_filter(self):
        import threading
        from PySide6.QtWidgets import QLabel, QProgressBar
        self.page.change_installation(next(r for r in self.page.entries if r['name']=='lulu-summary'),False)
        self.wait()
        dispatch = self.page.dispatch
        started, release = threading.Event(), threading.Event()
        def delayed(operation, **arguments):
            if operation == 'skill_install':
                started.set()
                if not release.wait(5): raise ValueError('test timeout')
            return dispatch(operation, **arguments)
        self.page.dispatch = delayed
        self.page.search.setText('智能摘要')
        row = next(r for r in self.page.entries if r['name']=='lulu-summary')
        try:
            self.page.change_installation(row,True)
            self.assertTrue(started.wait(1))
            self.page.search.setText(''); self.page.search.setText('智能摘要')
            self.assertFalse(self.page.scroll.widget().findChildren(ConsumerSwitch))
            restore = next(button for button in self.page.scroll.widget().findChildren(QPushButton)
                           if button.text() == '恢复')
            self.assertFalse(restore.isEnabled())
            self.assertTrue(self.page.scroll.widget().findChildren(QProgressBar))
            self.assertIn('正在安装',self.page.feedback['lulu-summary']['phase'])
        finally:
            release.set()
        self.wait()
        self.assertTrue(self.page.scroll.widget().findChildren(ConsumerSwitch)[0].isChecked())
        self.assertEqual(self.page.feedback['lulu-summary']['phase'],'已安装，可以使用')
        self.assertFalse(self.page.scroll.widget().findChildren(QProgressBar))


    def test_local_skill_file_preserves_references_through_preview_and_install(self):
        folder = self.root/'local-skill'
        folder.mkdir()
        (folder/'SKILL.md').write_text('---\nname: local-example\ndescription: 本地测试技能\n---\n\n保留事实。')
        (folder/'references').mkdir()
        (folder/'references/rules.md').write_text('完整参考')
        with patch.object(self.page, 'show_pending'):
            self.page.inspect(str(folder/'SKILL.md'))
            self.wait()
        self.assertFalse((self.root/'skills/local-example').exists())
        self.assertEqual(len(self.page.pending), 1)
        self.page.run('skill_install', lambda _: self.page.refresh(), token=self.page.pending[0]['token'])
        self.wait()
        self.assertEqual((self.root/'skills/local-example/references/rules.md').read_text(), '完整参考')
        self.assertTrue(next(row for row in self.page.entries if row['name']=='local-example')['installed'])

    def test_remote_or_command_input_never_reaches_core(self):
        with patch.object(self.page, 'dispatch') as dispatch:
            for source in ('https://github.com/a/b', 'npx skills add example', 'file:///tmp/skill'):
                self.page.inspect(source)
            dispatch.assert_not_called()
        with patch('src.ui.skills_page.QFileDialog.getOpenFileName', return_value=('', '')), patch.object(self.page, 'inspect') as inspect:
            self.page.choose_file()
            inspect.assert_not_called()
