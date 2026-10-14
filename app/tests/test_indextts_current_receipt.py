import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import artifact_trust as at
from src.core.synthesis import runtime_packages as rp, indextts2_readiness as readiness
from src.core.synthesis import indextts2_weights as iw


class CurrentIndexReceiptTests(unittest.TestCase):
    def test_install_page_and_generation_accept_the_same_current_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(rp, 'tts_root', return_value=root/'tts'), \
                 patch.object(rp.mm, 'app_data_dir', return_value=root), \
                 patch.object(rp, 'current_platform', return_value='mac-arm64'):
                mid = 'local_indextts2_0'
                rt, md = rp.runtime_dir(mid), rp.weights_dir(mid)
                for rel in readiness.RUNTIME_REQUIRED:
                    path = rt/rel
                    if rel in ('python', 'python/nltk_data'):
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True);path.write_text('fixture')
                py = rt/'python/bin/python3.11';py.parent.mkdir();py.write_text('fixture')
                for rel in readiness.MODEL_REQUIRED:
                    path = md/rel;path.parent.mkdir(parents=True, exist_ok=True);path.write_text('fixture')
                (md/'config.yaml').write_text('version: 2.0\n')
                manifest = rp._trusted_manifest(rp.TTS_RUNTIMES[mid], 'mac-arm64')
                (rp.model_dir(mid)/rp._STATE).write_text(json.dumps(rp._runtime_state(mid, 'mac-arm64', manifest)))
                at.write_install_receipt(rp._receipt_id('tts-runtime',mid), rt, manifest, root/'.artifact-trust')
                at.write_install_receipt('tts-weights:local_indextts2', md, iw._weights_trust('2.0'), root/'.artifact-trust')
                self.assertTrue(rp.is_runtime_installed(mid))
                status = readiness.check_runtime(expected_version='2.0')
                self.assertTrue(status.ready, status.detail_redacted)
                # Replaced interpreter must be rejected by both entry points.
                py.write_text('replaced')
                self.assertFalse(rp.is_runtime_installed(mid))
                self.assertFalse(readiness.check_runtime(expected_version='2.0').ready)
