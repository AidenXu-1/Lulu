"""Both Index versions have real, isolated model-management operations."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.synthesis import runtime_packages as rp
from src.core.synthesis import indextts2_weights as iw


class IndexModelManagementTests(unittest.TestCase):
    def test_both_versions_are_managed_without_aliasing_storage(self):
        self.assertIn("local_indextts2_0", rp.TTS_RUNTIMES)
        with tempfile.TemporaryDirectory() as tmp, patch.object(rp, "tts_root", return_value=Path(tmp)):
            self.assertEqual(rp.model_dir("local_indextts2_0"), Path(tmp) / "local_indextts2")
            self.assertEqual(rp.model_dir("local_indextts2"), Path(tmp) / "local_indextts2_5")

    def test_index2_download_plan_has_its_own_codec_and_receipt(self):
        trust = iw._weights_trust(version="2.0")
        repos = [item["repo"] for item in trust["repositories"]]
        self.assertEqual(repos[0], "IndexTeam/IndexTTS-2")
        self.assertIn("amphion/MaskGCT", repos)
        self.assertNotIn("IndexTeam/IndexTTS-2.5", repos)
        with tempfile.TemporaryDirectory() as tmp:
            missing = iw.missing_files(Path(tmp), version="2.0")
            self.assertIn("bpe.model", missing)
            self.assertIn("hf_cache/semantic_codec_model.safetensors", missing)
            self.assertNotIn("codec.pth", missing)


class IndexInstallIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in (("tts_root", self.root / "tts"), ("current_platform", "mac-arm64")):
            p = patch.object(rp, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(rp.mm, "app_data_dir", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def fake_snapshot(repo, **kwargs):
        root = Path(kwargs['local_dir'])
        if repo == 'IndexTeam/IndexTTS-2':
            names = ['config.yaml', 'gpt.pth', 's2mel.pth', 'feat1.pt', 'feat2.pt', 'bpe.model',
                     'wav2vec2bert_stats.pt', 'qwen0.6bemo4-merge/model.safetensors',
                     'qwen0.6bemo4-merge/config.json']
        else:
            names = kwargs['allow_patterns']
        for name in names:
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'fixture')

    def test_legacy_installation_recognized_and_uninstall_preserves_25(self):
        import json
        from src.core.artifact_trust import write_install_receipt
        spec = rp.TTS_RUNTIMES['local_indextts2_0']
        rt = rp.runtime_dir(spec.model_id)
        py = rt / 'python/bin/python3.11'
        py.parent.mkdir(parents=True)
        py.write_bytes(b'fixture')
        lock = rp._trusted_manifest(spec, 'mac-arm64')
        state = {'schema': 1, 'model_id': 'local_indextts2', 'platform': 'mac-arm64',
                 'version': '20260629', 'file': lock['file'], 'size': 397816510, 'sha256': lock['sha256']}
        (rt.parent / '.lulu_runtime.json').write_text(json.dumps(state))
        write_install_receipt('tts-runtime:local_indextts2', rt, lock, self.root / '.artifact-trust')
        with patch('huggingface_hub.snapshot_download', side_effect=self.fake_snapshot):
            rp.install_weights(spec.model_id)
        self.assertEqual(rp.status(spec.model_id), 'ready')
        self.assertTrue(iw.is_installed(rp.weights_dir(spec.model_id), version='2.0'))
        self.assertFalse(iw.is_installed(rp.weights_dir(spec.model_id)))
        other = rp.weights_dir('local_indextts2') / 'keep.wav'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'other-version')
        with patch.object(rp, 'remove_install_receipt') as remove:
            rp.uninstall(spec.model_id, remove_weights=True)
        self.assertEqual([c.args[0] for c in remove.call_args_list],
                         ['tts-runtime:local_indextts2', 'tts-weights:local_indextts2'])
        self.assertFalse(rt.parent.exists())
        self.assertEqual(other.read_bytes(), b'other-version')

    def test_index2_download_failure_preserves_weights_and_retry_cache(self):
        root = rp.weights_dir('local_indextts2_0')
        root.mkdir(parents=True)
        old = root / 'gpt.pth'
        old.write_bytes(b'original')
        def fail(repo, **kwargs):
            if repo == 'amphion/MaskGCT':
                raise OSError('fixture interruption')
            self.fake_snapshot(repo, **kwargs)
        with self.assertRaisesRegex(RuntimeError, '已保留当前进度'):
            iw.install(root, lambda *_: None, lambda: False, version='2.0',
                       _snapshot_download=fail, _receipt_dir=self.root / '.artifact-trust')
        self.assertEqual(old.read_bytes(), b'original')
        self.assertGreater(iw.resumable_download_size(root, version='2.0'), 0)
        iw.install(root, lambda *_: None, lambda: False, version='2.0',
                   _snapshot_download=self.fake_snapshot, _receipt_dir=self.root / '.artifact-trust')
        self.assertTrue(iw.is_installed(root, version='2.0'))
        self.assertFalse(iw.is_installed(root))

    def test_index2_runtime_uses_published_url_and_legacy_state_id(self):
        import shutil
        from tests.test_tts_runtime_packages import TTSRuntimePackagesTests
        fixture = TTSRuntimePackagesTests()
        fixture.root = self.root
        archive, sha = fixture._make_fake_pkg()
        spec = rp.TTS_RUNTIMES['local_indextts2_0']
        manifest = {**rp._trusted_manifest(spec, 'mac-arm64'), 'sha256': sha, 'size': archive.stat().st_size}
        urls = []
        def download(url, target, *_args):
            urls.append(url)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, target)
        with patch.object(rp, '_trusted_manifest', return_value=manifest), patch.object(rp, '_download', side_effect=download):
            rp.install_runtime(spec.model_id)
            self.assertTrue(rp.is_runtime_installed(spec.model_id))
        self.assertIn('/local_indextts2-mac-arm64/', urls[0])
        self.assertNotIn('local_indextts2_0-mac-arm64', urls[0])
        self.assertFalse(rp.runtime_dir('local_indextts2').exists())


if __name__ == "__main__":
    unittest.main()
