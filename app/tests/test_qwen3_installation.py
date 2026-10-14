import hashlib, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis import qwen3_weights as qw, qwen3_runtime as qr, runtime_packages as rp

class QwenInstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.target=self.root/'installed';(self.source/'Base').mkdir(parents=True)
        self.data=b'official pinned model';(self.source/'Base/model.safetensors').write_bytes(self.data)
        self.lock={'Base':{'repo':'Qwen/test','revision':'a'*40,'files':[{'path':'model.safetensors','size':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest()}]}}
        for p in (patch.object(qw,'MODELS',self.lock),patch.object(qw.mm,'app_data_dir',return_value=self.root/'private')):p.start();self.addCleanup(p.stop)
    def test_local_import_verified_and_mutation_invalidates_receipt(self):
        qw.install(self.target,local_models=self.source)
        self.assertTrue(qw.is_installed(self.target));self.assertEqual((self.target/'Base/model.safetensors').read_bytes(),self.data)
        (self.target/'Base/model.safetensors').write_bytes(b'corrupted')
        self.assertFalse(qw.is_installed(self.target))
    def test_bad_source_preserves_previous_model(self):
        (self.target/'Base').mkdir(parents=True);(self.target/'Base/keep').write_bytes(b'old')
        (self.source/'Base/model.safetensors').write_bytes(b'bad')
        with self.assertRaisesRegex(RuntimeError,'校验失败'):qw.install(self.target,local_models=self.source)
        self.assertEqual((self.target/'Base/keep').read_bytes(),b'old');self.assertFalse(qw.is_installed(self.target))
    def test_cancel_keeps_source_and_no_ready_destination(self):
        with self.assertRaises(rp.CancelledError):qw.install(self.target,is_cancelled=lambda:True,local_models=self.source)
        self.assertEqual((self.source/'Base/model.safetensors').read_bytes(),self.data);self.assertFalse(qw.is_installed(self.target))
    def test_production_requires_managed_runtime(self):
        with patch.dict('os.environ',{},clear=True),patch.object(rp,'is_runtime_installed',return_value=False):
            self.assertFalse(qr.readiness('clone')[0])
    def test_repair_failure_restores_previous_files(self):
        (self.target/'Base').mkdir(parents=True);(self.target/'Base/keep').write_bytes(b'old')
        with patch.object(qw,'write_install_receipt',side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError,'write failed'):qw.install(self.target,local_models=self.source)
        self.assertEqual((self.target/'Base/keep').read_bytes(),b'old')
