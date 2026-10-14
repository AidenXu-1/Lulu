from __future__ import annotations

from contextlib import contextmanager
import importlib
import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
import runpy
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from src.version import __build__, __channel__, __version__


APP = Path(__file__).resolve().parents[1]
RELEASE_FEATURES = runpy.run_path(str(APP / "src/release_features.py"))
PROFILE_GATE = runpy.run_path(str(APP / "packaging/profile_gate.py"))
SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40


class TrustedTestEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(APP))

    def tearDown(self) -> None:
        sys.path.remove(str(APP))
        sys.modules.pop("run_trusted_test", None)
        sys.modules.pop("src.trusted_main", None)

    def _sealed_root(self, root: Path) -> None:
        product_source = PROFILE_GATE["product_source_manifest"](
            "trusted_test",
            source_commit=SOURCE_COMMIT,
            source_tree=SOURCE_TREE,
            source_loader=lambda repo_path: (
                APP / repo_path.removeprefix("app/")
            ).read_bytes(),
        )
        product_source_digest = PROFILE_GATE["write_product_source"](
            root / "product_source.json",
            product_source,
        )
        for bundle_path, record in product_source["resources"].items():
            destination = root / bundle_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                (APP / record["path"].removeprefix("app/")).read_bytes()
            )
        PROFILE_GATE["write_profile"](
            root / "build_profile.json",
            "trusted_test",
            version=__version__,
            build=__build__,
            source_commit=SOURCE_COMMIT,
            source_tree=SOURCE_TREE,
            product_source_sha256=product_source_digest,
        )
        profile = root / "build_profile.json"
        (root / "AUTHORSHIP.json").write_text(
            json.dumps(
                {
                    "build_profile": "trusted_test",
                    "build_profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                    "source_commit": SOURCE_COMMIT,
                    "source_tree": SOURCE_TREE,
                    "product_source_sha256": product_source_digest,
                }
            ),
            encoding="utf-8",
        )
        RELEASE_FEATURES["write_manifest"](
            root / "release_features.json",
            profile="trusted_test",
        )

    def _synthetic_frozen_bundle(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path]:
        app = root / "Lulu-Trusted-Test.app"
        contents = app / "Contents"
        frameworks = contents / "Frameworks"
        resources = contents / "Resources"
        macos = contents / "MacOS"
        frameworks.mkdir(parents=True)
        resources.mkdir()
        macos.mkdir()
        executable = macos / "Lulu-Trusted-Test"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self._sealed_root(resources)
        (frameworks / "build_profile.json").symlink_to(
            "../Resources/build_profile.json"
        )
        (frameworks / "release_features.json").symlink_to(
            "../Resources/release_features.json"
        )
        return frameworks, resources, executable

    @contextmanager
    def _frozen_runtime(self, frameworks: Path, executable: Path):
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys,
            "_MEIPASS",
            str(frameworks),
            create=True,
        ), patch.object(
            sys,
            "executable",
            str(executable),
        ):
            yield

    def _synthetic_trusted_app(self, root: Path) -> tuple[Path, Path]:
        app = root / "Lulu-Trusted-Test.app"
        resources = app / "Contents/Resources"
        macos = app / "Contents/MacOS"
        resources.mkdir(parents=True)
        macos.mkdir()
        executable = macos / "Lulu-Trusted-Test"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self._sealed_root(resources)
        profile = resources / "build_profile.json"
        profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
        product_source_digest = hashlib.sha256(
            json.dumps(
                json.loads((resources / "product_source.json").read_text(encoding="utf-8")),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (resources / "AUTHORSHIP.json").write_text(
            json.dumps(
                {
                    "build_profile": "trusted_test",
                    "build_profile_sha256": profile_sha,
                    "source_commit": SOURCE_COMMIT,
                    "source_tree": SOURCE_TREE,
                    "product_source_sha256": product_source_digest,
                }
            ),
            encoding="utf-8",
        )
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                    "CFBundleName": "Lulu Trusted Test",
                    "CFBundleExecutable": "Lulu-Trusted-Test",
                    "CFBundlePackageType": "APPL",
                },
                handle,
            )
        analysis = root / "analysis.json"
        PROFILE_GATE["write_analysis_receipt"](
            analysis,
            "trusted_test",
            [("src.trusted_main", str(APP / "src/trusted_main.py"))],
            [
                "AUTHORSHIP.json",
                "build_profile.json",
                "release_features.json",
                "product_source.json",
                *json.loads(
                    (resources / "product_source.json").read_text(encoding="utf-8")
                )["resources"],
            ],
            profile_sha,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
        )
        return app, analysis

    def test_release_manifest_is_exact_and_value_anomalies_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "release_features.json"
            RELEASE_FEATURES["write_manifest"](path, profile="trusted_test")
            loaded = RELEASE_FEATURES["load_manifest"](
                path,
                expected_profile="trusted_test",
            )
            self.assertIs(
                loaded["features"]["homepage_collection"]["xiaohongshu"],
                False,
            )
            self.assertFalse(
                RELEASE_FEATURES["homepage_collection_enabled"](
                    "xiaohongshu",
                    manifest=loaded,
                )
            )
            with self.assertRaises(KeyError):
                RELEASE_FEATURES["homepage_collection_enabled"](
                    "douyin",
                    manifest=loaded,
                )

            bad_values = (0, None, True, "false", {})
            for index, value in enumerate(bad_values):
                bad = RELEASE_FEATURES["manifest_payload"]("trusted_test")
                bad["features"]["homepage_collection"]["xiaohongshu"] = value
                bad_path = root / f"bad-{index}.json"
                bad_path.write_text(json.dumps(bad), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    RELEASE_FEATURES["load_manifest"](
                        bad_path,
                        expected_profile="trusted_test",
                    )

            extra = RELEASE_FEATURES["manifest_payload"]("trusted_test")
            extra["features"]["homepage_collection"]["bilibili"] = False
            extra_path = root / "extra.json"
            extra_path.write_text(json.dumps(extra), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                RELEASE_FEATURES["load_manifest"](
                    extra_path,
                    expected_profile="trusted_test",
                )

    def test_release_manifest_rejects_symlink_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.json"
            RELEASE_FEATURES["write_manifest"](target, profile="trusted_test")
            with self.assertRaises(FileExistsError):
                RELEASE_FEATURES["write_manifest"](target, profile="trusted_test")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                RELEASE_FEATURES["load_manifest"](
                    link,
                    expected_profile="trusted_test",
                )

    def test_packaged_entry_reads_resources_file_instead_of_frameworks_symlink(self):
        run_trusted = importlib.import_module("run_trusted_test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            framework_manifest = frameworks / "release_features.json"
            framework_manifest.unlink()
            malicious = RELEASE_FEATURES["manifest_payload"]("trusted_test")
            malicious["features"]["homepage_collection"]["xiaohongshu"] = True
            outside_manifest = root / "outside-release-features.json"
            outside_manifest.write_text(json.dumps(malicious), encoding="utf-8")
            framework_manifest.symlink_to(outside_manifest)
            with self._frozen_runtime(frameworks, executable):
                manifest = run_trusted._assert_packaged_profile()
                self.assertEqual(
                    RELEASE_FEATURES["sealed_manifest_path"](),
                    resources / "release_features.json",
                )
                self.assertEqual(
                    RELEASE_FEATURES["load_sealed_features"](),
                    RELEASE_FEATURES["manifest_payload"]("trusted_test"),
                )
            self.assertFalse(
                manifest["features"]["homepage_collection"]["xiaohongshu"]
            )

    def test_packaged_entry_rejects_resources_symlink_and_bundle_path_drift(self):
        run_trusted = importlib.import_module("run_trusted_test")

        with self.subTest(state="resources_symlink"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            outside = root / "outside"
            resources.rename(outside)
            resources.symlink_to(outside, target_is_directory=True)
            with self._frozen_runtime(
                frameworks,
                executable,
            ), self.assertRaises(RuntimeError):
                run_trusted._assert_packaged_profile()

    def test_packaged_entry_rejects_product_source_or_authorship_drift(self):
        run_trusted = importlib.import_module("run_trusted_test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            product_source = resources / "product_source.json"
            payload = json.loads(product_source.read_text(encoding="utf-8"))
            payload["spec"]["sha256"] = "f" * 64
            product_source.write_text(json.dumps(payload), encoding="utf-8")
            with self._frozen_runtime(
                frameworks,
                executable,
            ), self.assertRaises(RuntimeError):
                run_trusted._assert_packaged_profile()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            authorship = resources / "AUTHORSHIP.json"
            payload = json.loads(authorship.read_text(encoding="utf-8"))
            payload["product_source_sha256"] = "f" * 64
            authorship.write_text(json.dumps(payload), encoding="utf-8")
            with self._frozen_runtime(
                frameworks,
                executable,
            ), self.assertRaises(RuntimeError):
                run_trusted._assert_packaged_profile()

        with self.subTest(state="meipass_outside"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _frameworks, _resources, executable = self._synthetic_frozen_bundle(root)
            outside = root / "outside"
            outside.mkdir()
            self._sealed_root(outside)
            with self._frozen_runtime(
                outside,
                executable,
            ), self.assertRaises(RuntimeError):
                run_trusted._assert_packaged_profile()

        with self.subTest(state="executable_drift"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, _resources, _executable = self._synthetic_frozen_bundle(root)
            outside_executable = root / "Lulu-Trusted-Test"
            outside_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            outside_executable.chmod(0o755)
            with self._frozen_runtime(
                frameworks,
                outside_executable,
            ), self.assertRaises(RuntimeError):
                run_trusted._assert_packaged_profile()

    def test_profile_gate_binds_trusted_manifest_to_bundle_seal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_trusted_app(root)

            def archive_reader(executable: Path, _profile: str):
                product_source = json.loads(
                    (
                        executable.parent.parent
                        / "Resources/product_source.json"
                    ).read_text(encoding="utf-8")
                )
                return (
                    ["src.trusted_main"],
                    [],
                    {
                        "format": "pyinstaller_carchive",
                        "carchive_count": 2,
                        "pyz_archive_count": 1,
                        "pyz_count": 1,
                        "executable_sha256": hashlib.sha256(
                            executable.read_bytes()
                        ).hexdigest(),
                        "app_entry_scripts": [product_source["entry_module"]],
                        "runtime_hook_scripts": [],
                        "product_code_sha256": {
                            name: record["code_sha256"]
                            for name, record in product_source["modules"].items()
                        },
                    },
                )

            result = PROFILE_GATE["_verify_app_with_reader"](
                app,
                "trusted_test",
                analysis,
                root / "receipt.json",
                archive_reader,
            )
            self.assertTrue(result["ok"])
            self.assertTrue(result["profile_in_bundle_seal"])
            self.assertTrue(result["checks"]["release_features_valid"])
            self.assertEqual(result["upload_target"], "none")

            manifest = app / "Contents/Resources/release_features.json"
            manifest.write_text(
                '{"schema":1,"profile":"trusted_test","features":'
                '{"homepage_collection":{"xiaohongshu":true}}}',
                encoding="utf-8",
            )
            tampered_seal = subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tampered_seal.returncode, 0)
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
                check=True,
                capture_output=True,
            )
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "trusted_test",
                    analysis,
                    root / "bad-receipt.json",
                    archive_reader,
                )
            receipt = json.loads(
                (root / "bad-receipt.json").read_text(encoding="utf-8")
            )
            self.assertFalse(receipt["checks"]["release_features_valid"])

    def test_packaged_entry_ignores_environment_and_arguments(self):
        run_trusted = importlib.import_module("run_trusted_test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            (resources / "config.json").write_text(
                json.dumps(
                    {
                        "build_profile": "external_candidate",
                        "homepage_collection": {"xiaohongshu": True},
                    }
                ),
                encoding="utf-8",
            )
            with self._frozen_runtime(frameworks, executable), patch.dict(
                os.environ,
                {
                    "LULU_BUILD_PROFILE": "external_candidate",
                    "LULU_RELEASE_FEATURES": '{"xiaohongshu":true}',
                    "LULU_LICENSE_ENV": "production",
                },
                clear=False,
            ), patch.object(
                sys,
                "argv",
                ["Lulu", "--profile", "external_candidate", "--xiaohongshu"],
            ):
                manifest = run_trusted._assert_packaged_profile()
            self.assertIs(
                manifest["features"]["homepage_collection"]["xiaohongshu"],
                False,
            )

    def test_packaged_entry_fails_before_main_for_identity_or_manifest_damage(self):
        run_trusted = importlib.import_module("run_trusted_test")
        mutations = (
            ("profile", "external_candidate"),
            ("upload_target", "github-release-candidate"),
            ("entry", "run_external.py"),
            ("version", "0.2.3"),
            ("build", "20260725.0"),
            ("source_commit", "a" * 39),
            ("source_tree", "g" * 40),
        )
        for key, value in mutations:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                frameworks, resources, executable = self._synthetic_frozen_bundle(
                    root
                )
                profile_path = resources / "build_profile.json"
                payload = json.loads(profile_path.read_text(encoding="utf-8"))
                payload[key] = value
                profile_path.write_text(json.dumps(payload), encoding="utf-8")
                with self._frozen_runtime(
                    frameworks,
                    executable,
                ), self.assertRaises(RuntimeError):
                    run_trusted._assert_packaged_profile()

        with self.subTest(state="authorship_mismatch"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            authorship = resources / "AUTHORSHIP.json"
            payload = json.loads(authorship.read_text(encoding="utf-8"))
            payload["source_commit"] = "c" * 40
            authorship.write_text(json.dumps(payload), encoding="utf-8")
            with self._frozen_runtime(
                frameworks,
                executable,
            ), self.assertRaisesRegex(RuntimeError, "作者归属"):
                run_trusted._assert_packaged_profile()

        with self.subTest(state="profile_symlink"), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frameworks, resources, executable = self._synthetic_frozen_bundle(root)
            profile = resources / "build_profile.json"
            outside = root / "outside-profile.json"
            outside.write_bytes(profile.read_bytes())
            profile.unlink()
            profile.symlink_to(outside)
            with self._frozen_runtime(
                frameworks,
                executable,
            ), self.assertRaisesRegex(RuntimeError, "普通文件"):
                run_trusted._assert_packaged_profile()

        for state in (
            "missing",
            "manifest_symlink",
            "corrupt",
            "wrong_profile",
            "xiaohongshu_enabled",
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                frameworks, resources, executable = self._synthetic_frozen_bundle(
                    root
                )
                manifest = resources / "release_features.json"
                if state == "missing":
                    manifest.unlink()
                elif state == "manifest_symlink":
                    manifest.unlink()
                    outside = root / "outside-release-features.json"
                    RELEASE_FEATURES["write_manifest"](
                        outside,
                        profile="trusted_test",
                    )
                    manifest.symlink_to(outside)
                elif state == "corrupt":
                    manifest.write_text('{"profile":"trusted_test"}', encoding="utf-8")
                else:
                    payload = RELEASE_FEATURES["manifest_payload"]("trusted_test")
                    if state == "wrong_profile":
                        payload["profile"] = "external_candidate"
                    else:
                        payload["features"]["homepage_collection"][
                            "xiaohongshu"
                        ] = True
                    manifest.write_text(json.dumps(payload), encoding="utf-8")
                with self._frozen_runtime(
                    frameworks,
                    executable,
                ), self.assertRaises(RuntimeError):
                    run_trusted._assert_packaged_profile()

    def test_trusted_main_preserves_real_home_and_never_saves_runtime_gate(self):
        trusted_main = importlib.import_module("src.trusted_main")
        calls: list[object] = []

        class FakeConfig:
            theme = "warm"
            auto_update_enabled = True

            @classmethod
            def load(cls):
                calls.append(("load", os.environ.get("HOME")))
                return cls()

            def save(self):
                calls.append("save")

        class FakeApplication:
            def __init__(self, argv):
                self.argv = list(argv)

            def setApplicationName(self, value):
                calls.append(("app_name", value))

            def setApplicationDisplayName(self, value):
                calls.append(("display_name", value))

            def exec(self):
                return 0

        class FakeWindow:
            def __init__(self):
                self.cfg = FakeConfig.load()

            def width(self):
                return 800

            def setWindowTitle(self, value):
                calls.append(("title", value))

            def show(self):
                calls.append("show")

            def raise_(self):
                pass

            def activateWindow(self):
                pass

            def enable_titlebar_inset(self):
                pass

        class FakeLabel:
            def __init__(self, text, parent):
                calls.append(("badge", text))

            def setObjectName(self, value):
                pass

            def setStyleSheet(self, value):
                pass

            def adjustSize(self):
                pass

            def width(self):
                return 100

            def move(self, x, y):
                pass

            def raise_(self):
                pass

        qt_core = types.ModuleType("PySide6.QtCore")
        qt_core.QTimer = types.SimpleNamespace(singleShot=lambda _ms, callback: callback())
        qt_widgets = types.ModuleType("PySide6.QtWidgets")
        qt_widgets.QApplication = FakeApplication
        qt_widgets.QLabel = FakeLabel
        pyside = types.ModuleType("PySide6")
        config = types.ModuleType("src.config")
        config.AppConfig = FakeConfig
        theme = types.ModuleType("src.ui.theme")
        theme.apply_fonts = lambda _app: calls.append("fonts")
        theme.apply_theme = lambda value: calls.append(("theme", value))
        macos = types.ModuleType("src.ui.macos")
        macos.activate_regular_app = lambda: calls.append("activate")
        macos.unify_titlebar = lambda _window: False
        main_window = types.ModuleType("src.ui.main_window")
        main_window.MainWindow = FakeWindow
        ui = types.ModuleType("src.ui")
        ui.theme = theme

        original_home = os.environ.get("HOME")
        fake_modules = {
            "PySide6": pyside,
            "PySide6.QtCore": qt_core,
            "PySide6.QtWidgets": qt_widgets,
            "src.config": config,
            "src.ui": ui,
            "src.ui.theme": theme,
            "src.ui.macos": macos,
            "src.ui.main_window": main_window,
        }
        with patch.object(
            trusted_main,
            "load_sealed_features",
            return_value=RELEASE_FEATURES["manifest_payload"]("trusted_test"),
        ), patch.dict(sys.modules, fake_modules), patch.dict(
            os.environ,
            {"HOME": original_home or "/Users/test"},
            clear=False,
        ):
            stages: list[tuple[str, str]] = []
            self.assertEqual(
                trusted_main.main(lambda phase, event: stages.append((phase, event))),
                0,
            )
        self.assertNotIn("save", calls)
        self.assertIn(("load", original_home or "/Users/test"), calls)
        self.assertIn(("app_name", "Lulu Trusted Test"), calls)
        self.assertIn(("badge", "兆基本机自用测试版 · 不可外发"), calls)
        self.assertEqual(
            stages,
            [
                ("trusted_main", "enter"),
                ("feature_gate", "enter"),
                ("feature_gate", "ok"),
                ("qt_import", "enter"),
                ("qt_import", "ok"),
                ("product_import", "enter"),
                ("product_import", "ok"),
                ("qapplication", "enter"),
                ("qapplication", "ok"),
                ("configuration_theme", "enter"),
                ("configuration_theme", "ok"),
                ("activation", "enter"),
                ("activation", "ok"),
                ("main_window", "enter"),
                ("main_window", "ok"),
                ("window_prepare", "enter"),
                ("window_prepare", "ok"),
                ("show_activate", "enter"),
                ("show_activate", "issued"),
                ("titlebar", "enter"),
                ("titlebar", "ok"),
                ("event_loop", "enter"),
                ("event_loop", "return"),
            ],
        )

    def test_diagnostic_stage_recorder_is_create_only_fsynced_and_closed_schema(self):
        run_trusted = importlib.import_module("run_trusted_test")
        expected_keys = {
            "schema_id",
            "diagnostic_contract_id",
            "build",
            "pid",
            "sequence",
            "phase",
            "event",
            "wall_time_ns",
            "monotonic_ns",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostic"
            root.mkdir(mode=0o700)
            stage_path = root / "stages.jsonl"
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ), patch.object(run_trusted.os, "fsync", wraps=os.fsync) as fsync:
                recorder = run_trusted._StageRecorder()
                recorder("bootstrap", "enter")
                recorder("profile_gate", "enter")
                recorder("profile_gate", "ok")
                recorder.close()
                self.assertGreaterEqual(fsync.call_count, 3)

            self.assertEqual(stat.S_IMODE(stage_path.stat().st_mode), 0o600)
            records = [json.loads(line) for line in stage_path.read_text().splitlines()]
            self.assertEqual([record["sequence"] for record in records], [0, 1, 2])
            self.assertEqual(
                [(record["phase"], record["event"]) for record in records],
                [("bootstrap", "enter"), ("profile_gate", "enter"), ("profile_gate", "ok")],
            )
            for record in records:
                self.assertEqual(set(record), expected_keys)
                self.assertEqual(record["schema_id"], "lulu.gui-not-visible-stage/v1")
                self.assertEqual(
                    record["diagnostic_contract_id"],
                    "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
                )
                self.assertEqual(record["build"], "20260823.4")
                self.assertEqual(record["pid"], os.getpid())
                self.assertIs(type(record["wall_time_ns"]), int)
                self.assertIs(type(record["monotonic_ns"]), int)
                serialized = json.dumps(record, sort_keys=True).lower()
                for forbidden in (
                    "argv", "home", "username", "hostname", "window", "title",
                    "keychain", "token", "traceback", "exception", "path",
                ):
                    self.assertNotIn(forbidden, serialized)

            winner = stage_path.read_bytes()
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ), self.assertRaises(FileExistsError):
                run_trusted._StageRecorder()
            self.assertEqual(stage_path.read_bytes(), winner)

    def test_diagnostic_stage_recorder_fails_closed_on_root_transition_and_write_errors(self):
        run_trusted = importlib.import_module("run_trusted_test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostic"
            root.mkdir(mode=0o755)
            stage_path = root / "stages.jsonl"
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ), self.assertRaises(RuntimeError):
                run_trusted._StageRecorder()
            self.assertFalse(stage_path.exists())

            root.chmod(0o700)
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ):
                recorder = run_trusted._StageRecorder()
                with self.assertRaises(RuntimeError):
                    recorder("profile_gate", "enter")
                recorder.close()

            stage_path.unlink()
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ):
                recorder = run_trusted._StageRecorder()
                with patch.object(run_trusted.os, "write", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        recorder("bootstrap", "enter")
                recorder.close()

            stage_path.unlink()
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ):
                recorder = run_trusted._StageRecorder()
                with patch.object(run_trusted.os, "fsync", side_effect=OSError("fsync failed")):
                    with self.assertRaisesRegex(OSError, "fsync failed"):
                        recorder("bootstrap", "enter")
                recorder.close()

            stage_path.unlink()
            protected = root / "protected"
            protected.write_bytes(b"unchanged")
            stage_path.symlink_to(protected)
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ), self.assertRaises(FileExistsError):
                run_trusted._StageRecorder()
            self.assertEqual(protected.read_bytes(), b"unchanged")

    def test_diagnostic_marker_authority_is_fixed_and_not_caller_configurable(self):
        run_trusted = importlib.import_module("run_trusted_test")
        trusted_main = importlib.import_module("src.trusted_main")
        self.assertEqual(list(inspect.signature(run_trusted.main).parameters), [])
        self.assertEqual(
            list(inspect.signature(run_trusted._StageRecorder).parameters),
            [],
        )
        self.assertEqual(
            list(inspect.signature(trusted_main.main).parameters),
            ["stage_marker"],
        )
        self.assertEqual(
            run_trusted._STAGE_ROOT,
            Path("/private/tmp/lulu-gui-not-visible-20260823.4-A0EC73"),
        )
        self.assertEqual(run_trusted._STAGE_PATH, run_trusted._STAGE_ROOT / "stages.jsonl")

    def test_diagnostic_main_preserves_outer_order_and_reraises_original_error(self):
        run_trusted = importlib.import_module("run_trusted_test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostic"
            root.mkdir(mode=0o700)
            stage_path = root / "stages.jsonl"
            trusted_module = types.ModuleType("src.trusted_main")

            def failing_main(marker):
                marker("trusted_main", "enter")
                marker("feature_gate", "enter")
                marker("feature_gate", "error")
                raise LookupError("original failure")

            trusted_module.main = failing_main
            with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                run_trusted, "_STAGE_PATH", stage_path
            ), patch.object(
                run_trusted, "_assert_packaged_profile", return_value={}
            ), patch.dict(sys.modules, {"src.trusted_main": trusted_module}), self.assertRaisesRegex(
                LookupError, "original failure"
            ):
                run_trusted.main()

            records = [json.loads(line) for line in stage_path.read_text().splitlines()]
            self.assertEqual(
                [(record["phase"], record["event"]) for record in records],
                [
                    ("bootstrap", "enter"),
                    ("profile_gate", "enter"),
                    ("profile_gate", "ok"),
                    ("trusted_main_import", "enter"),
                    ("trusted_main_import", "ok"),
                    ("trusted_main_call", "enter"),
                    ("trusted_main", "enter"),
                    ("feature_gate", "enter"),
                    ("feature_gate", "error"),
                ],
            )

    def test_outer_business_error_survives_error_marker_write_or_fsync_failure(self):
        run_trusted = importlib.import_module("run_trusted_test")
        real_import = __import__
        real_write = os.write
        real_fsync = os.fsync
        error_marker_call = {
            "profile_gate": 3,
            "trusted_main_import": 5,
            "trusted_main_call": 7,
        }

        class BusinessFailure(RuntimeError):
            pass

        for phase in error_marker_call:
            for operation in ("write", "fsync"):
                with self.subTest(
                    phase=phase,
                    marker_operation=operation,
                ), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp) / "diagnostic"
                    root.mkdir(mode=0o700)
                    stage_path = root / "stages.jsonl"
                    sentinel = BusinessFailure(f"business:{phase}")
                    calls = 0

                    def marker_io(*args):
                        nonlocal calls
                        calls += 1
                        if calls == error_marker_call[phase]:
                            raise OSError(f"marker:{operation}")
                        if operation == "write":
                            return real_write(*args)
                        real_fsync(*args)
                        return None

                    def import_with_failure(
                        name,
                        globals=None,
                        locals=None,
                        fromlist=(),
                        level=0,
                    ):
                        if phase == "trusted_main_import" and name == "src.trusted_main":
                            raise sentinel
                        return real_import(name, globals, locals, fromlist, level)

                    trusted_module = types.ModuleType("src.trusted_main")

                    def fail_in_trusted_main(_marker):
                        raise sentinel

                    trusted_module.main = fail_in_trusted_main
                    with patch.object(run_trusted, "_STAGE_ROOT", root), patch.object(
                        run_trusted, "_STAGE_PATH", stage_path
                    ), patch.object(
                        run_trusted,
                        "_assert_packaged_profile",
                        side_effect=sentinel if phase == "profile_gate" else None,
                        return_value={},
                    ), patch(
                        "builtins.__import__",
                        side_effect=import_with_failure,
                    ), patch.dict(
                        sys.modules,
                        {"src.trusted_main": trusted_module}
                        if phase == "trusted_main_call"
                        else {},
                    ), patch.object(
                        run_trusted.os,
                        operation,
                        side_effect=marker_io,
                    ), self.assertRaises(BusinessFailure) as raised:
                        run_trusted.main()
                    self.assertIs(raised.exception, sentinel)

    def test_packaging_scripts_are_local_only_and_publish_chains_reject_trusted(self):
        trusted_script = APP / "packaging/build_trusted_test.sh"
        source = trusted_script.read_text(encoding="utf-8")
        self.assertIn("build_app.sh --identity trusted-test", source)
        self.assertIn("profile-channel trusted_test", source)
        self.assertNotIn("DMG_FILENAME=", source)
        self.assertIn("Signature=adhoc", source)
        self.assertNotIn("notarytool", source)
        self.assertNotIn("github", source.lower())
        self.assertNotIn("curl", source)

        external_source = (APP / "packaging/build_release.sh").read_text(encoding="utf-8")
        self.assertIn("profile-channel external_candidate", external_source)
        self.assertIn('DMG_NAME="Lulu-${VERSION}-${BUILD}-${ARCH}.dmg"', external_source)
        self.assertIn("make_install_instructions.py", external_source)

        validate = subprocess.run(
            ["bash", str(trusted_script), "--validate-only"],
            cwd=APP,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("profile=trusted_test", validate.stdout)
        self.assertIn(f"version={__version__}", validate.stdout)
        self.assertIn(f"build={__build__}", validate.stdout)
        self.assertIn(f"channel={__channel__}", validate.stdout)
        self.assertIn(
            f"app={APP}/dist/phase10-trusted-test-{__build__}/Lulu-Trusted-Test.app",
            validate.stdout,
        )
        self.assertNotIn("dmg=", validate.stdout)

        release = subprocess.run(
            ["bash", str(APP / "packaging/build_release.sh"), "trusted_test"],
            cwd=APP,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(release.returncode, 0)
        self.assertIn("只接受显式 external_candidate", release.stderr)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Lulu-Trusted-Test.app"
            resources = app / "Contents/Resources"
            resources.mkdir(parents=True)
            PROFILE_GATE["write_profile"](
                resources / "build_profile.json",
                "trusted_test",
                version=__version__,
                build=__build__,
                source_commit=SOURCE_COMMIT,
                source_tree=SOURCE_TREE,
            )
            dmg = root / "trusted.dmg"
            key = root / "key.pem"
            notes = root / "notes.md"
            for path in (dmg, key, notes):
                path.write_text("fixture", encoding="utf-8")
            key.chmod(0o600)
            update = subprocess.run(
                [
                    sys.executable,
                    str(APP / "packaging/make_update_release.py"),
                    "--app",
                    str(app),
                    "--dmg",
                    str(dmg),
                    "--output",
                    str(root / "out"),
                    "--private-key",
                    str(key),
                    "--notes",
                    str(notes),
                    "--repository",
                    "AidenXu-1/lulu-releases",
                ],
                cwd=APP,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(update.returncode, 0)
            self.assertIn("只接受 external_candidate", update.stderr)
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
