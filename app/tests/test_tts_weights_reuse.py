import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.core import artifact_trust as at, model_transfer as mt, models_manager as mm
from src.core.synthesis import local_models as lm, qwen3_weights as qw, runtime_packages as rp
from src.core.synthesis import weights_reuse as reuse


class TTSWeightsReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.receipts = self.base / '.artifact-trust'
        for name in ('models_root', 'app_data_dir'):
            mocked = patch.object(mm, name, return_value=self.base)
            mocked.start(); self.addCleanup(mocked.stop)
        for name in ('_snapshot_download_cancellable',):
            mocked = patch.object(rp, name, side_effect=AssertionError('must not download weights'))
            mocked.start(); self.addCleanup(mocked.stop)
        self.payload = b'official known weights'
        self.lock = dict(repo='Qwen/fixture', revision='a' * 40, files=[
            dict(path='model.safetensors', size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest())])
        mocked = patch.object(qw, 'MODELS', {'Base': self.lock})
        mocked.start(); self.addCleanup(mocked.stop)

    def legacy(self, artifact, root, trust, paths=None):
        receipt = at.write_install_receipt(artifact, root, trust, self.receipts, inventory_paths=paths)
        value = json.loads(receipt.read_text())
        value.pop('content_sha256')
        value.pop('root_volume_uuid', None)  # Legacy receipt before stable volume IDs.
        value['root_device'] += 1
        for item in value['files']: item['device'] += 1
        receipt.write_text(json.dumps(value))
        self.receipt, self.original = receipt, receipt.read_bytes()
        return value

    def qwen(self):
        self.models = rp.model_dir('local_qwen3_tts') / 'models'
        self.root = self.models / 'Base'
        self.root.mkdir(parents=True)
        self.weight = self.root / 'model.safetensors'
        self.weight.write_bytes(self.payload)
        (self.root / '.lulu-qwen-model.json').write_text(json.dumps({'model': 'Base', 'revision': self.lock['revision']}))
        self.legacy('qwen3-weights:Base', self.root, self.lock)

    def vox(self):
        self.root = lm.model_dir(lm.VOXCPM2.id)
        self.root.mkdir(parents=True)
        self.trust = lm._trust(lm.VOXCPM2.id)
        cache = lm.hf_cache_dir(lm.VOXCPM2.id)
        storage = cache / ('models--' + self.trust['hf_repo'].replace('/', '--'))
        self.blobs = storage / 'blobs'; self.blobs.mkdir(parents=True)
        snapshot = storage / 'snapshots' / self.trust['hf_revision']; snapshot.mkdir(parents=True)
        self.plan_files, inventory = [], []
        for name, raw, algorithm in [('model.safetensors', self.payload, 'sha256'), ('config.json', b'{}', 'git-sha1')]:
            digest = hashlib.sha256(raw).hexdigest() if algorithm == 'sha256' else hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
            blob = self.blobs / digest; blob.write_bytes(raw); inventory.append(blob)
            link = snapshot / name; link.symlink_to(os.path.relpath(blob, link.parent))
            self.plan_files.append(mt.File(link, len(raw), digest, algorithm))
        self.weight = inventory[0]
        marker = self.root / '.lulu_done'; marker.write_bytes(b'ok'); inventory.append(marker)
        self.legacy('tts-weights:' + lm.VOXCPM2.id, self.root, self.trust, inventory)
        self.plan = mt.Snapshot(self.trust['hf_repo'], self.trust['hf_revision'], cache, self.plan_files, local_dir=False)
        mocked = patch.object(mt, 'hf_plan', return_value=self.plan)
        self.metadata = mocked.start(); self.addCleanup(mocked.stop)


    def test_vox_direct_install_reuses_blob_files_and_preserves_snapshot_links(self):
        self.vox()
        before = self.weight.stat()
        rp.install_weights('local_voxcpm2')
        self.assertTrue(lm.is_downloaded())
        self.assertFalse(rp.is_runtime_installed('local_voxcpm2'))
        self.assertEqual(self.weight.stat(), before)
        self.assertTrue(self.plan_files[0].path.is_symlink())
        self.assertTrue(self.metadata.call_args.kwargs['read_only'])
        self.assertEqual(len(json.loads(self.receipt.read_text())['content_sha256']), 3)

    def test_prepare_tts_recovers_weights_before_missing_runtime_blocks_install(self):
        self.vox()
        session = mt.Session(lambda *_: None, lambda: None)
        with patch.object(mt, 'prepare_runtime', side_effect=RuntimeError('runtime needs preparation')), \
                patch.object(session, 'add', side_effect=AssertionError('no weight download plan')), \
                self.assertRaisesRegex(RuntimeError, 'runtime needs preparation'):
            mt.prepare_tts(session, 'local_voxcpm2')
        self.assertTrue(lm.is_downloaded())


    def test_content_tamper_with_original_timestamp_stops_without_redownload(self):
        self.qwen(); before = self.weight.stat()
        self.weight.write_bytes(b'X' + self.payload[1:])
        os.utime(self.weight, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(RuntimeError, '内容校验'):
            qw.install(self.models, name='Base')
        self.assertEqual(self.receipt.read_bytes(), self.original)


if __name__ == '__main__': unittest.main()
