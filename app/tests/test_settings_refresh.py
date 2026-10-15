"""Settings redesign: local sources, saved paths and restart truth, isolated Qt."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QBoxLayout, QLabel
from src.ui.settings_page import SettingsPage
from src.ui.skills_page import LocalSkillDropZone
from src.ui import theme as T
from tests.test_settings_page import _Cfg


class SettingsRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = _Cfg(self.root)
        T.apply_theme('warm')
        self.addCleanup(lambda: T.apply_theme('warm'))

    def page(self):
        from src.core import library, models_manager
        from src.core.agent_workspace import skills
        with patch.object(library, 'LIBRARY_DIR', self.root/'library'), patch.object(models_manager, 'app_data_dir', return_value=self.root), patch.object(skills, 'skill_root', return_value=self.root/'skills'):
            page = SettingsPage(self.cfg)
        self.addCleanup(page.close)
        self.addCleanup(page.shutdown_workers)
        return page

    def test_theme_switching_is_retired_without_changing_other_preferences(self):
        previous = vars(self.cfg).copy()
        page = self.page()
        self.assertNotIn('appearance', page.category_order)
        self.assertFalse(hasattr(page, 'theme_buttons'))
        self.assertFalse(hasattr(page, '_pick_theme'))
        self.assertEqual(vars(self.cfg), previous)

    def test_managed_locations_disclose_without_creating_or_moving_data(self):
        page = self.page()
        self.assertEqual(tuple(page.storage_groups), ('导出与下载','音色与模型','Lulu 资料'))
        self.assertTrue(page.managed_locations.isHidden())
        page.managed_locations_button.click()
        self.assertFalse(page.managed_locations.isHidden())
        for text in page.managed_path_labels.values():
            self.assertTrue(text.text().startswith(str(self.root)))
        with patch('src.ui.settings_page.dialogs.alert') as alert, patch('src.ui.settings_page.QDesktopServices.openUrl') as opener:
            page.managed_open_buttons['库内音频'].click()
            alert.assert_called_once()
            opener.assert_not_called()
        self.assertFalse((self.root/'library').exists())
        folder = self.root/'library/audio'
        folder.mkdir(parents=True)
        with patch('src.ui.settings_page.QDesktopServices.openUrl', return_value=True) as opener:
            page.managed_open_buttons['库内音频'].click()
            self.assertEqual(opener.call_args.args[0].toLocalFile(), str(folder))

    def test_cli_is_separate_and_install_prompt_only_copies(self):
        from src.ui.cli_info import agent_skill_install_prompt
        from PySide6.QtWidgets import QPushButton
        from PySide6.QtGui import QGuiApplication
        page = self.page()
        self.assertEqual(page.category_order[page.category_order.index('skills')+1], 'cli')
        self.assertFalse(page.detail_scrolls['general'].isAncestorOf(page.tools_cli_button))
        texts = [b.text() for b in page.skills_page.findChildren(QPushButton)]
        self.assertNotIn('选择技能文件夹', texts)
        self.assertNotIn('待安装草稿', texts)
        self.assertTrue(page.cli_copy_button.isEnabled())
        self.assertTrue(page.cli_repository_button.isEnabled())
        self.assertEqual(page.cli_repository_url, 'https://github.com/AidenXu-1/lulu-agent')
        with patch('src.ui.settings_page.QDesktopServices.openUrl', return_value=True) as opener:
            page.cli_copy_button.click()
            opener.assert_not_called()
            self.assertEqual(QGuiApplication.clipboard().text(), agent_skill_install_prompt(page.cli_repository_url))
            page.cli_repository_button.click()
            self.assertEqual(opener.call_args.args[0].toString(), page.cli_repository_url)
        self.assertIn('已复制', page.cli_install_note.text())


    def test_drop_accepts_local_skill_and_rejects_urls_multiple_and_busy(self):
        folder = self.root/'my-skill'; folder.mkdir()
        file = folder/'SKILL.md'; file.write_text('skill')
        button = LocalSkillDropZone()
        dropped = Mock(); rejected = Mock()
        button.source_dropped.connect(dropped)
        button.rejected.connect(rejected)
        def drop(urls):
            mime = QMimeData(); mime.setUrls(urls)
            event = QDropEvent(QPointF(10,10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            button.dropEvent(event)
            return event.isAccepted()
        self.assertTrue(drop([QUrl.fromLocalFile(str(file))]))
        dropped.assert_called_once_with(str(folder))
        dropped.reset_mock()
        self.assertFalse(drop([QUrl('https://github.com/a/b')]))
        self.assertFalse(drop([QUrl.fromLocalFile(str(file))]*2))
        unsupported=self.root/'notes.txt';unsupported.write_text('x')
        self.assertFalse(drop([QUrl.fromLocalFile(str(unsupported))]))
        button.setEnabled(False)
        self.assertFalse(drop([QUrl.fromLocalFile(str(file))]))
        dropped.assert_not_called()
        self.assertEqual(rejected.call_count, 3)

    def test_document_export_uses_configured_folder_and_cancel_writes_nothing(self):
        from src.ui.library_page import LibraryViewer
        viewer = SimpleNamespace(_title='示例', _cfg=self.cfg)
        with patch('src.ui.library_page.QFileDialog.getSaveFileName', return_value=('', '')) as picker, patch('src.ui.library_page.write_transcript') as write:
            LibraryViewer._export(viewer)
            self.assertEqual(picker.call_args.args[2], str(self.root/'exports/示例.txt'))
            write.assert_not_called()

    def test_workspace_audio_and_batch_pickers_use_configured_defaults(self):
        from src.ui.agent_workspace.widget import Bridge
        widget = SimpleNamespace(closing=False, cfg=self.cfg, batch_request=None)
        bridge = SimpleNamespace(widget=widget, respond=Mock())
        with patch('src.ui.agent_workspace.widget.QFileDialog.getExistingDirectory', return_value='') as picker:
            Bridge.nativeAction(bridge, 'batch', 'batch_export', '{"document_ids":["doc1"]}')
            self.assertEqual(picker.call_args.args[2], self.cfg.last_output_dir)
        with patch('src.core.library.load_voice', return_value={'missing_flags':{'audio_missing':False},'audio_filename':'音频.wav'}), patch('src.ui.agent_workspace.widget.QFileDialog.getSaveFileName', return_value=('', '')) as picker:
            Bridge.nativeAction(bridge, 'audio', 'audio_export', '{"document_id":"audio1"}')
            self.assertEqual(picker.call_args.args[2], str(self.root/'voices/音频.wav'))
