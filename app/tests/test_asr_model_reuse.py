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

    def test_cancel_preserves_original(self):
        def cancel():
            raise mm.DownloadCancelled()
        with self.assertRaises(mm.DownloadCancelled):
            self.prepare(check=cancel)
        self.assertEqual(self.receipt.read_bytes(), self.original)

    def test_unrecorded_file_is_not_adopted(self):
        (self.root / 'loader.py').write_text('unexpected')
        with self.assertRaisesRegex(RuntimeError, '清单'):
            self.prepare()
        self.assertEqual(self.receipt.read_bytes(), self.original)

    def test_unexpected_recorded_file_is_not_granted_trust(self):
        extra = self.root / 'loader.py'
        extra.write_text('unexpected')
        data = json.loads(self.original)
        data['files'].append(dict(at._file_entry(self.root, extra), device=data['root_device']))
        self.receipt.write_text(json.dumps(data))
        original = self.receipt.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '清单'):
            self.prepare()
        self.assertEqual(self.receipt.read_bytes(), original)

    def test_change_during_official_verification_does_not_reseal(self):
        original = mt.File.valid
        def race(file, check):
            result = original(file, check)
            self.weights.write_bytes(b'X' + self.payload[1:])
            return result
        with patch.object(mt.File, 'valid', race), self.assertRaisesRegex(RuntimeError, '变化'):
            self.prepare()
        self.assertEqual(self.receipt.read_bytes(), self.original)

    def test_read_only_hf_plan_does_not_create_sdk_cache_in_model_directory(self):
        revision = self.trust['repositories'][0]['revision']
        with patch.object(mt, '_metadata', return_value=[{
                'name': 'weights.safetensors', 'size': len(self.payload),
                'digest': hashlib.sha256(self.payload).hexdigest(), 'lfs': True}]), \
                patch('huggingface_hub._local_folder.get_local_download_paths',
                      side_effect=AssertionError('must not create SDK cache')):
            result = mt.hf_plan(self.trust['repositories'][0]['repo'], revision, self.root, read_only=True)
        self.assertEqual(result.files[0].path, self.weights)
        self.assertFalse((self.root / '.cache').exists())

    def test_wrong_revision_does_not_enter_legacy_reuse(self):
        from src.core.asr_model_reuse import recover_legacy_asr_receipt
        data = json.loads(self.original)
        data['trust']['repositories'][0]['revision'] = 'f' * 40
        self.receipt.write_text(json.dumps(data))
        original = self.receipt.read_bytes()
        with patch.object(mt, 'hf_plan', side_effect=AssertionError('must not query other version')):
            self.assertFalse(recover_legacy_asr_receipt(mt.Session(lambda *_: None, lambda: None),
                                                       'local_whisper_mlx'))
        self.assertEqual(self.receipt.read_bytes(), original)

    def test_missing_or_linked_files_do_not_enter_legacy_reuse(self):
        from src.core.asr_model_reuse import recover_legacy_asr_receipt
        self.weights.unlink()
        for linked in (False, True):
            if linked:
                self.weights.symlink_to(self.receipt)
            with patch.object(mt, 'hf_plan', side_effect=AssertionError('invalid model identity')):
                self.assertFalse(recover_legacy_asr_receipt(mt.Session(lambda *_: None, lambda: None),
                                                           'local_whisper_mlx'))
            self.assertEqual(self.receipt.read_bytes(), self.original)

    def test_receipt_write_failure_preserves_original(self):
        with patch.object(at, 'restore_install_receipt', side_effect=OSError('read-only receipt')), \
                self.assertRaisesRegex(OSError, 'read-only'):
            self.prepare()
        self.assertEqual(self.receipt.read_bytes(), self.original)

    def test_git_sha1_manifest_also_creates_sha256_for_future_offline_recovery(self):
        def git_plan(repo, revision, root, **kwargs):
            value = self.plan(repo, revision, root, **kwargs)
            value.files[0].algorithm = 'git-sha1'
            value.files[0].digest = hashlib.sha1(f'blob {len(self.payload)}\0'.encode() + self.payload).hexdigest()
            return value
        self.prepare(plan=git_plan)
        updated = json.loads(self.receipt.read_text())
        self.assertEqual(updated['content_sha256'][self.weights.name], hashlib.sha256(self.payload).hexdigest())

    def test_sensevoice_requires_official_hashes_for_both_submodels(self):
        root = self.base / 'models' / 'local_sensevoice'
        expected = {}
        for sub, repo in mm._SENSEVOICE_SUBMODELS.items():
            folder = root / sub
            folder.mkdir(parents=True)
            (folder / 'model.pt').write_bytes(self.payload)
            (folder / '.msc').write_bytes(b'not-unpickled-sdk-cache')
            expected[repo] = folder
        (root / '.lulu_done').write_text('ok')
        trust = mm._trust_options('local_sensevoice')[0]
        receipt = at.write_install_receipt(mm._artifact_id('local_sensevoice'), root, trust, self.receipts)
        data = json.loads(receipt.read_text())
        del data['content_sha256']
        data.pop('root_volume_uuid', None)  # Legacy receipt before stable volume IDs.
        data['root_device'] += 1
        for entry in data['files']:
            entry['device'] += 1
        receipt.write_text(json.dumps(data))
        seen = []
        def ms(repo, revision, destination):
            self.assertEqual(destination, expected[repo])
            self.assertEqual(revision, at.model_revision(repo, 'modelscope'))
            seen.append(repo)
            # Older ModelScope SDKs omit this Git-only bookkeeping file.
            return mt.Snapshot(repo, revision, destination, [mt.File(
                destination / 'model.pt', len(self.payload), hashlib.sha256(self.payload).hexdigest()),
                mt.File(destination / '.gitattributes', 1, hashlib.sha256(b'x').hexdigest())], 'modelscope')
        with patch.object(mt, 'modelscope_plan', side_effect=ms), \
                patch.object(mt, 'probe_source', side_effect=AssertionError('no content request')), \
                patch.object(mm, '_private_candidate', side_effect=AssertionError('no replacement directory')), \
                patch('pickle.load', side_effect=AssertionError('no pickle')):
            mt.prepare_asr(mt.Session(lambda *_: None, lambda: None), 'local_sensevoice')
        self.assertEqual(set(seen), set(mm._SENSEVOICE_SUBMODELS.values()))
        self.assertTrue(mm.is_downloaded('local_sensevoice'))


if __name__ == '__main__':
    unittest.main()
