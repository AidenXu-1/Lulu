"""Pinned candidate import/download transactions, with tiny fake weights only."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.core import models_manager as mm, model_transfer as mt
from src.core.engines import asr_resource as resource


class ModelInstallV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.model = 'local_whisper_mlx_4bit'
        self.repo = mm.LOCAL_MODELS[self.model].repo
        self.files = {'model.safetensors': b'weights', 'config.json': b'{}'}
        catalog = {self.model: {self.repo: {n: (len(b), hashlib.sha256(b).hexdigest()) for n,b in self.files.items()}}}
        for p in (patch.object(mm, 'models_root', return_value=self.root/'managed'),
                  patch.object(mm, 'app_data_dir', return_value=self.root/'appdata'),
                  patch.object(mm, 'LOCAL_MODEL_FILES', catalog),
                  patch.object(mm, 'runtime_issue', return_value=None)):
            p.start(); self.addCleanup(p.stop)
        self.source = self.root/'source'; self.source.mkdir()
        for n,b in self.files.items(): (self.source/n).write_bytes(b)

    def test_import_copies_exact_bytes_keeps_original_and_only_one_weight(self):
        result = mm.import_local_model(self.model, self.source)
        self.assertTrue(mm.is_downloaded(self.model))
        self.assertEqual((result/'weights.safetensors').read_bytes(), b'weights')
        self.assertFalse((result/'model.safetensors').exists())
        self.assertEqual((self.source/'model.safetensors').read_bytes(), b'weights')

    def test_invalid_import_cannot_replace_good_model(self):
        result = mm.import_local_model(self.model, self.source)
        (self.source/'model.safetensors').write_bytes(b'wrongxx')
        with self.assertRaisesRegex(RuntimeError, '固定版本'): mm.import_local_model(self.model, self.source)
        self.assertEqual((result/'weights.safetensors').read_bytes(), b'weights')
        self.assertTrue(mm.is_downloaded(self.model))

    def test_receipt_failure_rolls_back_entire_installation(self):
        result = mm.import_local_model(self.model, self.source)
        before = mm.snapshot_install_receipt(mm._artifact_id(self.model), mm._receipt_base())[1]
        with patch.object(mm, 'write_install_receipt', side_effect=OSError('full')):
            with self.assertRaises(OSError): mm.import_local_model(self.model, self.source)
        self.assertEqual((result/'weights.safetensors').read_bytes(), b'weights')
        self.assertEqual(mm.snapshot_install_receipt(mm._artifact_id(self.model), mm._receipt_base())[1], before)
        self.assertTrue(mm.is_downloaded(self.model))

    def test_download_filters_and_import_share_installation(self):
        calls = []
        def download(repo, *, revision, local_dir, allow_patterns, tqdm_class):
            calls.append((repo, allow_patterns))
            for n,b in self.files.items(): (Path(local_dir)/n).write_bytes(b)
        with patch.object(mt, 'hf_snapshot', side_effect=download):
            result = mm.download_blocking(self.model)
        self.assertEqual(calls, [(self.repo, list(self.files))])
        self.assertTrue(mm.is_downloaded(self.model))
        self.assertFalse((result/'model.safetensors').exists())

    def test_cancel_preserves_old_installation(self):
        result = mm.import_local_model(self.model, self.source)
        with self.assertRaises(mm.DownloadCancelled):
            mm.import_local_model(self.model, self.source, is_cancelled=lambda: True)
        self.assertEqual((result/'weights.safetensors').read_bytes(), b'weights')
        self.assertTrue(mm.is_downloaded(self.model))

    def test_maintenance_never_starts_while_batch_retains_weights(self):
        result = mm.import_local_model(self.model, self.source)
        with resource.batch_context('install-test'):
            with resource.model_use(self.model, retain=True): pass
        try:
            for operation in (lambda:mm.delete(self.model), lambda:mm.import_local_model(self.model,self.source),
                              lambda:mm.relocate_models(self.root/'other')):
                with self.assertRaises(resource.ASRResourceBusy): operation()
            self.assertTrue(result.is_dir())
        finally: resource.finish_batch('install-test')

    def test_config_save_failure_restores_files_and_receipts(self):
        from types import SimpleNamespace
        result = mm.import_local_model(self.model, self.source)
        cfg = SimpleNamespace(models_dir='', save=lambda:False)
        with self.assertRaisesRegex(RuntimeError,'恢复原位置'):
            mm.relocate_and_save(self.root/'other', cfg)
        self.assertEqual(cfg.models_dir, '')
        self.assertTrue(result.is_dir())
        self.assertTrue(mm.is_downloaded(self.model))
