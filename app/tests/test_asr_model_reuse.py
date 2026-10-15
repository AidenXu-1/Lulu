import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import artifact_trust as at, model_transfer as mt, models_manager as mm


class ASRModelReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'models' / 'local_whisper_mlx'
        self.root.mkdir(parents=True)
        self.payload = b'known official model bytes'
        self.weights = self.root / 'weights.safetensors'
        self.weights.write_bytes(self.payload)
        (self.root / '.lulu_done').write_text('ok')
        self.trust = mm._trust_options('local_whisper_mlx')[0]
        self.receipts = self.base / '.artifact-trust'
        self.receipt = at.write_install_receipt(mm._artifact_id('local_whisper_mlx'), self.root,
                                                self.trust, self.receipts)
        data = json.loads(self.receipt.read_text())
        del data['content_sha256']
        data.pop('root_volume_uuid', None)  # Legacy receipt before stable volume IDs.
        data['root_device'] += 1
        for file in data['files']:
            file['device'] += 1
        self.receipt.write_text(json.dumps(data))
        self.original = self.receipt.read_bytes()
        for name, value in [('models_root', self.base), ('app_data_dir', self.base)]:
            p = patch.object(mm, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.download = patch.object(mm, 'download_blocking', side_effect=AssertionError('no download')).start()
        self.addCleanup(patch.stopall)

    def plan(self, repo, revision, root, **kwargs):
        self.assertTrue(kwargs.get('read_only'))
        self.assertEqual(repo, self.trust['repositories'][0]['repo'])
        self.assertEqual(revision, self.trust['repositories'][0]['revision'])
        return mt.Snapshot(repo, revision, Path(root), [mt.File(
            Path(root) / self.weights.name, len(self.payload), hashlib.sha256(self.payload).hexdigest())])

    def prepare(self, check=lambda: None, plan=None):
        with patch.object(mt, 'hf_plan', side_effect=plan or self.plan), \
                patch.object(mm, 'runtime_issue', return_value=None), \
                patch.object(mt, 'probe_source', side_effect=AssertionError('no content request')), \
                patch.object(mm, '_private_candidate', side_effect=AssertionError('no replacement directory')):
            mt.prepare_asr(mt.Session(lambda *_: None, check), 'local_whisper_mlx')

    def test_legacy_device_change_reuses_official_content_without_download_or_model_mutation(self):
        before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.iterdir()}
        self.assertFalse(mm.is_downloaded('local_whisper_mlx'))
        self.prepare()
        self.assertTrue(mm.is_downloaded('local_whisper_mlx'))
        self.assertEqual(before, {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.iterdir()})
        updated = json.loads(self.receipt.read_text())
        self.assertEqual(updated['content_sha256'][self.weights.name], hashlib.sha256(self.payload).hexdigest())
        self.download.assert_not_called()
        with patch.object(mt, 'hf_plan', side_effect=AssertionError('repeat must be offline')):
            mt.prepare_asr(mt.Session(lambda *_: None, lambda: None), 'local_whisper_mlx')

    def test_same_size_tampering_with_restored_timestamp_does_not_reseal(self):
        before = self.weights.stat()
        self.weights.write_bytes(b'X' + self.payload[1:])
        os.utime(self.weights, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(RuntimeError, '校验'):
            self.prepare()
        self.assertEqual(self.receipt.read_bytes(), self.original)
        self.assertFalse(mm.is_downloaded('local_whisper_mlx'))

    def test_metadata_unavailable_preserves_original_without_starting_download(self):
        with self.assertRaisesRegex(OSError, 'offline'):
            self.prepare(plan=lambda *_a, **_k: (_ for _ in ()).throw(OSError('offline')))
        self.assertEqual(self.receipt.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
