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

    def test_real_toggle_search_detail_and_return(self):
        self.assertEqual(len(self.page.entries),6)
        self.assertEqual([self.page.origin.itemText(i) for i in range(self.page.origin.count())], ['全部来源','Lulu 系统','用户安装'])
        self.page.search.setText('智能摘要')
        toggles=self.page.scroll.widget().findChildren(ConsumerSwitch)
        self.assertEqual(len(toggles),1)
        QTest.mouseClick(toggles[0],Qt.LeftButton)
        self.wait()
        self.assertFalse(next(e for e in self.page.entries if e['name']=='lulu-summary')['enabled'])
        row=next(e for e in self.page.entries if e['name']=='lulu-summary')
        self.page.show_detail(row);self.wait()
        self.assertEqual(self.page.stack.currentIndex(),1)
        self.assertTrue(self.page.preview.toPlainText())
        self.page.file_select.setCurrentText('SKILL.md');self.wait()
        self.assertTrue(self.page.preview.toPlainText())
        self.page.stack.setCurrentIndex(0)
        self.assertEqual(self.page.search.text(),'智能摘要')

    def test_switch_uninstalls_and_reinstalls_real_files(self):
        self.page.search.setText('智能摘要')
        def toggle():
            return self.page.scroll.widget().findChildren(ConsumerSwitch)[0]
        path = self.root/'skills/lulu-summary'
        self.assertTrue(path.is_dir())
        QTest.mouseClick(toggle(),Qt.LeftButton)
        self.assertTrue(toggle().isChecked())  # No success shown before completion.
        self.wait()
        self.assertFalse(path.exists())
        self.assertFalse(toggle().isChecked())
        self.page.refresh(); self.wait()
        self.assertFalse(toggle().isChecked())
        QTest.mouseClick(toggle(),Qt.LeftButton)
        self.assertFalse(toggle().isChecked())
        self.wait()
        self.assertTrue(path.is_dir())
        self.assertTrue(toggle().isChecked())

    def test_local_picker_and_management_only_toolbar(self):
        with patch('src.ui.skills_page.QFileDialog.getOpenFileName',return_value=(str(self.root/'SKILL.md'), '')), patch.object(self.page,'inspect') as inspect:
            QTest.mouseClick(self.page.install_button, Qt.LeftButton)
            inspect.assert_called_once_with(str(self.root/'SKILL.md'))
        self.assertFalse(hasattr(self.page, 'choose_link'))
        self.assertFalse(hasattr(self.page, 'search_public'))
        buttons = self.page.findChildren(QPushButton)
        self.assertFalse(any(b.text() == '制作技能' for b in buttons))
        self.assertFalse(any(b.text() in {'更多','导出技能包','刷新','转换旧预设'} for b in buttons))
        details = [b for b in self.page.scroll.widget().findChildren(QPushButton) if b.text() == '详情']
        self.assertEqual(len(details), 6)
        folders = [b for b in buttons if b.property('skillFolder')]
        self.assertEqual(len(folders), 6)
        with patch('src.ui.skills_page.QDesktopServices.openUrl',return_value=True) as open_url:
            QTest.mouseClick(folders[0], Qt.LeftButton)
            self.assertEqual(open_url.call_args.args[0].toLocalFile(), folders[0].property('skillFolder'))

    def test_failed_toggle_returns_to_saved_state(self):
        self.page.dispatch=lambda *a,**k: (_ for _ in ()).throw(ValueError('模拟保存失败'))
        toggle=self.page.scroll.widget().findChildren(ConsumerSwitch)[0]
        original=toggle.isChecked()
        QTest.mouseClick(toggle,Qt.LeftButton)
        deadline=time.monotonic()+2
        while self.page.busy and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.01)
        self.assertIn('模拟保存失败',self.page.status.text())
        self.assertEqual(self.page.scroll.widget().findChildren(ConsumerSwitch)[0].isChecked(),original)

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
            toggle=self.page.scroll.widget().findChildren(ConsumerSwitch)[0]
            self.assertFalse(toggle.isChecked())
            self.assertFalse(toggle.isEnabled())
            self.assertTrue(self.page.scroll.widget().findChildren(QProgressBar))
            self.assertIn('正在安装',self.page.feedback['lulu-summary']['phase'])
        finally:
            release.set()
        self.wait()
        self.assertTrue(self.page.scroll.widget().findChildren(ConsumerSwitch)[0].isChecked())
        self.assertEqual(self.page.feedback['lulu-summary']['phase'],'已安装，可以使用')
        self.assertFalse(self.page.scroll.widget().findChildren(QProgressBar))

    def test_failed_local_inspection_shows_source_row_without_usable_switch(self):
        import threading
        from PySide6.QtWidgets import QProgressBar
        started, release = threading.Event(), threading.Event()
        def download(*a,**k):
            started.set()
            release.wait(5)
            raise ValueError('技能包已损坏')
        self.page.dispatch = download
        source = str(self.root/'broken.zip')
        Path(source).write_text('broken')
        try:
            self.page.inspect(source)
            self.assertTrue(started.wait(1))
            self.app.processEvents()
            self.assertTrue(self.page.scroll.widget().findChildren(QProgressBar))
            self.assertEqual(self.page.feedback['incoming:' + source]['phase'],'正在检查本地技能…')
        finally:
            release.set()
        end=time.monotonic()+5
        while self.page.busy and time.monotonic()<end:
            self.app.processEvents();time.sleep(.01)
        self.assertFalse(self.page.busy)
        self.assertIn('技能包已损坏',self.page.feedback['incoming:' + source]['phase'])
        switch=next(x for x in self.page.scroll.widget().findChildren(ConsumerSwitch) if x.property('skillName').startswith('incoming:'))
        self.assertFalse(switch.isChecked());self.assertFalse(switch.isEnabled())

    def test_successful_write_but_failed_confirmation_does_not_light_switch(self):
        self.page.change_installation(next(r for r in self.page.entries if r['name']=='lulu-summary'),False)
        self.wait()
        dispatch=self.page.dispatch
        def fail_read(operation, **arguments):
            if operation=='skill_list':raise ValueError('读取安装状态失败')
            return dispatch(operation, **arguments)
        self.page.dispatch=fail_read
        self.page.change_installation(next(r for r in self.page.entries if r['name']=='lulu-summary'),True)
        end=time.monotonic()+5
        while self.page.busy and time.monotonic()<end:
            self.app.processEvents();time.sleep(.01)
        switch=next(x for x in self.page.scroll.widget().findChildren(ConsumerSwitch) if x.property('skillName')=='lulu-summary')
        self.assertFalse(switch.isChecked())
        self.assertIn('状态确认失败',self.page.feedback['lulu-summary']['phase'])
        self.page.dispatch=dispatch
        self.page.refresh();self.wait()
        switch=next(x for x in self.page.scroll.widget().findChildren(ConsumerSwitch) if x.property('skillName')=='lulu-summary')
        self.assertTrue(switch.isChecked())

    def test_narrow_layout_keeps_all_buttons(self):
        self.page.resize(430,650)
        QTest.qWait(10)
        self.assertLessEqual(self.page.scroll.widget().width(),self.page.scroll.viewport().width())
        self.assertTrue(self.page.install_button.isVisible())

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
