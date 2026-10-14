import builtins
from contextlib import nullcontext
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import artifact_trust, models_manager as mm
from src.core import model_transfer as transfer
from src.core.artifact_trust import HF_MODEL_REVISIONS, MODELSCOPE_MODEL_REVISIONS, model_revision


class ModelArtifactTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hf_payload = b"complete"
        def plan(repo, revision, root, **_kwargs):
            entry = transfer.File(Path(root) / "weights.safetensors", len(self.hf_payload),
                                  hashlib.sha256(self.hf_payload).hexdigest())
            return transfer.Snapshot(repo, revision, Path(root), [entry])
        for p in (patch.object(transfer, "hf_plan", side_effect=plan),
                  patch.object(transfer, "incremental_hf_transport", nullcontext),
                  patch.object(transfer, "probe_source")):
            p.start()
            self.addCleanup(p.stop)

    def test_every_registered_model_and_submodel_has_immutable_revision(self):
        expected = {
            "iic/SenseVoiceSmall": "modelscope",
            "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch": "modelscope",
            "Qwen/Qwen3-ASR-0.6B": "modelscope",
            "Qwen/Qwen3-ForcedAligner-0.6B": "modelscope",
            "mlx-community/whisper-large-v3-turbo": "hf",
        }
        for repo, source in expected.items():
            with self.subTest(repo=repo):
                revision = model_revision(repo, source)
                self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_unknown_model_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "可信版本锁"):
            model_revision("attacker/replaced-model", "hf")

    def _fake_modelscope_modules(self, *, endpoint="https://www.modelscope.cn", files=None):
        payload = b"trusted-model-bytes"
        repo_files = files if files is not None else [{
            "Type": "blob",
            "Path": "model.pt",
            "Name": "model.pt",
            "Size": len(payload),
            "Sha256": hashlib.sha256(payload).hexdigest(),
            "Revision": "file-commit",
        }]

        class Api:
            def __init__(self):
                self.endpoint = endpoint

            def get_endpoint_for_read(self, **_kwargs):
                self.fail_if_called = True
                raise AssertionError("endpoint selection must not make a pre-validation request")

            def get_cookies(self):
                return None

            def get_model_files(self, **kwargs):
                self.requested_revision = kwargs["revision"]
                return repo_files

        class Config:
            @staticmethod
            def get_user_agent(user_agent=None):
                return "test-agent"

        class Cache:
            pass

        def cache_factory(_repo, *, local_dir, repo_type):
            temp = Path(local_dir) / ".tmp"
            temp.mkdir(parents=True, exist_ok=True)
            return str(temp), Cache()

        def download_files(repo_entries, _cache, _temp, _repo, _api, *_args, **kwargs):
            root = Path(kwargs.pop("_test_root", self.root))
            # local_dir 本身不在此私有 SDK 接口参数中；测试候选固定为 self.root。
            for entry in repo_entries:
                target = root / entry["Path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)

        api_module = types.ModuleType("modelscope.hub.api")
        api_module.HubApi = Api
        api_module.ModelScopeConfig = Config
        file_module = types.ModuleType("modelscope.hub.file_download")
        file_module.create_temporary_directory_and_cache = cache_factory
        snapshot_module = types.ModuleType("modelscope.hub.snapshot_download")
        snapshot_module._download_file_lists = download_files
        constant_module = types.ModuleType("modelscope.utils.constant")
        constant_module.REPO_TYPE_MODEL = "model"
        return {
            "modelscope.hub.api": api_module,
            "modelscope.hub.file_download": file_module,
            "modelscope.hub.snapshot_download": snapshot_module,
            "modelscope.utils.constant": constant_module,
        }

    def test_modelscope_commit_adapter_downloads_fixed_commit_without_branch_validation(self):
        repo = "iic/SenseVoiceSmall"
        revision = MODELSCOPE_MODEL_REVISIONS[repo]
        with patch.dict(sys.modules, self._fake_modelscope_modules(), clear=False):
            result = artifact_trust.modelscope_snapshot_download(
                repo,
                revision=revision,
                local_dir=str(self.root),
            )

        self.assertEqual(Path(result), self.root)
        self.assertEqual((self.root / "model.pt").read_bytes(), b"trusted-model-bytes")

    def test_modelscope_commit_adapter_rejects_untrusted_endpoint_before_file_download(self):
        repo = "iic/SenseVoiceSmall"
        revision = MODELSCOPE_MODEL_REVISIONS[repo]
        with patch.dict(
            sys.modules,
            self._fake_modelscope_modules(endpoint="https://attacker.example"),
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "官方可信范围"):
                artifact_trust.modelscope_snapshot_download(
                    repo,
                    revision=revision,
                    local_dir=str(self.root),
                )

        self.assertFalse((self.root / "model.pt").exists())

    def test_modelscope_commit_adapter_rejects_unhashed_or_unsafe_manifest(self):
        repo = "iic/SenseVoiceSmall"
        revision = MODELSCOPE_MODEL_REVISIONS[repo]
        bad_lists = (
            [{"Type": "blob", "Path": "../escape", "Name": "escape", "Size": 1, "Sha256": "0" * 64}],
            [{"Type": "blob", "Path": "model.pt", "Name": "model.pt", "Size": 1, "Sha256": ""}],
        )
        for files in bad_lists:
            with self.subTest(files=files), patch.dict(
                sys.modules,
                self._fake_modelscope_modules(files=files),
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    artifact_trust.modelscope_snapshot_download(
                        repo,
                        revision=revision,
                        local_dir=str(self.root),
                    )

    def test_sensevoice_downloads_use_modelscope_commit_locks(self):
        calls = []

        def snapshot(repo, **kwargs):
            calls.append((repo, kwargs))
            target = Path(kwargs["local_dir"])
            target.mkdir(parents=True, exist_ok=True)
            (target / "model.pt").write_bytes(repo.encode("utf-8"))

        with patch.object(mm, "modelscope_snapshot_download", side_effect=snapshot), patch.object(
            mm, "managed_dir", return_value=self.root
        ), patch.object(mm, "app_data_dir", return_value=self.root / "appdata"):
            mm.download_blocking("local_sensevoice")
            self.assertTrue(mm.is_downloaded("local_sensevoice"))

        self.assertEqual(len(calls), 2)
        for repo, kwargs in calls:
            self.assertEqual(kwargs["revision"], MODELSCOPE_MODEL_REVISIONS[repo])

    def test_sensevoice_does_not_use_branch_only_modelscope_snapshot_entrypoint(self):
        calls = []

        def commit_adapter(repo, **kwargs):
            calls.append((repo, kwargs))
            target = Path(kwargs["local_dir"])
            target.mkdir(parents=True, exist_ok=True)
            (target / "model.pt").write_bytes(repo.encode("utf-8"))

        def branch_only_snapshot(repo, **kwargs):
            raise RuntimeError(
                f"The model: {repo} has no revision: {kwargs.get('revision')} !"
            )

        fake = types.SimpleNamespace(snapshot_download=branch_only_snapshot)
        with patch.object(
            mm,
            "modelscope_snapshot_download",
            side_effect=commit_adapter,
            create=True,
        ), patch.object(mm, "managed_dir", return_value=self.root), patch.object(
            mm, "app_data_dir", return_value=self.root / "appdata"
        ), patch.dict(sys.modules, {"modelscope": fake}):
            mm.download_blocking("local_sensevoice")

        self.assertEqual(
            [repo for repo, _kwargs in calls],
            [
                "iic/SenseVoiceSmall",
                "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            ],
        )
        for repo, kwargs in calls:
            self.assertEqual(kwargs["revision"], MODELSCOPE_MODEL_REVISIONS[repo])

    def test_whisper_download_uses_huggingface_commit_lock(self):
        self.hf_payload = b"mlx-community/whisper-large-v3-turbo"
        calls = []

        def snapshot(repo, **kwargs):
            calls.append((repo, kwargs))
            target = Path(kwargs["local_dir"])
            target.mkdir(parents=True, exist_ok=True)
            (target / "weights.safetensors").write_bytes(repo.encode("utf-8"))

        fake = types.SimpleNamespace(snapshot_download=snapshot)
        real_import = builtins.__import__

        def import_without_modelscope(name, *args, **kwargs):
            if name == "modelscope" or name.startswith("modelscope."):
                self.fail("Whisper download must not import ModelScope/PyTorch")
            return real_import(name, *args, **kwargs)

        with patch.object(mm, "managed_dir", return_value=self.root), patch.object(mm, "app_data_dir", return_value=self.root / "appdata"), patch.dict(sys.modules, {"huggingface_hub": fake}), patch("builtins.__import__", side_effect=import_without_modelscope):
            mm.download_blocking("local_whisper_mlx")
            self.assertTrue(mm.is_downloaded("local_whisper_mlx"))
            (self.root / "local_whisper_mlx" / "weights.safetensors").write_bytes(b"tampered")
            self.assertFalse(mm.is_downloaded("local_whisper_mlx"))

        self.assertEqual(calls[0][1]["revision"], HF_MODEL_REVISIONS[calls[0][0]])

    def test_qwen_and_aligner_use_modelscope_commit_locks(self):
        calls = []

        def snapshot(repo, **kwargs):
            calls.append((repo, kwargs))
            Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)

        with patch.object(mm, "modelscope_snapshot_download", side_effect=snapshot), patch.object(
            mm, "managed_dir", return_value=self.root
        ), patch.object(mm, "app_data_dir", return_value=self.root / "appdata"):
            mm.download_blocking("local_qwen3")
            self.assertTrue(mm.is_downloaded("local_qwen3"))

        self.assertEqual({repo for repo, _kwargs in calls}, {
            "Qwen/Qwen3-ASR-0.6B",
            "Qwen/Qwen3-ForcedAligner-0.6B",
        })
        for repo, kwargs in calls:
            self.assertEqual(kwargs["revision"], MODELSCOPE_MODEL_REVISIONS[repo])

    def test_existing_files_and_legacy_markers_without_receipt_are_not_trusted(self):
        with patch.object(mm, "managed_dir", return_value=self.root), patch.object(mm, "app_data_dir", return_value=self.root / "appdata"):
            sensevoice = self.root / "local_sensevoice"
            (sensevoice / "SenseVoiceSmall").mkdir(parents=True)
            (sensevoice / "SenseVoiceSmall" / "model.pt").write_bytes(b"placeholder")
            (sensevoice / "fsmn-vad").mkdir()
            (sensevoice / ".lulu_done").write_text("ok", encoding="utf-8")
            whisper = self.root / "local_whisper_mlx"
            whisper.mkdir()
            (whisper / ".lulu_done").write_text("ok", encoding="utf-8")

            self.assertFalse(mm.is_downloaded("local_sensevoice"))
            self.assertFalse(mm.is_downloaded("local_whisper_mlx"))
            self.assertTrue(mm.needs_repair("local_sensevoice"))
            self.assertTrue(mm.needs_repair("local_whisper_mlx"))

    def test_failed_secure_repair_preserves_existing_model_directory(self):
        dest = self.root / "local_whisper_mlx"
        dest.mkdir()
        sentinel = dest / "existing.bin"
        sentinel.write_bytes(b"keep-existing")

        def snapshot(_repo, **kwargs):
            candidate = Path(kwargs["local_dir"])
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / "partial.bin").write_bytes(b"partial")
            raise OSError("network down")

        fake = types.SimpleNamespace(snapshot_download=snapshot)
        with patch.object(mm, "managed_dir", return_value=self.root), patch.object(mm, "app_data_dir", return_value=self.root / "appdata"), patch.dict(sys.modules, {"huggingface_hub": fake}):
            with self.assertRaisesRegex(OSError, "network down"):
                mm.download_blocking("local_whisper_mlx")

        self.assertEqual(sentinel.read_bytes(), b"keep-existing")
        self.assertEqual(list(self.root.glob(".local_whisper_mlx-install-*")), [])

    def test_interrupted_asr_download_reuses_partial_candidate_on_retry(self):
        calls = 0

        def snapshot(_repo, **kwargs):
            nonlocal calls
            calls += 1
            candidate = Path(kwargs["local_dir"])
            candidate.mkdir(parents=True, exist_ok=True)
            partial = candidate / "partial.bin"
            if calls == 1:
                partial.write_bytes(b"resume-me")
                raise OSError("network down")
            self.assertEqual(partial.read_bytes(), b"resume-me")
            (candidate / "weights.safetensors").write_bytes(b"complete")

        fake = types.SimpleNamespace(snapshot_download=snapshot)
        with patch.object(mm, "managed_dir", return_value=self.root), patch.object(
            mm, "app_data_dir", return_value=self.root / "appdata"
        ), patch.dict(sys.modules, {"huggingface_hub": fake}):
            with self.assertRaisesRegex(OSError, "network down"):
                mm.download_blocking("local_whisper_mlx")
            mm.download_blocking("local_whisper_mlx")
            self.assertTrue(mm.is_downloaded("local_whisper_mlx"))

        self.assertEqual(calls, 2)

    def test_cancelled_asr_download_stops_before_network(self):
        fake = types.SimpleNamespace(
            snapshot_download=lambda *_args, **_kwargs: self.fail("network should not start")
        )
        with patch.object(mm, "managed_dir", return_value=self.root), patch.object(
            mm, "app_data_dir", return_value=self.root / "appdata"
        ), patch.dict(sys.modules, {"huggingface_hub": fake}):
            with self.assertRaises(mm.DownloadCancelled):
                mm.download_blocking(
                    "local_whisper_mlx",
                    progress=lambda _fraction: None,
                    is_cancelled=lambda: True,
                )


if __name__ == "__main__":
    unittest.main()
