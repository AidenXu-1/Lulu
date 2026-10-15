from __future__ import annotations

import os
import plistlib
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.core import models_manager as mm
from src.core.engines.local_models import LOCAL_MODELS
from src.ui.model_settings import controller as models_controller
from types import SimpleNamespace


APP = Path(__file__).resolve().parents[1]
COMPATIBILITY = runpy.run_path(str(APP / "packaging/macos_compatibility.py"))


class FakeDistribution:
    def __init__(self, version: str, wheel: str):
        self.version = version
        self._wheel = wheel

    def read_text(self, name: str) -> str | None:
        return self._wheel if name == "WHEEL" else None


MLX_15_WHEEL = """\
Wheel-Version: 1.0
Root-Is-Purelib: false
Tag: cp311-cp311-macosx_15_0_arm64
"""
MLX_26_WHEEL = MLX_15_WHEEL.replace("macosx_15_0", "macosx_26_0")
METAL_15_WHEEL = MLX_15_WHEEL.replace("cp311-cp311", "py3-none")


class MacOSCompatibilityGateTests(unittest.TestCase):


    def _fake_app(self, root: Path, *, minos: str) -> Path:
        app = root / "Lulu.app"
        frameworks = app / "Contents/Frameworks"
        frameworks.mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump({"LSMinimumSystemVersion": "15.0"}, handle)
        native = frameworks / "libmlx.dylib"
        native.write_bytes(b"\xcf\xfa\xed\xfe" + b"fake")
        (frameworks / "mlx.metallib").write_bytes(b"MTLBfixture")
        mlx_info = frameworks / "mlx-0.31.2.dist-info"
        mlx_info.mkdir()
        (mlx_info / "WHEEL").write_text(MLX_15_WHEEL, encoding="utf-8")
        metal_info = frameworks / "mlx_metal-0.31.2.dist-info"
        metal_info.mkdir()
        (metal_info / "WHEEL").write_text(METAL_15_WHEEL, encoding="utf-8")
        whisper_info = frameworks / "mlx_whisper-0.4.3.dist-info"
        whisper_info.mkdir()
        (whisper_info / "WHEEL").write_text("Tag: py3-none-any\n", encoding="utf-8")
        self.fake_minos = minos
        return app

    def _vtool(self, _path: Path):
        return subprocess.CompletedProcess(
            ["vtool"],
            0,
            stdout=f"platform MACOS\n    minos {self.fake_minos}\n      sdk 26.4\n",
            stderr="",
        )


    def test_final_app_rejects_metallib_only_in_package_subdirectory(self):
        with tempfile.TemporaryDirectory() as raw:
            app = self._fake_app(Path(raw), minos="15.0")
            frameworks = app / "Contents/Frameworks"
            nested = frameworks / "mlx/lib"
            nested.mkdir(parents=True)
            (frameworks / "mlx.metallib").rename(nested / "mlx.metallib")
            report = COMPATIBILITY["evaluate_app"](app, vtool_runner=self._vtool)
        self.assertFalse(report["ok"])
        self.assertIn("metallib", " ".join(report["violations"]))


class WhisperMLXRuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _platform(version: str):
        return (
            mock.patch.object(mm.platform, "system", return_value="Darwin"),
            mock.patch.object(mm.platform, "machine", return_value="arm64"),
            mock.patch.object(mm.platform, "mac_ver", return_value=(version, ("", "", ""), "")),
        )


if __name__ == "__main__":
    unittest.main()
