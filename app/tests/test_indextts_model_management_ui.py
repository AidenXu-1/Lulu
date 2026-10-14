"""V2 routes both Index versions to their own installers and usage guards."""
from types import SimpleNamespace
from unittest.mock import patch
from tests.model_settings_fixtures import ControllerCase, FakeWorker, m
from src.core.synthesis import indextts2_subprocess as adapter
from src.ui.main_window import MainWindow

class IndexManagementUiTests(ControllerCase):
    def test_management_snapshot_keeps_both_index_versions_in_approved_order(self):
        with self.public_state():state=self.c._read_state()
        self.assertEqual([x['modelId'] for x in state['localModels']['voice']],['local_indextts2_0','local_indextts2','local_voxcpm2','local_qwen3_tts'])

    def test_index_install_routes_exact_version_without_file_picker(self):
        for public,real in [('indextts2','local_indextts2_0'),('indextts25','local_indextts2')]:
            worker=FakeWorker()
            with patch.object(m.rp,'current_platform',return_value='mac-arm64'),patch.object(m,'TTSRuntimeInstallWorker',return_value=worker) as install,patch.object(m.QFileDialog,'getOpenFileName',side_effect=AssertionError('unexpected file picker')):
                self.c.local_action(public,'install')
            self.assertEqual(install.call_args.args,(real,));worker.finish()

    def test_index_removal_targets_only_selected_version(self):
        worker=FakeWorker()
        with patch.object(m.dialogs,'confirm',return_value=True),patch.object(m,'ModelRemoveWorker',return_value=worker) as remove:
            self.c.local_action('indextts2','remove')
        remove.assert_called_once_with('local_indextts2_0',tts=True)
        worker.finish()

    def test_index2_busy_guard_does_not_block_idle_25(self):
        page = SimpleNamespace(_indextts2_worker=SimpleNamespace(_model_version='2.0', isRunning=lambda: True))
        window = SimpleNamespace(current=None, voice_page=page)
        with patch.object(adapter, 'persistent_worker_status', return_value={'alive': True, 'model_version': '2.0'}):
            self.assertIn('正在生成', MainWindow._model_usage_reason(window, 'local_indextts2_0'))
            self.assertEqual(MainWindow._model_usage_reason(window, 'local_indextts2'), '')
        page._indextts2_worker = None
        with patch.object(adapter, 'persistent_worker_status', return_value={'alive': True, 'model_version': '2.0'}):
            self.assertIn('仍在内存', MainWindow._model_usage_reason(window, 'local_indextts2_0'))
            self.assertEqual(MainWindow._model_usage_reason(window, 'local_indextts2'), '')
        with patch.object(adapter, 'persistent_worker_status', side_effect=RuntimeError('fixture')):
            self.assertIn('无法确认', MainWindow._model_usage_reason(window, 'local_indextts2_0'))
