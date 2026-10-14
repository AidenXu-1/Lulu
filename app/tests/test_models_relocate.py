import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core import models_manager as mm


class ModelsRelocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 把 models_root 指到临时旧根
        self.old_root = self.root / "old"
        self._orig = mm.models_root
        self._orig_app_data = mm.app_data_dir
        mm.models_root = lambda: self.old_root  # type: ignore[assignment]
        mm.app_data_dir = lambda: self.root / "appdata"  # type: ignore[assignment]

    def tearDown(self):
        mm.models_root = self._orig  # type: ignore[assignment]
        mm.app_data_dir = self._orig_app_data  # type: ignore[assignment]
        self.tmp.cleanup()

    def _mark_trusted(self, model_id: str, root: Path, option: int = 0) -> None:
        mm.write_install_receipt(
            mm._artifact_id(model_id),
            root,
            mm._trust_options(model_id)[option],
            mm._receipt_base(),
        )

    def test_relocate_moves_models_and_tts(self):
        # 造已装模型 + tts 运行时 + 不该搬的素材库
        (self.old_root / "models" / "local_sensevoice").mkdir(parents=True)
        (self.old_root / "models" / "local_sensevoice" / "x").write_text("m")
        (self.old_root / "tts" / "local_voxcpm2" / "runtime").mkdir(parents=True)
        (self.old_root / "tts" / "local_voxcpm2" / "runtime" / "y").write_text("t")
        (self.old_root / "library").mkdir(parents=True)
        (self.old_root / "library" / "a.json").write_text("{}")

        new_root = self.root / "外置盘 X" / "Lulu模型"
        mm.relocate_models(new_root)

        # models/ 与 tts/ 已搬到新根
        self.assertTrue((new_root / "models" / "local_sensevoice" / "x").exists())
        self.assertTrue((new_root / "tts" / "local_voxcpm2" / "runtime" / "y").exists())
        # 旧位置的 models/ tts/ 已不在
        self.assertFalse((self.old_root / "models").exists())
        self.assertFalse((self.old_root / "tts").exists())
        # library 不动(不是模型,留原地)
        self.assertTrue((self.old_root / "library" / "a.json").exists())

    def test_relocate_rejects_existing_target(self):
        (self.old_root / "models" / "m").mkdir(parents=True)
        new_root = self.root / "new"
        (new_root / "models").mkdir(parents=True)   # 目标已存在 models/
        with self.assertRaises(RuntimeError):
            mm.relocate_models(new_root)

    def test_relocate_same_path_noop(self):
        (self.old_root / "models").mkdir(parents=True)
        mm.relocate_models(self.old_root)   # 同路径,不报错、不动
        self.assertTrue((self.old_root / "models").exists())

    def test_explicit_source_rolls_first_directory_back_if_second_move_fails(self):
        explicit_old = self.root / "explicit-old"
        (explicit_old / "models" / "asr").mkdir(parents=True)
        (explicit_old / "models" / "asr" / "model.bin").write_text("asr")
        (explicit_old / "tts" / "voice").mkdir(parents=True)
        (explicit_old / "tts" / "voice" / "model.bin").write_text("tts")
        new_root = self.root / "new-root"
        real_move = mm.shutil.move

        def fail_on_tts(source, target):
            if Path(source).name == "tts":
                raise OSError("fake second move failure")
            return real_move(source, target)

        with mock.patch.object(mm.shutil, "move", side_effect=fail_on_tts):
            with self.assertRaisesRegex(OSError, "fake second move failure"):
                mm.relocate_models(new_root, source_root=explicit_old)

        self.assertTrue((explicit_old / "models" / "asr" / "model.bin").exists())
        self.assertTrue((explicit_old / "tts" / "voice" / "model.bin").exists())
        self.assertFalse((new_root / "models").exists())

    def test_models_root_reads_config(self):
        # config.models_dir 为空时 = app 数据目录;非空时 = 配置值
        from src import config
        cfgdir = self.root / "cfg"
        cfgdir.mkdir()
        orig_path = config._CONFIG_PATH
        config._CONFIG_PATH = cfgdir / "config.json"  # type: ignore[assignment]
        mm.models_root = self._orig  # 用真实实现读 config
        try:
            config._CONFIG_PATH.write_text(json.dumps({"models_dir": str(self.root / "custom")}))
            self.assertEqual(mm.models_root(), self.root / "custom")
        finally:
            config._CONFIG_PATH = orig_path  # type: ignore[assignment]
            mm.models_root = lambda: self.old_root  # type: ignore[assignment]

    def test_qwen3_runtime_metadata_required_for_ready(self):
        asr = self.old_root / "models" / "local_qwen3"
        aligner = asr / "_aligner"
        aligner.mkdir(parents=True)
        (asr / ".lulu_done").write_text("ok")
        (aligner / ".lulu_done").write_text("ok")
        self._mark_trusted("local_qwen3", asr)

        self.assertTrue(mm.is_downloaded("local_qwen3"))
        with mock.patch.object(mm.util, "find_spec", return_value=object()), \
                mock.patch.object(mm.metadata, "version", side_effect=mm.metadata.PackageNotFoundError):
            self.assertIn("metadata 缺失", mm.runtime_issue("local_qwen3"))
            self.assertFalse(mm.is_runtime_ready("local_qwen3"))
            self.assertFalse(mm.is_ready("local_qwen3"))

    def test_qwen3_ready_when_model_and_runtime_metadata_exist(self):
        asr = self.old_root / "models" / "local_qwen3"
        aligner = asr / "_aligner"
        aligner.mkdir(parents=True)
        (asr / ".lulu_done").write_text("ok")
        (aligner / ".lulu_done").write_text("ok")
        self._mark_trusted("local_qwen3", asr)

        with mock.patch.object(mm.util, "find_spec", return_value=object()), \
                mock.patch.object(mm.metadata, "version", return_value="0.14.0"):
            self.assertIsNone(mm.runtime_issue("local_qwen3"))
            self.assertTrue(mm.is_runtime_ready("local_qwen3"))
            self.assertTrue(mm.is_ready("local_qwen3"))

    def test_relocate_rebinds_existing_trusted_model_receipt(self):
        model = self.old_root / "models" / "local_whisper_mlx"
        model.mkdir(parents=True)
        (model / "weights.safetensors").write_bytes(b"trusted")
        (model / ".lulu_done").write_text("ok", encoding="utf-8")
        self._mark_trusted("local_whisper_mlx", model)
        self.assertTrue(mm.is_downloaded("local_whisper_mlx"))

        new_root = self.root / "new-model-root"
        mm.relocate_models(new_root)
        mm.models_root = lambda: new_root  # type: ignore[assignment]
        self.assertTrue(mm.is_downloaded("local_whisper_mlx"))


if __name__ == "__main__":
    unittest.main()
