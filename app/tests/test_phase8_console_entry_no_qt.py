"""Phase 8 dedicated console entry must remain Qt-free and zero-effect."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from src.version import __build__ as PRODUCT_BUILD
from src.version import __version__ as PRODUCT_VERSION


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _frozen_profile_bundle(
    root: Path,
) -> tuple[Path, Path, Path, dict[str, object]]:
    bundle = root / "Lulu.app"
    frameworks = bundle / "Contents" / "Frameworks"
    resources = bundle / "Contents" / "Resources"
    frameworks.mkdir(parents=True, mode=0o700)
    resources.mkdir(mode=0o700)
    payload: dict[str, object] = {
        "schema": 1,
        "profile": "external_candidate",
        "version": PRODUCT_VERSION,
        "build": PRODUCT_BUILD,
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "product_source_sha256": "3" * 64,
        "app_name": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "artifact_prefix": "Lulu",
        "upload_target": "github-release-candidate",
        "entry": "run_external.py",
    }
    profile = resources / "build_profile.json"
    _write_json(profile, payload)
    (frameworks / "build_profile.json").symlink_to(
        "../Resources/build_profile.json"
    )
    return frameworks, resources, profile, payload


def _write_catalog_resource(resources: Path, app_root: Path) -> Path:
    catalog = resources / "src" / "agent_cli" / "capability_catalog.json"
    catalog.parent.mkdir(parents=True, mode=0o700)
    catalog.write_bytes(
        (app_root / "src" / "agent_cli" / "capability_catalog.json").read_bytes()
    )
    os.chmod(catalog, 0o600)
    return catalog


class Phase8ConsoleEntryNoQtTests(unittest.TestCase):
    def test_frozen_capabilities_reads_the_catalog_from_product_resources(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = __import__("run_agent_cli")

        with tempfile.TemporaryDirectory(
            prefix="Phase8 frozen catalog root 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            frameworks, resources, _profile, _payload = _frozen_profile_bundle(
                root
            )
            _write_json(
                resources / "agent_product_manifest.json",
                json.loads(
                    (
                        app_root / "packaging" / "agent_product_manifest.json"
                    ).read_text(encoding="utf-8")
                ),
            )
            _write_catalog_resource(resources, app_root)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(
                sys,
                "frozen",
                True,
                create=True,
            ), mock.patch.object(
                sys,
                "_MEIPASS",
                str(frameworks),
                create=True,
            ), mock.patch(
                "src.agent_cli.catalog.files",
                side_effect=AssertionError("frozen catalog used source fallback"),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = namespace.main(["capabilities", "--output", "json"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            result = json.loads(stdout.getvalue())["result"]
            self.assertEqual(result["capability_count"], 143)
            self.assertEqual(
                result["state_counts"],
                {"unavailable": 135, "gui_only": 8},
            )

    def test_explicit_catalog_root_rejects_unsafe_or_tampered_resources(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        catalog_module = __import__("src.agent_cli.catalog", fromlist=["catalog"])
        load_catalog = catalog_module.load_capability_catalog

        with tempfile.TemporaryDirectory(
            prefix="Phase8 unsafe frozen catalog root 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            for case in (
                "wrong_shape",
                "ancestor_symlink",
                "src_symlink",
                "agent_cli_symlink",
                "file_symlink",
                "missing",
                "group_writable",
                "world_writable",
                "tampered",
                "byte_tampered",
            ):
                with self.subTest(case=case):
                    case_root = root / case
                    _frameworks, resources, _profile, _payload = (
                        _frozen_profile_bundle(case_root)
                    )
                    catalog = _write_catalog_resource(resources, app_root)
                    explicit_root = resources
                    if case == "wrong_shape":
                        explicit_root = case_root / "ordinary-resources"
                        explicit_root.mkdir(mode=0o700)
                        _write_catalog_resource(explicit_root, app_root)
                    elif case == "ancestor_symlink":
                        alias_parent = case_root / "alias-parent"
                        alias_parent.symlink_to(
                            case_root,
                            target_is_directory=True,
                        )
                        explicit_root = (
                            alias_parent
                            / "Lulu.app"
                            / "Contents"
                            / "Resources"
                        )
                    elif case == "src_symlink":
                        source_dir = resources / "src"
                        actual_dir = resources / "actual-src"
                        source_dir.rename(actual_dir)
                        source_dir.symlink_to(actual_dir.name, target_is_directory=True)
                    elif case == "agent_cli_symlink":
                        agent_dir = resources / "src" / "agent_cli"
                        actual_dir = resources / "src" / "actual-agent-cli"
                        agent_dir.rename(actual_dir)
                        agent_dir.symlink_to(actual_dir.name, target_is_directory=True)
                    elif case == "file_symlink":
                        actual = catalog.with_name("actual-catalog.json")
                        catalog.rename(actual)
                        catalog.symlink_to(actual.name)
                    elif case == "missing":
                        catalog.unlink()
                    elif case == "group_writable":
                        os.chmod(catalog, 0o620)
                    elif case == "world_writable":
                        os.chmod(catalog, 0o602)
                    elif case == "tampered":
                        snapshot = json.loads(catalog.read_text(encoding="utf-8"))
                        snapshot["catalog"]["capabilities"][0][
                            "current_cli_state"
                        ] = "available"
                        _write_json(catalog, snapshot)
                    else:
                        catalog.write_bytes(catalog.read_bytes() + b"\n")

                    with self.assertRaises(RuntimeError):
                        load_catalog(resource_root=explicit_root)

            _frameworks, resources, _profile, _payload = _frozen_profile_bundle(
                root / "foreign-owner"
            )
            _write_catalog_resource(resources, app_root)
            with mock.patch.object(
                catalog_module,
                "os",
                create=True,
            ) as catalog_os:
                catalog_os.getuid.return_value = os.getuid() + 1
                with self.assertRaises(RuntimeError):
                    load_catalog(resource_root=resources)

            with self.assertRaises(RuntimeError):
                load_catalog(resource_root=Path("relative/resources"))

    def test_frozen_console_accepts_only_the_exact_internal_profile_link(self) -> None:
        namespace = __import__("run_agent_cli")
        assert_profile = namespace._assert_packaged_profile

        with tempfile.TemporaryDirectory(
            prefix="Phase8 frozen profile link 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            frameworks, resources, _profile, expected = _frozen_profile_bundle(
                root
            )
            with mock.patch.object(
                sys,
                "frozen",
                True,
                create=True,
            ), mock.patch.object(
                sys,
                "_MEIPASS",
                str(frameworks),
                create=True,
            ):
                self.assertEqual(assert_profile(), expected)
                commands = (
                    ["version", "--output", "json"],
                    ["capabilities", "--output", "json"],
                    ["doctor", "--output", "json"],
                )
                with mock.patch(
                    "src.agent_cli.product_entry.main",
                    return_value=0,
                ) as product_main:
                    for command in commands:
                        self.assertEqual(namespace.main(command), 0)
                self.assertEqual(
                    product_main.call_args_list,
                    [
                        mock.call(command, resource_root=resources)
                        for command in commands
                    ],
                )

    def test_frozen_console_rejects_unsafe_profile_link_shapes(self) -> None:
        namespace = __import__("run_agent_cli")
        assert_profile = namespace._assert_packaged_profile

        with tempfile.TemporaryDirectory(
            prefix="Phase8 unsafe frozen profile link 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            for case in (
                "absolute",
                "escape",
                "wrong_target",
                "chained",
                "cycle",
                "dangling",
                "group_writable",
                "world_writable",
            ):
                with self.subTest(case=case):
                    case_root = root / case
                    frameworks, resources, profile, payload = (
                        _frozen_profile_bundle(case_root)
                    )
                    link = frameworks / "build_profile.json"
                    if case == "absolute":
                        link.unlink()
                        link.symlink_to(profile)
                    elif case == "escape":
                        outside = case_root / "outside.json"
                        _write_json(outside, payload)
                        link.unlink()
                        link.symlink_to("../../../outside.json")
                    elif case == "wrong_target":
                        _write_json(resources / "other.json", payload)
                        link.unlink()
                        link.symlink_to("../Resources/other.json")
                    elif case == "chained":
                        actual = resources / "actual-profile.json"
                        profile.rename(actual)
                        profile.symlink_to(actual.name)
                    elif case == "cycle":
                        profile.unlink()
                        profile.symlink_to("../Frameworks/build_profile.json")
                    elif case == "dangling":
                        profile.unlink()
                    elif case == "group_writable":
                        os.chmod(profile, 0o620)
                    else:
                        os.chmod(profile, 0o602)

                    with self.assertRaises(RuntimeError):
                        assert_profile(resource_root=frameworks)

            real_parent = root / "ancestor_symlink" / "real-parent"
            frameworks, _resources, _profile, _payload = _frozen_profile_bundle(
                real_parent
            )
            alias_parent = root / "ancestor_symlink" / "alias-parent"
            alias_parent.symlink_to(real_parent, target_is_directory=True)
            aliased_frameworks = (
                alias_parent / "Lulu.app" / "Contents" / "Frameworks"
            )
            self.assertEqual(
                aliased_frameworks.resolve(strict=True),
                frameworks,
            )
            with self.assertRaises(RuntimeError):
                assert_profile(resource_root=aliased_frameworks)

    def test_console_product_entry_reports_shared_identity_without_importing_qt(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        runner = app_root / "run_agent_cli.py"
        self.assertTrue(runner.is_file(), "Phase 8 frozen Agent CLI shim is missing")
        source = runner.read_text(encoding="utf-8")
        for forbidden in ("PySide6", "QApplication", "MainWindow", "src.ui", "src.main"):
            self.assertNotIn(forbidden, source)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            audit_path = root / "imports.json"
            _write_json(
                root / "build_profile.json",
                {
                    "schema": 1,
                    "profile": "external_candidate",
                    "version": "0.3.1",
                    "build": "20260815.3",
                    "source_commit": "1" * 40,
                    "source_tree": "2" * 40,
                    "product_source_sha256": "3" * 64,
                    "app_name": "Lulu",
                    "bundle_id": "com.lulu.transcribe",
                    "artifact_prefix": "Lulu",
                    "upload_target": "github-release-candidate",
                    "entry": "run_external.py",
                },
            )
            _write_json(
                root / "agent_product_manifest.json",
                {
                    "schema_id": "lulu.agent-product-manifest/v1",
                    "schema_revision": 1,
                    "product_family": "Lulu",
                    "product_version": "0.3.1",
                    "product_build": "20260815.3",
                    "cli_protocol_version": "1.0.0",
                    "installation_identity_scope": "shared_lulu_installation",
                    "installation_identity": {
                        "scope": "shared_lulu_installation",
                        "status": "unverified_until_real_install",
                        "agent_specific_copy": "forbidden",
                    },
                    "supported_profiles": ["external_candidate", "trusted_test"],
                    "adapters": {
                        "gui": {
                            "executable": "Contents/MacOS/Lulu",
                            "mode": "windowed",
                            "stdio": False,
                        },
                        "agent_cli": {
                            "executable": "Contents/MacOS/lulu-agent-cli",
                            "mode": "console",
                            "stdio": True,
                        },
                    },
                    "shared_runtime": {
                        "analysis": "single",
                        "python": "single",
                        "core": "single",
                        "resources": "single",
                        "storage_layout": "lulu.shared-storage/v1",
                    },
                    "trust_evidence": {
                        "bundle_signature": "unverified",
                        "team_id": "unverified",
                        "acl": "unverified",
                        "stdio": "source_contract_only",
                        "package_size": "planning_only",
                    },
                },
            )
            script = (
                "import json,sys\n"
                "from pathlib import Path\n"
                "from run_agent_cli import _assert_packaged_profile\n"
                "from src.agent_cli.product_entry import main\n"
                f"root=Path({str(root)!r})\n"
                "assert _assert_packaged_profile(resource_root=root)['profile']=='external_candidate'\n"
                "code=main(['version','--output','json'],resource_root=root)\n"
                "forbidden=sorted(name for name in sys.modules if "
                "name=='PySide6' or name.startswith('PySide6.') or "
                "name=='src.ui' or name.startswith('src.ui.'))\n"
                f"Path({str(audit_path)!r}).write_text(json.dumps(forbidden),encoding='utf-8')\n"
                "raise SystemExit(code)\n"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(app_root)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=app_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(completed.stdout.count("\n"), 1)
            payload = json.loads(completed.stdout)
            identity = payload["result"]["product_identity"]
            self.assertEqual(identity["adapter_id"], "agent_cli")
            self.assertEqual(identity["shared"]["bundle_id"], "com.lulu.transcribe")
            self.assertEqual(
                identity["shared"].get("installation_identity"),
                {
                    "scope": "shared_lulu_installation",
                    "status": "unverified_until_real_install",
                    "agent_specific_copy": "forbidden",
                },
            )
            self.assertEqual(
                identity["shared"]["protected_capabilities"],
                {
                    "state": "unavailable",
                    "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
                },
            )
            self.assertEqual(json.loads(audit_path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
