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


class ModelLifecycleCoreTests(unittest.TestCase):
    def test_delete_removes_model_receipt_and_all_revision_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "models/local_whisper_mlx"
            model.mkdir(parents=True)
            (model / "weights.bin").write_bytes(b"model")
            cache_root = root / "models/.download-cache"
            current = cache_root / "local_whisper_mlx-current/candidate"
            old = cache_root / "local_whisper_mlx-old/candidate"
            current.mkdir(parents=True)
            old.mkdir(parents=True)
            (current / "part.bin").write_bytes(b"part")
            (old / "part.bin").write_bytes(b"old")

            removed_receipts = []
            with (
                patch.object(mm, "models_root", return_value=root),
                patch.object(mm, "app_data_dir", return_value=root),
                patch.object(mm, "remove_install_receipt", side_effect=lambda artifact, _base: removed_receipts.append(artifact)),
            ):
                mm.delete("local_whisper_mlx")

            self.assertFalse(model.exists())
            self.assertFalse((cache_root / "local_whisper_mlx-current").exists())
            self.assertFalse((cache_root / "local_whisper_mlx-old").exists())
            self.assertEqual(removed_receipts, ["asr-model:local_whisper_mlx"])

    def test_delete_unlinks_managed_symlink_without_touching_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            managed = root / "models"
            managed.mkdir()
            (managed / "local_whisper_mlx").symlink_to(outside, target_is_directory=True)

            with (
                patch.object(mm, "models_root", return_value=root),
                patch.object(mm, "app_data_dir", return_value=root),
                patch.object(mm, "remove_install_receipt"),
            ):
                mm.delete("local_whisper_mlx")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse(os.path.lexists(managed / "local_whisper_mlx"))

    def test_managed_storage_size_includes_partial_download_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = root / "models/local_sensevoice"
            partial = root / "models/.download-cache/local_qwen3-test/candidate"
            installed.mkdir(parents=True)
            partial.mkdir(parents=True)
            (installed / "model.bin").write_bytes(b"a" * 7)
            (partial / "part.bin").write_bytes(b"b" * 11)
            with patch.object(mm, "models_root", return_value=root):
                self.assertEqual(mm.managed_storage_size(), 18)

    def test_main_window_blocks_only_the_exact_asr_model_currently_in_use(self):
        worker = SimpleNamespace(
            _engine_name="local_whisper_mlx",
            isRunning=lambda: True,
        )
        window = SimpleNamespace(
            current=(object(), worker),
            voice_page=None,
            gsv_finetune_running=lambda: False,
        )

        self.assertIn("正在转录", MainWindow._model_usage_reason(window, "local_whisper_mlx"))
        self.assertEqual(MainWindow._model_usage_reason(window, "local_sensevoice"), "")

    def test_main_window_blocks_only_the_exact_tts_model_currently_in_use(self):
        running = SimpleNamespace(isRunning=lambda: True)
        voice_page = SimpleNamespace(
            _worker=running,
            _gsv_worker=None,
            _indextts2_worker=None,
            _indextts2_release_worker=None,
        )
        window = SimpleNamespace(
            current=None,
            voice_page=voice_page,
            gsv_finetune_running=lambda: False,
        )

        self.assertIn("正在生成", MainWindow._model_usage_reason(window, "local_voxcpm2"))
        self.assertEqual(MainWindow._model_usage_reason(window, "local_sensevoice"), "")


from tests.model_settings_fixtures import ControllerCase, FakeWorker, m

class ModelLifecycleUiTests(ControllerCase):
    def test_selection_failure_restores_old_default_and_does_not_notify(self):
        for model, field, old in [('sensevoice','engine_name','local_whisper_mlx'),('indextts2','default_tts_model','local_indextts2')]:
            setattr(self.cfg, field, old)
            self.cfg.save.return_value = False
            with patch.object(mm,'is_ready',return_value=True), patch.object(rp,'status',return_value='ready'):
                with self.assertRaisesRegex(RuntimeError,'保留原设置'):
                    self.c.local_action(model,'default')
            self.assertEqual(getattr(self.cfg,field),old)
        self.changed.assert_not_called()

    def test_selection_exception_restores_old_default(self):
        self.cfg.save.side_effect = OSError('disk full')
        with patch.object(mm,'is_ready',return_value=True), self.assertRaises(OSError):
            self.c.local_action('whisper','default')
        self.assertEqual(self.cfg.engine_name,'local_sensevoice')

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

    def test_active_install_prevents_second_model_install(self):
        worker=FakeWorker();self.c._operation(worker,'whisper','local_whisper_mlx')
        with self.assertRaisesRegex(RuntimeError,'模型操作'):
            self.c.local_action('indextts2','install')
        self.assertEqual(self.c.active_model_ids(),{'local_whisper_mlx'})

    def test_pause_keeps_owner_until_thread_finished_then_is_resumable(self):
        worker=FakeWorker();self.c._operation(worker,'whisper','local_whisper_mlx')
        self.c.local_action('whisper','pause')
        self.assertTrue(worker.cancel_requested)
        self.assertTrue(self.c.has_active_work())
        self.assertIn(worker,self.c.owned_workers())
        worker.cancelled.emit();worker.finish()
        self.assertFalse(self.c.has_active_work())
        self.assertEqual(self.c.jobs['whisper']['status'],'paused')

    def test_removal_failure_is_visible_and_refreshes_actual_state(self):
        worker=FakeWorker();self.c._operation(worker,'voxcpm2','local_voxcpm2')
        worker.settled.emit('still present');worker.finish()
        self.assertIn('still present',self.notices)
        self.changed.assert_called_once()
        self.c.refresh.assert_called_once()
        self.assertNotIn('voxcpm2',self.c.jobs)

    def test_invalid_operation_never_allocates_a_worker(self):
        for mid,action in [('unknown','install'),('whisper','erase')]:
            with self.assertRaises(ValueError):self.c.local_action(mid,action)
        self.assertEqual(self.c.owned_workers(),[])
