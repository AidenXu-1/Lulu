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

    def test_removal_worker_finishes_before_owner_can_be_destroyed(self):
        model = self.root / "asr/models/local_sensevoice"
        model.mkdir(parents=True)
        (model / "weights").write_bytes(b"fixture")
        worker = ModelRemoveWorker("local_sensevoice")
        outcomes = []
        worker.settled.connect(outcomes.append)
        with patch.object(mm, "models_root", return_value=self.root / "asr"):
            worker.start()
            self.assertTrue(worker.wait(3000))
            self.app.processEvents()
        self.assertEqual(outcomes, [""])
        self.assertFalse(model.exists())

    def test_runtime_import_failure_is_offline_and_does_not_pass_health_check(self):
        child = Mock()
        child.poll.return_value = 1
        child.returncode = 1
        with patch.object(rp.subprocess, "Popen", return_value=child) as start:
            with self.assertRaisesRegex(RuntimeError, "无法正常加载模型依赖"):
                rp.check_runtime_health("local_voxcpm2", self.root)
        env = start.call_args.kwargs["env"]
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")
        self.assertNotIn(str(self.root), env["NUMBA_CACHE_DIR"])
        self.assertFalse(Path(env["NUMBA_CACHE_DIR"]).parent.exists())
        self.assertIn("socket.socket.connect = offline", start.call_args.args[0][-1])

    def test_runtime_probe_cancellation_reaps_child(self):
        child = Mock(pid=12345)
        child.poll.return_value = None
        with patch.object(rp.subprocess, "Popen", return_value=child), patch.object(rp.os, "killpg") as kill:
            with self.assertRaises(rp.CancelledError):
                rp.check_runtime_health("local_voxcpm2", self.root, lambda: True)
        kill.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_qwen_preflight_failure_or_pause_does_not_leave_a_phantom_download(self):
        from src.core.synthesis import qwen3_deployment as deployment
        for failure, expected in ((RuntimeError("space"), "failed"), (rp.CancelledError(), "paused")):
            deployment.request(["Base"])
            worker = QwenFeatureWorker(operation="install")
            with patch.object(mt, "prepare_tts", side_effect=failure):
                worker.run()
            state = deployment.read_state()["features"]["Base"]
            self.assertEqual(state["state"], expected)
            self.assertFalse(deployment._live_download(state))

class InstallRepairStateTests(ControllerCase):
    def test_repair_state_takes_priority_over_untrusted_partial_bytes(self):
        with self.public_state(status='needs_repair',partial_install_bytes=4_000_000_000):value=self.c._read_state()
        model=next(x for x in value['localModels']['voice'] if x['id']=='voxcpm2')
        self.assertTrue(model['repair']);self.assertFalse(model['ready']);self.assertEqual(model['resumable'],4_000_000_000)


if __name__ == "__main__":
    unittest.main()
