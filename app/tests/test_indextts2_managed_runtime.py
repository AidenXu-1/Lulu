import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from src.core.synthesis import indextts2_worker
from src.core.synthesis.indextts2_readiness import MODEL_REQUIRED, check_runtime


class IndexTTS2ManagedReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.model = self.root / "weights"

    def _make_runtime(self):
        py = self.runtime / "python" / "bin" / "python3.11"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(py, 0o755)
        (self.runtime / "python" / "nltk_data").mkdir(parents=True)
        cli = self.runtime / "vendor" / "index-tts" / "indextts" / "cli_v2.py"
        cli.parent.mkdir(parents=True)
        cli.write_text("# fake cli\n", encoding="utf-8")

    def _make_model(self, *, include_config: bool = True):
        for rel in MODEL_REQUIRED:
            if rel == "config.yaml" and not include_config:
                continue
            p = self.model / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    def test_runtime_not_installed_reports_runtime_missing(self):
        self._make_model()
        status = check_runtime(self.runtime, self.model, runtime_source="test", model_source="test")
        self.assertFalse(status.runtime_ready)
        self.assertTrue(status.model_ready)
        self.assertIn("runtime", status.missing_items)

    def test_missing_weights_reports_model_missing(self):
        self._make_runtime()
        status = check_runtime(self.runtime, self.model, runtime_source="test", model_source="test")
        self.assertTrue(status.runtime_ready)
        self.assertFalse(status.model_ready)
        self.assertIn("model", status.missing_items)

    def test_missing_config_blocks_config_only(self):
        self._make_runtime()
        self._make_model(include_config=False)
        status = check_runtime(self.runtime, self.model, runtime_source="test", model_source="test")
        self.assertTrue(status.runtime_ready)
        self.assertFalse(status.model_ready)
        self.assertFalse(status.config_ready)
        self.assertIn("model/config.yaml", status.missing_items)

    def test_files_without_valid_install_receipts_are_not_ready(self):
        self._make_runtime()
        self._make_model()
        with patch("src.core.synthesis.indextts2_readiness.rp.status", return_value="needs_repair"), \
             patch("src.core.synthesis.indextts2_readiness.rp.is_runtime_installed", return_value=False), \
             patch("src.core.synthesis.indextts2_readiness.rp.is_weights_installed", return_value=False):
            status = check_runtime(self.runtime, self.model)
        self.assertFalse(status.ready)
        self.assertFalse(status.runtime_ready)
        self.assertFalse(status.model_ready)

    def test_official_25_requires_its_codec_and_tokenizer_not_legacy_files(self):
        self._make_runtime()
        entry = self.runtime / "vendor/index-tts/indextts/infer_v2_5.py"
        entry.write_text("# official 2.5 entry\n")
        self._make_model()
        (self.model / "config.yaml").write_text("version: 2.5\n")
        (self.model / "bpe.model").unlink()
        (self.model / "hf_cache/semantic_codec_model.safetensors").unlink()
        for name in ("codec.pth", "multilingual_zh_ja_yue_char_del.tiktoken"):
            (self.model / name).write_bytes(b"fixture")
        status = check_runtime(self.runtime, self.model, runtime_source="test", model_source="test")
        self.assertTrue(status.ready, status.missing_items)
        self.assertEqual(status.model_version, "2.5")
        (self.model / "codec.pth").unlink()
        self.assertFalse(check_runtime(self.runtime, self.model, runtime_source="test", model_source="test").ready)


class IndexTTS2ManagedWorkerFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.vendor = self.runtime / "vendor" / "index-tts"
        self.vendor.mkdir(parents=True)
        self.py = self.runtime / "python" / "bin" / "python3.11"
        self.py.parent.mkdir(parents=True)
        self.model = self.root / "model"
        self.ref = self.root / "ref.wav"
        sf.write(str(self.ref), np.zeros(1600, dtype="float32"), 16000)

    def _run(self, *, model_dir: Path | None = None, runtime_python: Path | None = None):
        payload = {
            "job_id": "t",
            "runtime_dir": str(self.runtime),
            "runtime_python": str(runtime_python or self.py),
            "vendor_dir": str(self.vendor),
            "model_dir": str(model_dir or self.model),
            "voice": {"reference_audio_path": str(self.ref)},
            "output": {"temp_wav_path": str(self.root / "temp.wav")},
            "text": "你好世界",
            "emotion": {"control_mode": "follow"},
            "device": "cpu",
            "options": {"timeout_seconds": 1},
        }
        req = self.root / "req.json"
        req.write_text(json.dumps(payload), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = indextts2_worker.run_request(req)
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return rc, events

    def test_missing_runtime_python_points_to_model_config_page(self):
        self.model.mkdir()
        rc, events = self._run()
        self.assertNotEqual(rc, 0)
        err = events[-1]
        self.assertEqual(err["code"], "RUNTIME_NOT_READY")
        self.assertIn("AI 大模型配置", err["message"])

    def test_missing_model_points_to_model_config_page(self):
        self.py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.py, 0o755)
        rc, events = self._run()
        self.assertNotEqual(rc, 0)
        err = events[-1]
        self.assertEqual(err["code"], "MODEL_NOT_READY")
        self.assertIn("AI 大模型配置", err["message"])

    def test_model_loading_forces_offline_even_if_parent_disables_it(self):
        self.py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.model.mkdir()
        observed = {}

        def inspect_environment(**kwargs):
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
                observed[key] = os.environ.get(key)
            raise RuntimeError("isolated model boundary")

        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"}), \
             patch.object(indextts2_worker, "_create_tts", side_effect=inspect_environment):
            self._run()
        self.assertEqual(observed, {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"})


if __name__ == "__main__":
    unittest.main()
