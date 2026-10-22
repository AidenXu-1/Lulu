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
        self.assertEqual(self.cfg.engine_name,'local_sensevoice_q8')
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
        self.assertEqual(self.cfg.engine_name,'local_sensevoice_q8')
        self.assertFalse(self.c.active)

    def test_in_use_and_relocation_prevent_model_changes(self):
        self.c.model_in_use=lambda mid:'正在使用' if mid=='local_whisper_mlx' else ''
        with patch.object(m,'DownloadWorker') as worker, self.assertRaisesRegex(RuntimeError,'正在使用'):
            self.c.local_action('whisper','install')
        worker.assert_not_called()
        self.c.storage_busy=lambda:True
        with self.assertRaisesRegex(RuntimeError,'迁移'):
            self.c.local_action('indextts2','install')

    def connect_window_guards(self, *, task_active=False):
        page = SimpleNamespace(storage_is_busy=self.c.storage_is_busy,
            has_active_work=self.c.has_active_work, active_model_ids=self.c.active_model_ids)
        window = SimpleNamespace(models_page=page, tasks=[SimpleNamespace(is_active=task_active)],
            _storage_relocation_in_progress=False, _mixed_rec_active=False,
            _mixed_pending_stop=False, _system_rec_session=None)
        window.has_active_work=lambda:MainWindow.has_active_work(window)
        window._model_usage_reason=lambda mid:MainWindow._model_usage_reason(window, mid)
        self.c.storage_busy=lambda:MainWindow.storage_relocation_active(window)
        self.c.relocation_busy=lambda:MainWindow.storage_relocation_busy(window)
        self.c.model_in_use=window._model_usage_reason
        return window

    def test_unrelated_extraction_does_not_block_tts_install(self):
        self.connect_window_guards(task_active=True)
        self.assertTrue(self.c.relocation_busy())
        with patch.object(m.rp,'current_platform',return_value='mac-arm64'), \
                patch.object(m,'TTSRuntimeInstallWorker',FakeWorker):
            self.c.local_action('indextts2','install')
        self.assertIn('indextts2',self.c.active)

    def test_another_install_still_blocks_model_storage_changes(self):
        self.connect_window_guards(task_active=True)
        self.c._operation(FakeWorker(),'voxcpm2','local_voxcpm2')
        with patch.object(m,'TTSRuntimeInstallWorker') as install:
            with self.assertRaisesRegex(RuntimeError,'有模型操作'):
                self.c.local_action('indextts2','install')
        install.assert_not_called()

    def test_qwen_active_install_can_pause_cancel_and_queue_another_feature(self):
        from src.core.synthesis import qwen3_deployment as dp
        for action, feature, phase in [('pause','clone','paused'),
                                       ('cancel','clone','cancelling'),
                                       ('install','preset',None),
                                       ('resume','preset',None)]:
            with self.subTest(action=action):
                window = self.connect_window_guards()
                worker = FakeWorker()
                self.c._operation(worker,'qwen','local_qwen3_tts',qwen=True)
                self.assertTrue(MainWindow.storage_relocation_busy(window))
                with patch.object(dp,'read_state',return_value={'features':{'Base':{'state':'installing'}}}), \
                        patch.object(dp,'request') as request, \
                        patch.object(m.dialogs,'confirm',return_value=True):
                    self.c.feature_action(feature,action)
                name = m.FEATURE_IDS[feature]
                if phase:
                    request.assert_called_once_with([name],phase)
                else:
                    request.assert_called_once_with([name])
                worker.finish()

    def test_loaded_index_version_only_blocks_its_own_install(self):
        self.connect_window_guards()
        with patch('src.core.synthesis.indextts2_subprocess.persistent_worker_status',return_value={'alive':True,'model_version':'2.5'}), \
                patch('src.core.synthesis.qwen3_subprocess.persistent_worker_status',return_value={'alive':False}), \
                patch.object(m.rp,'current_platform',return_value='mac-arm64'), \
                patch.object(m,'TTSRuntimeInstallWorker',FakeWorker):
            self.assertTrue(self.c.relocation_busy())
            with self.assertRaisesRegex(RuntimeError,'仍在内存'):
                self.c.local_action('indextts25','install')
            self.c.local_action('indextts2','install')
        self.assertIn('indextts2',self.c.active)

    def test_actual_relocation_still_blocks_active_qwen_controls(self):
        from src.core.synthesis import qwen3_deployment as dp
        window = self.connect_window_guards()
        window._storage_relocation_in_progress=True
        self.c._operation(FakeWorker(),'qwen','local_qwen3_tts',qwen=True)
        with patch.object(dp,'request') as request:
            for action in ('pause','cancel','install','resume'):
                with self.subTest(action=action), self.assertRaisesRegex(RuntimeError,'位置正在变化'):
                    self.c.feature_action('clone',action)
        request.assert_not_called()

    def test_active_task_blocks_relocation_before_folder_picker(self):
        self.connect_window_guards(task_active=True)
        with patch.object(m.QFileDialog,'getExistingDirectory') as picker:
            with self.assertRaisesRegex(RuntimeError,'任务'):
                self.c.storage_change('voice')
        picker.assert_not_called()

    def test_task_starting_during_relocation_confirmation_prevents_move(self):
        window = self.connect_window_guards()
        def confirm(*args, **kwargs):
            window.tasks[0].is_active=True
            return True
        with patch('src.core.synthesis.indextts2_subprocess.persistent_worker_status',return_value={'alive':False}), \
                patch('src.core.synthesis.qwen3_subprocess.persistent_worker_status',return_value={'alive':False}), \
                patch.object(m.mm,'models_root',return_value=Path('/fixture/old')), \
                patch.object(m.QFileDialog,'getExistingDirectory',return_value='/fixture/new'), \
                patch.object(m.dialogs,'confirm',side_effect=confirm), \
                patch.object(m,'ModelsRelocateWorker') as relocate:
            with self.assertRaisesRegex(RuntimeError,'任务'):
                self.c.storage_change('voice')
        relocate.assert_not_called()

    def test_idle_relocation_still_moves_only_after_confirmation(self):
        self.connect_window_guards()
        with patch('src.core.synthesis.indextts2_subprocess.persistent_worker_status',return_value={'alive':False}), \
                patch('src.core.synthesis.qwen3_subprocess.persistent_worker_status',return_value={'alive':False}), \
                patch.object(m.mm,'models_root',return_value=Path('/fixture/old')), \
                patch.object(m.QFileDialog,'getExistingDirectory',return_value='/fixture/new'), \
                patch.object(m.dialogs,'confirm',return_value=True), \
                patch.object(m,'ModelsRelocateWorker',FakeWorker):
            self.c.storage_change('voice')
        worker = self.c.active['storage'][0]
        self.assertEqual(worker.args,(Path('/fixture/new'),self.cfg))
