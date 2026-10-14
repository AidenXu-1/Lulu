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
    def test_build_environment_rejects_host_selected_macos_26_mlx(self):
        distributions = {
            "mlx": FakeDistribution("0.31.2", MLX_26_WHEEL),
            "mlx-metal": FakeDistribution("0.31.2", METAL_15_WHEEL),
            "mlx-whisper": FakeDistribution("0.4.3", "Tag: py3-none-any\n"),
        }

        report = COMPATIBILITY["evaluate_build_environment"](
            distribution_reader=distributions.__getitem__,
        )

        self.assertFalse(report["ok"])
        self.assertIn("macosx_26_0_arm64", "\n".join(report["violations"]))

    def test_build_environment_accepts_exact_macos_15_lock(self):
        distributions = {
            "mlx": FakeDistribution("0.31.2", MLX_15_WHEEL),
            "mlx-metal": FakeDistribution("0.31.2", METAL_15_WHEEL),
            "mlx-whisper": FakeDistribution("0.4.3", "Tag: py3-none-any\n"),
        }

        report = COMPATIBILITY["evaluate_build_environment"](
            distribution_reader=distributions.__getitem__,
        )

        self.assertTrue(report["ok"], report)

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

    def test_final_app_gate_rejects_any_native_component_above_macos_15(self):
        with tempfile.TemporaryDirectory() as raw:
            app = self._fake_app(Path(raw), minos="26.0")

            report = COMPATIBILITY["evaluate_app"](app, vtool_runner=self._vtool)

        self.assertFalse(report["ok"])
        self.assertEqual(report["native"]["above_floor_count"], 1)
        self.assertIn("libmlx.dylib", report["native"]["violations"][0]["path"])

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

    def test_final_app_gate_accepts_truthful_macos_15_bundle(self):
        with tempfile.TemporaryDirectory() as raw:
            app = self._fake_app(Path(raw), minos="15.0")

            report = COMPATIBILITY["evaluate_app"](app, vtool_runner=self._vtool)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["declared_minimum"], "15.0")
        self.assertEqual(report["native"]["maximum_minos"], "15.0")

    def test_verified_materialized_wheel_alias_is_logical_once_but_every_macho_is_scanned(self):
        with tempfile.TemporaryDirectory() as raw:
            app = self._fake_app(Path(raw), minos="15.0")
            alias_root = app / "Contents/Resources/materialized-runtime"
            alias_root.parent.mkdir(parents=True)
            shutil.copytree(app / "Contents/Frameworks", alias_root)
            scanned: list[str] = []

            def recording_vtool(path: Path):
                scanned.append(path.relative_to(app).as_posix())
                return self._vtool(path)

            rejected = COMPATIBILITY["evaluate_app"](
                app,
                vtool_runner=recording_vtool,
            )
            self.assertFalse(rejected["ok"])
            self.assertIn("非别名 dist-info 数量为 2", "\n".join(rejected["violations"]))

            scanned.clear()
            accepted = COMPATIBILITY["evaluate_app"](
                app,
                vtool_runner=recording_vtool,
                materialized_alias_roots=(
                    "Contents/Resources/materialized-runtime",
                ),
            )

        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["native"]["file_count"], 2)
        self.assertEqual(len(scanned), 2)
        self.assertTrue(
            any(path.startswith("Contents/Resources/materialized-runtime/") for path in scanned)
        )


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

    def test_macos_15_detects_macos_26_wheel_before_native_import_or_download(self):
        patches = self._platform("15.6")
        with patches[0], patches[1], patches[2], \
                mock.patch.object(mm.metadata, "distribution", return_value=FakeDistribution("0.31.2", MLX_26_WHEEL)), \
                mock.patch.object(mm, "managed_dir", side_effect=AssertionError("must fail before filesystem")):
            issue = mm.runtime_issue("local_whisper_mlx")
            with self.assertRaisesRegex(RuntimeError, "macOS 26"):
                mm.download_blocking("local_whisper_mlx")

        self.assertIn("macOS 26", issue or "")
        self.assertIn("SenseVoice", issue or "")

    def test_macos_15_accepts_macos_15_wheel(self):
        patches = self._platform("15.6")
        with patches[0], patches[1], patches[2], \
                mock.patch.object(mm.metadata, "distribution", return_value=FakeDistribution("0.31.2", MLX_15_WHEEL)):
            self.assertIsNone(mm.runtime_issue("local_whisper_mlx"))

    def test_model_controller_blocks_download_before_model_exists(self):
        message = "当前 Lulu 的 Whisper MLX 需要 macOS 26；模型文件会保留，可继续使用 SenseVoice。"
        cfg=SimpleNamespace(engine_name='local_sensevoice',default_tts_model='local_indextts2')
        with mock.patch('src.core.model_connections.Service'):
            controller=models_controller.ModelController(cfg)
        with mock.patch.object(mm,'runtime_issue',return_value=message),mock.patch.object(models_controller,'DownloadWorker') as download:
            with self.assertRaisesRegex(RuntimeError,'macOS 26'):
                controller.local_action('whisper','install')
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
