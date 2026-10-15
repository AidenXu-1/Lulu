"""Four visible models must target independent managed assets, never another version."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.core.synthesis import runtime_packages as rp, indextts2_weights as iw
from src.core.artifact_trust import write_install_receipt


class IndexVersionManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target, name, value in ((rp, 'tts_root', self.root/'tts'), (rp.mm, 'app_data_dir', self.root)):
            p = patch.object(target, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(rp, 'current_platform', return_value='mac-arm64')
        p.start(); self.addCleanup(p.stop)

    def _download(self, repo, local_dir, **kwargs):
        root = Path(local_dir)
        profile = iw.weight_profile('2.0' if repo == 'IndexTeam/IndexTTS-2' else '2.5')
        if repo.startswith('IndexTeam/'):
            files = [f for f in profile.required_files if not f.startswith('hf_cache/')]
            files += ['qwen0.6bemo4-merge/model.safetensors']
        elif repo == 'facebook/w2v-bert-2.0':
            files = ['model.safetensors', 'config.json', 'preprocessor_config.json']
        else:
            files = kwargs['allow_patterns']
        for name in files:
            p = root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(repo.encode())

    def _install_fixture(self, mid):
        root = rp.model_dir(mid)
        py = rp.runtime_dir(mid)/'python/bin/python3.11'
        py.parent.mkdir(parents=True); py.write_bytes(b'fixture')
        manifest = rp._trusted_manifest(rp.TTS_RUNTIMES[mid], 'mac-arm64')
        # Old 2.0 metadata was written before the separate management ID existed.
        state = {'schema': 1, **manifest}; state.pop('model_id')
        state['model_id'] = 'local_indextts2'
        (root/rp._STATE).write_text(json.dumps(state))
        write_install_receipt(rp._receipt_id('tts-runtime', mid), rp.runtime_dir(mid), manifest, self.root/'.artifact-trust')
        download = Mock(side_effect=self._download)
        iw.install(rp.weights_dir(mid), lambda *_: None, lambda: False,
                   _snapshot_download=download, _receipt_dir=self.root/'.artifact-trust',
                   **rp.index_weights_options(mid))
        return download

    def test_existing_20_and_25_are_ready_and_never_redownload(self):
        for mid, repo in (('local_indextts2_0','IndexTeam/IndexTTS-2'), ('local_indextts2','IndexTeam/IndexTTS-2.5')):
            download = self._install_fixture(mid)
            self.assertEqual(download.call_args_list[0].args[0], repo)
            repos = [c.args[0] for c in download.call_args_list]
            self.assertEqual('amphion/MaskGCT' in repos, mid.endswith('_0'))
            self.assertEqual(rp.status(mid), 'ready')
            with patch.object(rp,'_download',side_effect=AssertionError('duplicate runtime download')), patch.object(iw,'install',side_effect=AssertionError('duplicate weight download')):
                rp.install(mid)
        self.assertNotEqual(rp.model_dir('local_indextts2_0'), rp.model_dir('local_indextts2'))
        self.assertFalse(iw.is_installed(rp.weights_dir('local_indextts2_0')))

    def test_each_uninstall_preserves_other_version_files_and_receipts(self):
        for removed, kept in (('local_indextts2_0','local_indextts2'), ('local_indextts2','local_indextts2_0')):
            for mid in (removed, kept):
                if not rp.is_runtime_installed(mid): self._install_fixture(mid)
            rp.uninstall(removed, remove_weights=True)
            self.assertFalse(rp.model_dir(removed).exists())
            self.assertEqual(rp.status(kept), 'ready')
