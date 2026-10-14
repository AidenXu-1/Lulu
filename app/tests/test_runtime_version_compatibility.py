"""An installed, trusted old runtime survives an App download-lock update."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from src.core import artifact_trust as trust
from src.core.synthesis import runtime_packages as rp


class RuntimeVersionCompatibilityTests(unittest.TestCase):
    def test_published_installations_survive_future_download_lock(self):
        for mid in ('local_voxcpm2', 'local_qwen3_tts', 'local_indextts2_0', 'local_indextts2'):
            with self.subTest(model=mid):
                spec = rp.TTS_RUNTIMES[mid]
                previous = {'local_voxcpm2':'20260628', 'local_qwen3_tts':'20260917.1',
                            'local_indextts2_0':'20260629', 'local_indextts2':'20260920.1'}[mid]
                old = next(m for m in rp._runtime_manifests(spec, 'mac-arm64') if m['version'] == previous)
                runtime = rp.runtime_dir(mid)
                python = runtime/'python/bin/python3.11'
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_bytes(b'isolated interpreter fixture')
                (rp.model_dir(mid)/rp._STATE).write_text(json.dumps(rp._runtime_state(mid,'mac-arm64',old)))
                trust.write_install_receipt(rp._receipt_id('tts-runtime',mid),runtime,old,self.root/'data/.artifact-trust',inventory_paths=[python])
                key = (mid, 'mac-arm64')
                future = replace(trust.TTS_RUNTIME_ARTIFACTS[key], version='future', sha256='f'*64)
                with patch.dict(trust.TTS_RUNTIME_ARTIFACTS, {key: future}):
                    self.assertTrue(rp.is_runtime_installed(mid))
                    self.assertEqual(rp._installed_runtime_manifest(mid), old)
                    with self.assertRaisesRegex(RuntimeError, '可信锁'):
                        rp._validate_manifest_lock(spec, 'mac-arm64', old)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for target,name,value in [(rp,'tts_root',lambda:self.root/'tts'),(rp.mm,'app_data_dir',lambda:self.root/'data'),(rp,'current_platform',lambda:'mac-arm64')]:
            context=patch.object(target,name,value);context.start();self.addCleanup(context.stop)
        self.mid='local_indextts2'
        self.spec=rp.TTS_RUNTIMES[self.mid]
        self.legacy=rp._runtime_manifests(self.spec,'mac-arm64')[1]
        self.runtime=rp.runtime_dir(self.mid)
        python=self.runtime/'python/bin/python3.11';python.parent.mkdir(parents=True)
        python.write_bytes(b'isolated interpreter fixture')
        self.python=python
        (rp.model_dir(self.mid)/rp._STATE).write_text(json.dumps(rp._runtime_state(self.mid,'mac-arm64',self.legacy)))
        trust.write_install_receipt(rp._receipt_id('tts-runtime',self.mid),self.runtime,self.legacy,self.root/'data/.artifact-trust',inventory_paths=[python])

    def test_valid_legacy_runtime_is_reused_without_downloading(self):
        self.assertTrue(rp.is_runtime_installed(self.mid))
        with patch.object(rp,'_download',side_effect=AssertionError('old installation must be reused')):
            rp.install_runtime(self.mid)
        self.assertEqual(rp._installed_runtime_manifest(self.mid),self.legacy)

    def test_changed_old_runtime_is_not_accepted(self):
        self.python.write_bytes(b'damaged')
        self.assertFalse(rp.is_runtime_installed(self.mid))

    def test_legacy_archive_is_not_a_valid_new_download(self):
        with self.assertRaisesRegex(RuntimeError,'可信锁'):
            rp._validate_manifest_lock(self.spec,'mac-arm64',self.legacy)

    def test_unknown_version_is_not_accepted_even_with_receipt(self):
        unknown={**self.legacy,'version':'unknown'}
        (rp.model_dir(self.mid)/rp._STATE).write_text(json.dumps(rp._runtime_state(self.mid,'mac-arm64',unknown)))
        trust.write_install_receipt(rp._receipt_id('tts-runtime',self.mid),self.runtime,unknown,self.root/'data/.artifact-trust',inventory_paths=[self.python])
        self.assertFalse(rp.is_runtime_installed(self.mid))
