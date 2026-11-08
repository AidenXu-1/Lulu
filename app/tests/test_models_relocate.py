import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core import models_manager as mm


class ModelsRelocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
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




    def test_stop_after_last_move_restores_the_old_directory(self):
        source = self.old_root / "models" / "fixture"
        source.mkdir(parents=True)
        (source / "model.bin").write_text("fixture")
        destination = self.root / "new"

        def stopping():
            return (destination / "models").exists()

        with self.assertRaisesRegex(RuntimeError, "已取消"):
            mm.relocate_models(destination, is_cancelled=stopping)
        self.assertEqual((source / "model.bin").read_text(), "fixture")
        self.assertFalse((destination / "models").exists())







if __name__ == "__main__":
    unittest.main()
