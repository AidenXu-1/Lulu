"""Real worker/installer integration with small files and a fake transport."""
import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from src.core import model_transfer as mt, models_manager as mm
from src.core.synthesis import runtime_packages as rp
from src.ui.worker import TTSRuntimeInstallWorker, ModelRemoveWorker, QwenFeatureWorker
from tests.model_settings_fixtures import ControllerCase


class InstallSessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(patch.object(mm, "app_data_dir", return_value=self.root / "appdata"))
        self.scope.enter_context(patch.object(rp.local_models, "model_root", return_value=self.root / "models"))
        self.scope.enter_context(patch.object(rp, "tts_root", return_value=self.root / "tts"))
        self.scope.enter_context(patch.object(rp, "install_runtime"))
        self.scope.enter_context(patch.object(rp, "is_runtime_installed", return_value=True))
        self.scope.enter_context(patch.object(mt, "probe_source"))
        self.payload = b"complete verified model"
        self.spec = rp.TTS_RUNTIMES["local_voxcpm2"]
        self.revision = rp.HF_MODEL_REVISIONS[self.spec.weights_repo]
        self.cache = rp.local_models.model_dir() / f".weights-download-{self.revision}"
        self.file = self.cache / "models--openbmb--VoxCPM2" / "snapshots" / self.revision / "weights.bin"
        self.partial = self.cache / "partial.incomplete"
        def prepare(session, *_args, **_kwargs):
            entry = mt.File(self.file, len(self.payload), hashlib.sha256(self.payload).hexdigest(), partials=(self.partial,))
            session.add(mt.Snapshot(self.spec.weights_repo, self.revision, self.cache, [entry], local_dir=False))
        self.scope.enter_context(patch.object(mt, "prepare_tts", side_effect=prepare))

    def run_install(self, transport):
        worker = TTSRuntimeInstallWorker(self.spec.model_id)
        outcomes = []
        worker.finished_ok.connect(lambda: outcomes.append("success"))
        worker.cancelled.connect(lambda: outcomes.append("paused"))
        worker.failed.connect(outcomes.append)
        with patch.object(rp, "_snapshot_download_process", side_effect=transport):
            worker.run()
        return outcomes

    def test_pause_continue_uses_same_candidate_and_verifies_complete_file(self):
        def pause(*_args, **_kwargs):
            self.partial.parent.mkdir(parents=True, exist_ok=True)
            self.partial.write_bytes(self.payload[:6])
            raise rp.CancelledError()
        self.assertEqual(self.run_install(pause), ["paused"])
        self.assertEqual(self.partial.read_bytes(), self.payload[:6])
        def finish(*_args, **_kwargs):
            self.assertEqual(self.partial.read_bytes(), self.payload[:6])
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_bytes(self.partial.read_bytes() + self.payload[6:])
        self.assertEqual(self.run_install(finish), ["success"])
        self.assertTrue(rp.local_models.is_downloaded())
        self.assertFalse(self.cache.exists())

    def test_corrupt_transfer_keeps_old_cache_and_does_not_emit_success(self):
        old = rp.local_models.hf_cache_dir()
        old.mkdir(parents=True)
        sentinel = old / "old.bin"
        sentinel.write_bytes(b"old preserved")
        def corrupt(*_args, **_kwargs):
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_bytes(b"X" + self.payload[1:])
        outcome = self.run_install(corrupt)
        self.assertEqual(len(outcome), 1)
        self.assertIn("校验未通过", outcome[0])
        self.assertEqual(sentinel.read_bytes(), b"old preserved")
        self.assertFalse(rp.local_models.is_downloaded())


if __name__ == "__main__":
    unittest.main()
