import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.core import models_manager as mm
from src.core.synthesis import indextts2_weights as iw
from src.core.synthesis import runtime_packages as rp
from src.ui.main_window import MainWindow


from tests.model_settings_fixtures import ControllerCase, FakeWorker, m

class ModelLifecycleUiTests(ControllerCase):


    def test_default_model_removal_confirms_offline_gap_without_an_available_fallback(self):
        with patch.object(m.mm,'is_ready',return_value=False), patch.object(m.dialogs,'confirm',return_value=True) as confirm, patch.object(m,'ModelRemoveWorker',FakeWorker):
            self.c.local_action('sensevoice','remove')
        self.assertIn('本地转录暂不可用',confirm.call_args.args[2])
        self.assertEqual(self.cfg.engine_name,'local_sensevoice')
        self.assertIn('sensevoice',self.c.active)

    def test_default_model_removal_commits_ready_fallback_before_starting_worker(self):
        with patch.object(m.mm,'is_ready',side_effect=lambda mid:mid=='local_whisper_mlx'), patch.object(m.dialogs,'confirm',return_value=True), patch.object(m,'ModelRemoveWorker',FakeWorker):
            self.c.local_action('sensevoice','remove')
        self.assertEqual(self.cfg.engine_name,'local_whisper_mlx')
        self.cfg.save.assert_called_once()
        self.assertIn('sensevoice',self.c.active)

    def test_default_model_removal_save_failure_keeps_original_and_never_deletes(self):
        self.cfg.save.return_value=False
        with patch.object(m.mm,'is_ready',side_effect=lambda mid:mid=='local_whisper_mlx'), patch.object(m.dialogs,'confirm',return_value=True), patch.object(m,'ModelRemoveWorker',FakeWorker):
            with self.assertRaisesRegex(RuntimeError,'保存替代模型'):
                self.c.local_action('sensevoice','remove')
        self.assertEqual(self.cfg.engine_name,'local_sensevoice')
        self.assertFalse(self.c.active)

    def test_in_use_and_relocation_prevent_model_changes(self):
        self.c.model_in_use=lambda mid:'正在使用' if mid=='local_whisper_mlx' else ''
        with patch.object(m,'DownloadWorker') as worker, self.assertRaisesRegex(RuntimeError,'正在使用'):
            self.c.local_action('whisper','install')
        worker.assert_not_called()
        self.c.relocation_busy=lambda:True
        with self.assertRaisesRegex(RuntimeError,'迁移'):
            self.c.local_action('indextts2','install')
