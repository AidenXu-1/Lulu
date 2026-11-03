"""Real Qt controls, temporary media and fake model/update services only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QWidget

from src.config import AppConfig
from src.core.agent_workspace.control import SETTINGS
from src.core.agent_workspace.documents import digest
from src.core.agent_workspace.store import encode
from src.core.updater.models import UpdateSnapshot
from src.core.live_recording.service import uid
from src.ui.cli_control import CliControl
from tests.test_live_full_export import FullMediaTests
from tests.test_live_recording_ui import NativeLiveTests


class ControlWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.cfg = AppConfig()
        self.window.cfg.save = Mock(return_value=True)
        self.window._on_models_changed = Mock()
        self.window.update_controller = SimpleNamespace(snapshot=UpdateSnapshot(), _worker=None)
        self.control = CliControl(self.window, enabled=False)
        self.addCleanup(self.window.deleteLater)

    def command(self, domain, action, arguments):
        return dict(request_id='request1', domain=domain, action=action, arguments=arguments, session=self.control.nonce)

    def test_settings_apply_changes_window_and_persistent_save_together(self):
        current = {key: getattr(self.window.cfg, key) for key in SETTINGS}
        self.control.execute(self.command('settings', 'apply', dict(changes={'prefer_subtitles':True}, expected_hash=digest(encode(current)))))
        self.assertTrue(self.window.cfg.prefer_subtitles)
        self.window.cfg.save.assert_called_once()
        self.assertEqual(self.control.reports[-1]['state'], 'succeeded')
        with self.assertRaisesRegex(ValueError, '已有变化'):
            self.control.execute(self.command('settings', 'apply', dict(changes={'prefer_subtitles':False}, expected_hash=digest(encode(current)))))
        self.assertTrue(self.window.cfg.prefer_subtitles)

    def test_settings_failed_save_restores_window_memory(self):
        self.window.cfg.save.return_value = False
        current = {key: getattr(self.window.cfg, key) for key in SETTINGS}
        with self.assertRaises(ValueError):
            self.control.execute(self.command('settings', 'apply', dict(changes={'prefer_subtitles':True}, expected_hash=digest(encode(current)))))
        self.assertFalse(self.window.cfg.prefer_subtitles)

    def test_model_receipt_waits_for_existing_controller_actual_settlement(self):
        from tests.model_settings_fixtures import FakeWorker
        from PySide6.QtCore import Signal
        class Controller(QWidget):
            operation_settled = Signal(str, str, bool)
            def __init__(self):
                super().__init__()
                self.active = {}
            def local_action(self, key, action, **kwargs):
                self.active[key] = (FakeWorker(), 'local_whisper_mlx')
        controller = Controller()
        self.addCleanup(controller.deleteLater)
        self.window._ensure_page_loaded = lambda _: SimpleNamespace(controller=controller)
        self.control.execute(self.command('models', 'install', dict(model_id='local_whisper_mlx', destination=None, local_archive=None)))
        self.assertEqual(self.control.reports[-1]['state'], 'running')
        controller.operation_settled.emit('whisper', '模拟下载中断', False)
        self.assertEqual(self.control.reports[-1]['state'], 'failed')
        self.assertIn('中断', self.control.reports[-1]['message'])

    def test_qwen_feature_waits_for_plan_and_actual_install_settlement(self):
        from PySide6.QtCore import Signal
        class Controller(QWidget):
            operation_settled = Signal(str, str, bool)
            def __init__(self):
                super().__init__()
                self.active = {}
            def feature_action(self, feature, action, **kwargs):
                self.active['qwen-plan'] = (object(), 'local_qwen3_tts')
        controller = Controller()
        self.addCleanup(controller.deleteLater)
        self.window._ensure_page_loaded = lambda _: SimpleNamespace(controller=controller)
        self.control.execute(self.command('models', 'install', dict(model_id='local_qwen3_tts', feature='clone', local_archive=None)))
        self.assertEqual(self.control.reports[-1]['state'], 'running')
        controller.operation_settled.emit('qwen', '模拟计划校验失败', False)
        self.assertEqual(self.control.reports[-1]['state'], 'failed')


class FullExportWindowTests(NativeLiveTests):
    def test_full_export_button_creates_actual_mp4_and_reports_completion(self):
        from tests.test_live_recording_delete import register_recording
        media = FullMediaTests()
        media.setUp()
        try:
            rid, folder, _ = register_recording(self.service, media.root)
            self.service.index.write('DELETE FROM segments WHERE recording_id=?', (rid,))
            self.service.index.write('UPDATE recordings SET gaps=? WHERE id=?', ('[[1,2]]', rid))
            for index, segment in enumerate(media.segments):
                target = folder / Path(segment['path']).name
                Path(segment['path']).replace(target)
                segment['path'] = str(target)
                self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                    (uid('segment'), rid, segment['path'], index+1, segment['duration'], segment['bytes'], 1, 1, segment['sha256']))
            data = self.service.handle('detail', dict(session=self.controller.session, recording_id=rid))
            self.page._detail(data)
            self.app.processEvents()
            dialog = self.page.detail_dialog
            self.assertTrue(dialog.full_export_button.isEnabled())
            output = media.root/'gui-full.mp4'
            with patch('src.ui.live_recording_page.QFileDialog.getSaveFileName', return_value=(str(output), 'MP4 视频 (*.mp4)')):
                dialog.full_export_button.click()
                self.until(lambda: bool(self.page.exporting))
                self.assertTrue(self.page.cancel_exports_button.isVisible())
                self.until(lambda: output.exists() and not self.page.exporting)
            self.assertIn('已导出', self.page.message.text())
            self.assertIn('断流', self.page.message.text())
            self.assertTrue(all(Path(s['path']).exists() for s in media.segments))
        finally:
            media.doCleanups()
