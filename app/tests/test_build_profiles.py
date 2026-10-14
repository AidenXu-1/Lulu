from __future__ import annotations

import ast
import json
import hashlib
import marshal
import os
from pathlib import Path
import runpy
import shutil
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1]
PROFILE_GATE = runpy.run_path(str(APP / "packaging/profile_gate.py"))
SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40


class BuildProfileTests(unittest.TestCase):
    def test_trusted_test_keeps_gui_only_and_safe_e2e_has_independent_profile(self):
        trusted = PROFILE_GATE["PROFILES"]["trusted_test"]
        self.assertEqual(trusted["package_surface"], "gui_only/v1")
        self.assertNotIn("trusted_test", PROFILE_GATE["APPROVED_SECONDARY_ENTRY_CONTRACTS"])
        safe = PROFILE_GATE["PROFILES"]["trusted_test_safe_e2e"]
        self.assertEqual(safe["package_surface"], "isolated_local_transcription_e2e/v1")
        self.assertNotEqual(safe["bundle_id"], trusted["bundle_id"])
        self.assertNotIn("trusted_test_safe_e2e", PROFILE_GATE["APPROVED_SECONDARY_ENTRY_CONTRACTS"])
        dirty = PROFILE_GATE["scan_collected_members"](
            "trusted_test",
            [
                ("run_agent_cli", ""),
                ("src.agent_cli.main", ""),
                ("src.core.cli_runtime.factory", ""),
                ("src.core.license.store", ""),
                ("keyring.backends.macOS", ""),
            ],
            [
                "agent_product_manifest.json",
                "src/agent_cli/capability_catalog.json",
                "skills/lulu-agent/SKILL.md",
                "src/agent_cli/gui_host_runtime_hook.py",
            ],
        )
        self.assertEqual(dirty["forbidden_count"], 9)

    def test_r20_production_target_authority_has_no_caller_path_seams(self):
        app_root = Path(__file__).resolve().parents[1]
        files = (
            app_root / "packaging/phase10_install_candidate.py",
            app_root / "packaging/phase10_install_versioned_ticket.py",
            app_root / "src/core/cli_runtime/macos_keychain_acl.py",
            app_root / "src/core/cli_runtime/factory.py",
        )
        forbidden_parameters = {
            "app_target",
            "target_path",
            "destination",
            "profile_override",
        }
        production_entry_names = {
            "install_candidate",
            "rollback_install",
            "install_versioned_ticket",
            "bind_versioned_keychain_acl",
            "_build_verified_trusted_test_runtime",
        }
        for source_path in files:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in production_entry_names:
                    parameters = {argument.arg for argument in node.args.args + node.args.kwonlyargs}
                    self.assertTrue(
                        forbidden_parameters.isdisjoint(parameters),
                        f"{source_path.name}:{node.name}:{sorted(parameters & forbidden_parameters)}",
                    )

    def test_gui_host_runtime_hook_keeps_existing_profile_executable_names(self):
        spec = (APP / "packaging/Lulu.spec").read_text(encoding="utf-8")
        manifest = json.loads(
            (APP / "packaging/agent_product_manifest.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn('name=PROFILE_META["artifact_prefix"]', spec)
        self.assertIn('"artifact_prefix": "Lulu-Trusted-Test"', spec)
        self.assertIn('"artifact_prefix": "Lulu"', spec)
        self.assertIn('"agent_cli", "gui_host_runtime_hook.py"', spec)
        self.assertEqual(
            manifest["adapters"]["gui"]["executable"],
            "Contents/MacOS/{artifact_prefix}",
        )

    @staticmethod
    def _synthetic_archive_reader(executable: Path, profile: str):
        product_source = json.loads(
            (
                executable.parent.parent
                / "Resources"
                / "product_source.json"
            ).read_text(encoding="utf-8")
        )
        return (
            ["src.main"],
            [],
            {
                "format": "pyinstaller_carchive",
                "carchive_count": 2,
                "pyz_archive_count": 1,
                "pyz_count": 1,
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "app_entry_scripts": [product_source["entry_module"]],
                "product_entry_scripts": [product_source["entry_module"]],
                "runtime_hook_scripts": ["gui_host_runtime_hook"],
                "product_code_sha256": {
                    name: record["code_sha256"]
                    for name, record in product_source["modules"].items()
                },
            },
        )

    def _synthetic_external_app(self, root: Path) -> tuple[Path, Path]:
        app = root / "Lulu.app"
        resources = app / "Contents/Resources"
        macos = app / "Contents/MacOS"
        resources.mkdir(parents=True)
        macos.mkdir()
        executable = macos / "Lulu"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        profile = resources / "build_profile.json"
        product_source = PROFILE_GATE["product_source_manifest"](
            "external_candidate",
            source_commit=SOURCE_COMMIT,
            source_tree=SOURCE_TREE,
            source_loader=lambda repo_path: (
                APP / repo_path.removeprefix("app/")
            ).read_bytes(),
        )
        product_source_digest = PROFILE_GATE["write_product_source"](
            resources / "product_source.json",
            product_source,
        )
        for bundle_path, record in product_source["resources"].items():
            destination = resources / bundle_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                (APP / record["path"].removeprefix("app/")).read_bytes()
            )
        PROFILE_GATE["write_profile"](
            profile,
            "external_candidate",
            version="1",
            build="2",
            source_commit=SOURCE_COMMIT,
            source_tree=SOURCE_TREE,
            product_source_sha256=product_source_digest,
        )
        profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
        (resources / "AUTHORSHIP.json").write_text(
            json.dumps(
                {
                    "build_profile": "external_candidate",
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
                    "CFBundleIdentifier": "com.lulu.transcribe",
                    "CFBundleName": "Lulu",
                    "CFBundleExecutable": "Lulu",
                    "CFBundlePackageType": "APPL",
                },
                handle,
            )
        from src.core.agent_entry.delivery_identity import prepare_delivery_marker, prepare_link_policy
        prepare_delivery_marker(app)
        prepare_link_policy(app)
        analysis = root / "analysis.json"
        PROFILE_GATE["write_analysis_receipt"](
            analysis,
            "external_candidate",
            [("src.main", str(APP / "src/main.py"))],
            [
                "AUTHORSHIP.json",
                "build_profile.json",
                "component_delivery.v2.json",
                "agent_links.v2.json",
                "product_source.json",
                *product_source["resources"],
            ],
            profile_sha,
        )
        self._resign(app)
        return app, analysis

    def _resign(self, app: Path) -> None:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
        )

    def test_profile_names_metadata_and_upload_targets_are_mutually_exclusive(self):
        kwargs = {
            "version": "1",
            "build": "2",
            "source_commit": SOURCE_COMMIT,
            "source_tree": SOURCE_TREE,
        }
        internal = PROFILE_GATE["profile_payload"]("internal_test", **kwargs)
        trusted = PROFILE_GATE["profile_payload"]("trusted_test", **kwargs)
        external = PROFILE_GATE["profile_payload"]("external_candidate", **kwargs)
        for key in ("app_name", "bundle_id", "artifact_prefix", "entry"):
            self.assertNotEqual(internal[key], external[key], key)
            self.assertNotEqual(internal[key], trusted[key], key)
            self.assertNotEqual(trusted[key], external[key], key)
        self.assertEqual(internal["upload_target"], "none")
        self.assertEqual(trusted["upload_target"], "none")
        self.assertEqual(external["upload_target"], "github-release-candidate")

    def test_all_profiles_have_recomputable_product_source_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for profile in ("internal_test", "trusted_test", "external_candidate"):
                with self.subTest(profile=profile):
                    resources = root / profile
                    resources.mkdir()
                    manifest = PROFILE_GATE["product_source_manifest"](
                        profile,
                        source_commit=SOURCE_COMMIT,
                        source_tree=SOURCE_TREE,
                        source_loader=lambda repo_path: (
                            APP / repo_path.removeprefix("app/")
                        ).read_bytes(),
                    )
                    for bundle_path, record in manifest["resources"].items():
                        destination = resources / bundle_path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(
                            (
                                APP
                                / record["path"].removeprefix("app/")
                            ).read_bytes()
                        )
                    checks, _summary = PROFILE_GATE["_product_source_checks"](
                        manifest,
                        profile,
                        {
                            "app_entry_scripts": [manifest["entry_module"]],
                            "product_entry_scripts": [manifest["entry_module"]],
                            "runtime_hook_scripts": ["gui_host_runtime_hook"],
                            "product_code_sha256": {
                                name: record["code_sha256"]
                                for name, record in manifest["modules"].items()
                            },
                        },
                        resources,
                        expected_manifest=manifest,
                    )
                    self.assertTrue(all(checks.values()), checks)

    def test_product_code_fingerprint_survives_archive_round_trip_and_rejects_drift(self):
        source = (
            b"VALUES = {'beta': 2, 'alpha': 1}\n"
            b"def render(value):\n"
            b"    return VALUES.get(value, 0) + 1\n"
        )
        code = compile(source, "src/example.py", "exec", dont_inherit=True, optimize=0)
        round_tripped = marshal.loads(marshal.dumps(code))
        stable_digest = PROFILE_GATE["_stable_code_sha256"](code)
        self.assertEqual(
            stable_digest,
            PROFILE_GATE["_stable_code_sha256"](round_tripped),
        )
        self.assertEqual(
            stable_digest,
            PROFILE_GATE["_code_sha256"](source, "app/src/example.py"),
        )

        drifted = source.replace(b"+ 1", b"+ 2")
        self.assertNotEqual(
            stable_digest,
            PROFILE_GATE["_code_sha256"](drifted, "app/src/example.py"),
        )

    def test_source_identity_is_full_git_object_and_ignores_git_environment_overrides(self):
        expected_commit = subprocess.run(
            ["/usr/bin/git", "-C", str(APP), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        expected_tree = subprocess.run(
            ["/usr/bin/git", "-C", str(APP), "rev-parse", "--verify", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": "/tmp/forged-git-dir",
                "GIT_WORK_TREE": "/tmp/forged-work-tree",
                "GIT_INDEX_FILE": "/tmp/forged-index",
                "LULU_SOURCE_COMMIT": "c" * 40,
                "LULU_SOURCE_TREE": "d" * 40,
            },
            clear=False,
        ):
            source_commit, source_tree = PROFILE_GATE["source_identity"](
                APP,
                require_clean=False,
            )
        self.assertEqual((source_commit, source_tree), (expected_commit, expected_tree))
        self.assertRegex(source_commit, r"^[0-9a-f]{40}$")
        self.assertRegex(source_tree, r"^[0-9a-f]{40}$")

        for commit, tree in (
            ("a" * 39, SOURCE_TREE),
            ("g" * 40, SOURCE_TREE),
            (SOURCE_COMMIT, "b" * 39),
            (SOURCE_COMMIT, "G" * 40),
        ):
            with self.subTest(commit=commit, tree=tree), self.assertRaises(ValueError):
                PROFILE_GATE["profile_payload"](
                    "trusted_test",
                    version="1",
                    build="2",
                    source_commit=commit,
                    source_tree=tree,
                )

    def test_source_identity_refuses_dirty_input_and_cli_identity_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
            app_root = root / "app"
            authorship = app_root / "packaging/AUTHORSHIP.json"
            authorship.parent.mkdir(parents=True)
            source = app_root / "source.py"
            source.write_text("value = 1\n", encoding="utf-8")
            authorship.write_text('{"fixture":true}\n', encoding="utf-8")
            subprocess.run(["/usr/bin/git", "-C", str(root), "add", "app"], check=True)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Lulu Test",
                    "-c",
                    "user.email=lulu-test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            commit, tree = PROFILE_GATE["source_identity"](app_root)
            self.assertRegex(commit, r"^[0-9a-f]{40}$")
            self.assertRegex(tree, r"^[0-9a-f]{40}$")

            authorship.write_text('{"fixture":"generated"}\n', encoding="utf-8")
            self.assertEqual(
                PROFILE_GATE["source_identity"](
                    app_root,
                    allowed_dirty_paths=frozenset(
                        {"app/packaging/AUTHORSHIP.json"}
                    ),
                ),
                (commit, tree),
            )
            source.write_text("value = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "未提交改动"):
                PROFILE_GATE["source_identity"](
                    app_root,
                    allowed_dirty_paths=frozenset(
                        {"app/packaging/AUTHORSHIP.json"}
                    ),
                )

        rejected = subprocess.run(
            [
                sys.executable,
                str(APP / "packaging/profile_gate.py"),
                "create",
                "--profile",
                "trusted_test",
                "--version",
                "1",
                "--build",
                "2",
                "--build-venv-root",
                str(APP),
                "--source-commit",
                "c" * 40,
                "--source-tree",
                "d" * 40,
                "--output",
                "/tmp/forbidden-profile.json",
                "--product-source-output",
                "/tmp/forbidden-product-source.json",
            ],
            cwd=APP,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unrecognized arguments", rejected.stderr)

    def test_machine_receipt_binds_profile_hash_to_valid_bundle_seal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            result = PROFILE_GATE["_verify_app_with_reader"](
                app,
                "external_candidate",
                analysis,
                root / "receipt.json",
                self._synthetic_archive_reader,
                expected_product_source=json.loads(
                    (
                        app
                        / "Contents/Resources/product_source.json"
                    ).read_text(encoding="utf-8")
                ),
            )
            self.assertTrue(result["ok"])
            self.assertTrue(result["profile_in_bundle_seal"])
            self.assertEqual(result["inventory"]["difference_count"], 0)
            self.assertEqual(result["digests"]["analysis_scope_inventory_sha256"], result["digests"]["analysis_inventory_sha256"])
            self.assertIn("bundle_executable_inventory_sha256", result["digests"])
            self.assertEqual(result["inventory"]["bundle_executable_count"], 1)
            self.assertIsNotNone(result["digests"]["code_resources_sha256"])
            self.assertTrue(result["checks"]["release_privacy_valid"])
            self.assertEqual(result["privacy"]["finding_count"], 0)
            self.assertTrue((root / "receipt.json").is_file())

    def test_product_source_binding_rejects_foreign_entry_missing_module_and_code_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            product_source = json.loads(
                (
                    app / "Contents/Resources/product_source.json"
                ).read_text(encoding="utf-8")
            )

            valid = PROFILE_GATE["_verify_app_with_reader"](
                app,
                "external_candidate",
                analysis,
                root / "valid.json",
                self._synthetic_archive_reader,
                expected_product_source=product_source,
            )
            self.assertTrue(valid["checks"]["product_entry_exact"])
            self.assertTrue(valid["checks"]["required_product_modules_present"])
            self.assertTrue(valid["checks"]["product_code_matches_source"])
            self.assertTrue(valid["checks"]["product_source_matches_fixed_tree"])

            def forged_reader(kind):
                def read(executable: Path, profile: str):
                    modules, findings, meta = self._synthetic_archive_reader(
                        executable,
                        profile,
                    )
                    meta = dict(meta)
                    if kind == "foreign_entry":
                        meta["app_entry_scripts"] = ["foreign_app"]
                        meta["product_entry_scripts"] = ["foreign_app"]
                    elif kind == "missing_module":
                        code = dict(meta["product_code_sha256"])
                        code.pop("src.main")
                        meta["product_code_sha256"] = code
                    elif kind == "code_drift":
                        code = dict(meta["product_code_sha256"])
                        code["src.main"] = "f" * 64
                        meta["product_code_sha256"] = code
                    return modules, findings, meta

                return read

            for kind, failed_check in (
                ("foreign_entry", "product_entry_exact"),
                ("missing_module", "required_product_modules_present"),
                ("code_drift", "product_code_matches_source"),
            ):
                output = root / f"{kind}.json"
                with self.subTest(kind=kind), self.assertRaises(SystemExit):
                    PROFILE_GATE["_verify_app_with_reader"](
                        app,
                        "external_candidate",
                        analysis,
                        output,
                        forged_reader(kind),
                        expected_product_source=product_source,
                    )
                receipt = json.loads(output.read_text(encoding="utf-8"))
                self.assertFalse(receipt["checks"][failed_check])

            missing_resource = next(iter(product_source["resources"]))
            (app / "Contents/Resources" / missing_resource).unlink()
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "missing-resource.json",
                    self._synthetic_archive_reader,
                    expected_product_source=product_source,
                )
            receipt = json.loads(
                (root / "missing-resource.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                receipt["checks"]["product_resources_match_source"]
            )

    def test_product_source_binding_rejects_self_consistent_arbitrary_spec_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            resources = app / "Contents/Resources"
            product_path = resources / "product_source.json"
            expected_product_source = json.loads(
                product_path.read_text(encoding="utf-8")
            )
            forged = json.loads(json.dumps(expected_product_source))
            forged["spec"]["sha256"] = "e" * 64
            forged_digest = PROFILE_GATE["write_product_source"](
                product_path,
                forged,
            )
            profile_path = resources / "build_profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["product_source_sha256"] = forged_digest
            profile_path.write_text(
                json.dumps(
                    profile,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            profile_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()
            authorship_path = resources / "AUTHORSHIP.json"
            authorship = json.loads(authorship_path.read_text(encoding="utf-8"))
            authorship["build_profile_sha256"] = profile_sha
            authorship["product_source_sha256"] = forged_digest
            authorship_path.write_text(json.dumps(authorship), encoding="utf-8")
            PROFILE_GATE["write_analysis_receipt"](
                analysis,
                "external_candidate",
                [("src.main", str(APP / "src/main.py"))],
                [
                    "AUTHORSHIP.json",
                    "build_profile.json",
                        "product_source.json",
                    *forged["resources"],
                ],
                profile_sha,
            )
            self._resign(app)

            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "forged-spec.json",
                    self._synthetic_archive_reader,
                    expected_product_source=expected_product_source,
                )
            receipt = json.loads(
                (root / "forged-spec.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                receipt["checks"]["product_source_matches_fixed_tree"]
            )

    def test_machine_receipt_binds_profile_and_authorship_to_same_source_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            authorship = app / "Contents/Resources/AUTHORSHIP.json"
            payload = json.loads(authorship.read_text(encoding="utf-8"))
            payload["source_tree"] = "c" * 40
            authorship.write_text(json.dumps(payload), encoding="utf-8")
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "receipt.json",
                    self._synthetic_archive_reader,
                )
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["authorship_source_tree"])
            self.assertEqual(receipt["source_identity"]["source_commit"], SOURCE_COMMIT)
            self.assertEqual(receipt["source_identity"]["source_tree"], SOURCE_TREE)
            self.assertEqual(
                receipt["source_identity"]["build_input_source_commit"],
                SOURCE_COMMIT,
            )
            self.assertEqual(
                receipt["source_identity"]["build_input_source_tree"],
                SOURCE_TREE,
            )

    def test_machine_receipt_rejects_source_identity_that_differs_from_build_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "receipt.json",
                    self._synthetic_archive_reader,
                    expected_source_identity=("c" * 40, "d" * 40),
                )
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["source_matches_build_input"])
            self.assertEqual(
                receipt["source_identity"]["build_input_source_commit"],
                "c" * 40,
            )
            self.assertEqual(
                receipt["source_identity"]["build_input_source_tree"],
                "d" * 40,
            )

    def test_profile_gate_rejects_missing_or_nonregular_source_seal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, _analysis = self._synthetic_external_app(root)
            resources = app / "Contents/Resources"
            profile = resources / "build_profile.json"
            payload = json.loads(profile.read_text(encoding="utf-8"))
            payload.pop("source_commit")
            profile.write_text(json.dumps(payload), encoding="utf-8")
            profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
            analysis = root / "analysis-missing-source.json"
            PROFILE_GATE["write_analysis_receipt"](
                analysis,
                "external_candidate",
                [("src.main", str(APP / "src/main.py"))],
                ["AUTHORSHIP.json", "build_profile.json"],
                profile_sha,
            )
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "missing-receipt.json",
                    self._synthetic_archive_reader,
                )
            receipt = json.loads((root / "missing-receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["source_identity_valid"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            profile = app / "Contents/Resources/build_profile.json"
            outside = root / "outside-profile.json"
            outside.write_bytes(profile.read_bytes())
            profile.unlink()
            profile.symlink_to(outside)
            with self.assertRaisesRegex(SystemExit, "普通文件"):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "symlink-receipt.json",
                    self._synthetic_archive_reader,
                )

    def test_final_bundle_privacy_gate_rejects_builder_path_even_when_inventory_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, _old_analysis = self._synthetic_external_app(root)
            leak = app / "Contents/Resources/dependency.bin"
            leak.write_bytes(f"compiled at {Path.home()}/private/workspace/module.py".encode("utf-8"))
            profile = app / "Contents/Resources/build_profile.json"
            analysis = root / "analysis-current.json"
            resources = sorted(
                path.relative_to(app / "Contents/Resources").as_posix()
                for path in (app / "Contents/Resources").rglob("*")
                if path.is_file() or path.is_symlink()
            )
            PROFILE_GATE["write_analysis_receipt"](
                analysis,
                "external_candidate",
                [("src.main", str(APP / "src/main.py"))],
                resources,
                hashlib.sha256(profile.read_bytes()).hexdigest(),
            )
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "receipt.json",
                    self._synthetic_archive_reader,
                )
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["release_privacy_valid"])
            self.assertEqual(
                receipt["privacy"]["findings"],
                [{"kind": "builder_path", "value": "Contents/Resources/dependency.bin"}],
            )
            self.assertNotIn(str(Path.home()), json.dumps(receipt, ensure_ascii=False))


    def test_analysis_projection_matches_pyinstaller_bundle_symlink_roots(self):
        projected = PROFILE_GATE["_project_analysis_resources"](
            {
                "Crypto/Cipher/_raw_aes.abi3.so",
                "PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore",
                "plain/data.json",
            },
            [
                {"path": "Crypto", "type": "symlink"},
                {"path": "PySide6/Qt/lib", "type": "symlink"},
                {"path": "plain/data.json", "type": "file"},
            ],
        )
        self.assertEqual(projected, {"Crypto", "PySide6/Qt/lib", "plain/data.json"})

    def test_build_scripts_bind_receipt_to_real_pyinstaller_spec_workdir(self):
        spec = (APP / "packaging/Lulu.spec").read_text(encoding="utf-8")
        build = (APP / "packaging/build_app.sh").read_text(encoding="utf-8")
        self.assertIn('os.path.join(workpath, "profile-analysis.json")', spec)
        self.assertNotIn("WORKPATH", spec)
        self.assertIn('--analysis-receipt "$BUILD_ROOT/Lulu/profile-analysis.json"', build)
        self.assertIn('["source_commit"]', build)
        self.assertIn('["source_tree"]', build)
        self.assertIn('--source-commit "$SOURCE_COMMIT"', build)
        self.assertIn('--source-tree "$SOURCE_TREE"', build)
        self.assertIn("--product-source-output product_source.json", build)
        self.assertIn('--product-source-sha256 "$PRODUCT_SOURCE_SHA256"', build)
        self.assertIn(
            'json.load(open("build_profile.json", encoding="utf-8"))["product_source_sha256"]',
            build,
        )
        self.assertNotIn("shasum -a 256 product_source.json", build)
        self.assertIn("-path '*/?*.dist-info/*' -exec chmod 0644 {} +", build)
        self.assertIn('/usr/bin/codesign --force --deep --sign - "$APP_PATH"', build)
        self.assertIn('datas += [(PRODUCT_SOURCE_FILE, ".")]', spec)
        self.assertNotIn("LULU_SOURCE_COMMIT", build)
        self.assertNotIn("LULU_SOURCE_TREE", build)
        self.assertNotIn("git rev-parse --short", build)

    def test_authorship_requires_and_records_full_source_identity(self):
        script = APP / "packaging/make_authorship.py"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "AUTHORSHIP.json"
            base_args = [
                sys.executable,
                str(script),
                "--version",
                "1",
                "--build-time",
                "2026-07-28T00:00:00Z",
                "--git-commit",
                "abc1234",
                "--build-profile",
                "trusted_test",
                "--build-profile-sha256",
                "c" * 64,
                "--source-commit",
                SOURCE_COMMIT,
                "--source-tree",
                SOURCE_TREE,
                "--product-source-sha256",
                "d" * 64,
                "--output",
                str(output),
            ]
            subprocess.run(base_args, check=True, capture_output=True, text=True)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_commit"], SOURCE_COMMIT)
            self.assertEqual(payload["source_tree"], SOURCE_TREE)
            self.assertEqual(payload["product_source_sha256"], "d" * 64)

            invalid = list(base_args)
            invalid[invalid.index(SOURCE_COMMIT)] = "a" * 39
            failed = subprocess.run(invalid, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("40位", failed.stderr)

    def test_external_bundle_rejects_extra_macos_executables_and_markers(self):
        cases = (
            ("marked", b"internal_test run_internal MockLicenseTransport\n", 0o755),
            ("unmarked", b"plain helper\n", 0o755),
            ("script", b"#!/bin/sh\nexit 0\n", 0o755),
            ("macho", Path("/bin/echo"), 0o755),
        )
        for label, source, mode in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                app, analysis = self._synthetic_external_app(root)
                helper = app / "Contents/MacOS/diagnostic-helper"
                if isinstance(source, Path):
                    shutil.copyfile(source, helper)
                else:
                    helper.write_bytes(source)
                helper.chmod(mode)
                self._resign(app)
                with self.assertRaises(SystemExit):
                    PROFILE_GATE["_verify_app_with_reader"](
                        app,
                        "external_candidate",
                        analysis,
                        root / "receipt.json",
                        self._synthetic_archive_reader,
                    )
                receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
                self.assertFalse(receipt["checks"]["bundle_executable_contract_valid"])
                self.assertTrue(any(item["kind"] == "unknown_executable_member" for item in receipt["final_findings"]))

    def test_external_bundle_allows_explicit_framework_macho_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            nested = app / "Contents/Frameworks/example.cpython-311-darwin.so"
            nested.parent.mkdir()
            shutil.copyfile("/bin/echo", nested)
            nested.chmod(0o755)
            self._resign(app)
            result = PROFILE_GATE["_verify_app_with_reader"](
                app,
                "external_candidate",
                analysis,
                root / "receipt.json",
                self._synthetic_archive_reader,
            )
            self.assertTrue(result["ok"])
            nested_record = next(item for item in result["inventory"]["bundle_executables"] if item["path"].endswith(".so"))
            self.assertEqual(nested_record["contract"], "framework_macho")
            self.assertTrue(nested_record["allowed"])

    def test_production_verify_rejects_plain_or_damaged_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["verify_app"](app, "external_candidate", analysis, root / "plain-receipt.json")

            executable = app / "Contents/MacOS/Lulu"
            executable.write_bytes(b"damaged-pyinstaller-cookie\x00\x00\x00")
            executable.chmod(0o755)
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["verify_app"](app, "external_candidate", analysis, root / "damaged-receipt.json")

    def test_production_archive_reader_rejects_empty_no_pyz_and_broken_pyz(self):
        executable = APP / "run_external.py"

        class EmptyArchive:
            def __init__(self, _path):
                self.toc = {}

        class NoPyzArchive:
            def __init__(self, _path):
                self.toc = {"run_external": (0, 0, 0, 0, "s")}

            def extract(self, _name):
                return b""

        class BrokenPyzArchive(NoPyzArchive):
            def __init__(self, _path):
                self.toc = {
                    "run_external": (0, 0, 0, 0, "s"),
                    "PYZ.pyz": (0, 0, 0, 0, "z"),
                }

            def open_embedded_archive(self, _name):
                raise RuntimeError("broken pyz")

        for reader in (EmptyArchive, NoPyzArchive, BrokenPyzArchive):
            with self.subTest(reader=reader.__name__), patch("PyInstaller.archive.readers.CArchiveReader", reader):
                with self.assertRaises(SystemExit):
                    PROFILE_GATE["_archive_inventory"](executable, "external_candidate")

    def test_r17_real_failed_app_separates_product_entries_from_runtime_hook(self):
        raw_app = os.environ.get("LULU_R17_FAILED_APP", "")
        if not raw_app:
            self.skipTest("set LULU_R17_FAILED_APP for the preserved real archive gate")
        app = Path(raw_app).resolve(strict=True)
        contracts = [
            {
                "module": "run_trusted_test",
                "relative_path": "Contents/MacOS/Lulu-Trusted-Test",
                "contract": "main_executable",
                "path": app / "Contents/MacOS/Lulu-Trusted-Test",
            },
            {
                "module": "run_agent_cli",
                "relative_path": "Contents/MacOS/lulu-agent-cli",
                "contract": "agent_cli_executable",
                "path": app / "Contents/MacOS/lulu-agent-cli",
            },
        ]

        for contract in contracts:
            with self.subTest(executable=contract["relative_path"]):
                _modules, findings, metadata = PROFILE_GATE["_archive_inventory"](
                    contract["path"],
                    "trusted_test",
                )
                self.assertEqual(findings, [])
                self.assertEqual(
                    metadata.get(
                        "product_entry_scripts",
                        metadata.get("app_entry_scripts", []),
                    ),
                    [contract["module"]],
                )
                self.assertEqual(
                    metadata.get("runtime_hook_scripts", []),
                    ["gui_host_runtime_hook"],
                )

        _modules, findings, metadata = PROFILE_GATE[
            "_combined_archive_inventory"
        ](contracts, "trusted_test", PROFILE_GATE["_archive_inventory"])
        self.assertEqual(findings, [])
        self.assertEqual(
            metadata["product_entry_scripts"],
            ["run_agent_cli", "run_trusted_test"],
        )
        self.assertEqual(
            metadata["runtime_hook_scripts"],
            ["gui_host_runtime_hook"],
        )

    def test_r17_archive_roles_reject_missing_foreign_double_and_unknown_scripts(self):
        executable = APP / "run_trusted_test.py"
        contract = {
            "module": "run_trusted_test",
            "relative_path": "Contents/MacOS/Lulu-Trusted-Test",
            "contract": "main_executable",
            "path": executable,
        }

        class FakePyz:
            toc = {"src.trusted_main": (0, 0, 0, 0, "m")}

            @staticmethod
            def extract(_name, raw=False):
                del raw
                return marshal.dumps(compile("pass", "src/trusted_main.py", "exec"))

        def inspect_scripts(*scripts):
            class FakeArchive:
                def __init__(self, _path):
                    self.toc = {
                        name: (0, 0, 0, 0, "s")
                        for name in scripts
                    }
                    self.toc["PYZ.pyz"] = (0, 0, 0, 0, "z")

                @staticmethod
                def extract(name):
                    return marshal.dumps(compile("pass", f"{name}.py", "exec"))

                @staticmethod
                def open_embedded_archive(_name):
                    return FakePyz()

            with patch("PyInstaller.archive.readers.CArchiveReader", FakeArchive):
                inventory = PROFILE_GATE["_archive_inventory"](
                    executable,
                    "trusted_test",
                )
            return inventory

        exact = inspect_scripts("run_trusted_test")
        self.assertEqual(exact[2]["product_entry_scripts"], ["run_trusted_test"])
        self.assertEqual(
            exact[2]["runtime_hook_scripts"],
            [],
        )
        self.assertEqual(
            PROFILE_GATE["_combined_archive_inventory"](
                [contract],
                "trusted_test",
                lambda *_args: exact,
            )[1],
            [],
        )

        cases = {
            "missing_product": (),
            "foreign_product": ("foreign_entry",),
            "double_product": (
                "run_trusted_test",
                "run_agent_cli",
            ),
            "unknown_script": (
                "run_trusted_test",
                "unexpected_script",
            ),
            "unexpected_runtime_hook": (
                "gui_host_runtime_hook",
                "run_trusted_test",
            ),
        }
        for name, scripts in cases.items():
            inventory = inspect_scripts(*scripts)
            _modules, findings, _metadata = PROFILE_GATE[
                "_combined_archive_inventory"
            ]([contract], "trusted_test", lambda *_args, value=inventory: value)
            with self.subTest(case=name):
                self.assertTrue(
                    any(
                        finding["kind"]
                        in {"archive_entry_contract", "archive_runtime_hook_contract"}
                        for finding in findings
                    )
                )

        with self.assertRaises(TypeError):
            PROFILE_GATE["_archive_inventory"](
                executable,
                "trusted_test",
                {"caller_added_hook"},
            )

    def test_gui_only_trusted_archive_requires_an_exact_empty_runtime_hook_set(self):
        executable = APP / "run_trusted_test.py"
        contract = {
            "module": "run_trusted_test",
            "relative_path": "Contents/MacOS/Lulu-Trusted-Test",
            "contract": "main_executable",
            "path": executable,
        }
        real_gui_only_metadata = {
            "format": "pyinstaller_carchive",
            "carchive_count": 1,
            "pyz_archive_count": 1,
            "pyz_count": 1,
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "product_entry_scripts": ["run_trusted_test"],
            "runtime_hook_scripts": [],
            "product_code_sha256": {},
        }

        _modules, findings, metadata = PROFILE_GATE[
            "_combined_archive_inventory"
        ](
            [contract],
            "trusted_test",
            lambda *_args: (["src.trusted_main"], [], real_gui_only_metadata),
        )

        self.assertFalse(
            any(
                finding["kind"] == "archive_runtime_hook_contract"
                for finding in findings
            ),
            findings,
        )
        self.assertEqual(metadata["runtime_hook_scripts"], [])

        exact_contracts = {
            "internal_test": ("run_internal", []),
            "trusted_test": ("run_trusted_test", []),
            "external_candidate": ("run_external", ["gui_host_runtime_hook"]),
        }
        for profile, (entry_module, expected_hooks) in exact_contracts.items():
            profile_contract = dict(contract, module=entry_module)
            profile_metadata = dict(
                real_gui_only_metadata,
                product_entry_scripts=[entry_module],
                runtime_hook_scripts=expected_hooks,
            )
            with self.subTest(profile=profile, contract="exact"):
                self.assertFalse(
                    any(
                        finding["kind"] == "archive_runtime_hook_contract"
                        for finding in PROFILE_GATE["_combined_archive_inventory"](
                            [profile_contract],
                            profile,
                            lambda *_args, value=profile_metadata: (
                                ["src.main"],
                                [],
                                value,
                            ),
                        )[1]
                    )
                )

            wrong_hooks = (
                [] if expected_hooks else ["gui_host_runtime_hook"]
            )
            wrong_metadata = dict(
                profile_metadata,
                runtime_hook_scripts=wrong_hooks,
            )
            with self.subTest(profile=profile, contract="wrong"):
                self.assertTrue(
                    any(
                        finding["kind"] == "archive_runtime_hook_contract"
                        for finding in PROFILE_GATE["_combined_archive_inventory"](
                            [profile_contract],
                            profile,
                            lambda *_args, value=wrong_metadata: (
                                ["src.main"],
                                [],
                                value,
                            ),
                        )[1]
                    )
                )

    def test_final_resource_injection_rejects_forged_zero_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            (app / "Contents/Resources/internal-test-bypass.json").write_text('{"enabled":true}\n', encoding="utf-8")
            self._resign(app)
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "receipt.json",
                    self._synthetic_archive_reader,
                )
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["final_forbidden_zero"])
            self.assertGreater(receipt["inventory"]["difference_count"], 0)

    def test_final_pyz_injection_rejects_zero_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            malicious_reader = lambda executable, profile: (
                ["src.internal_main"],
                [{"kind": "pyz_module", "value": "src.internal_main"}],
                {"format": "pyinstaller_carchive", "carchive_count": 2, "pyz_archive_count": 1, "pyz_count": 1, "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()},
            )
            with self.assertRaises(SystemExit):
                PROFILE_GATE["_verify_app_with_reader"](
                    app,
                    "external_candidate",
                    analysis,
                    root / "receipt.json",
                    malicious_reader,
                )
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["checks"]["final_forbidden_zero"])
            self.assertIn("src.internal_main", receipt["inventory"]["differences"]["modules_extra"])

    def test_stale_forged_profile_and_inventory_receipts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, analysis = self._synthetic_external_app(root)
            valid = json.loads(analysis.read_text(encoding="utf-8"))
            cases = []
            old = {"profile": "external_candidate", "forbidden_count": 0}
            cases.append(old)
            forged = dict(valid)
            forged["inventory_digest"] = "0" * 64
            cases.append(forged)
            mismatch = dict(valid)
            mismatch["profile"] = "internal_test"
            cases.append(mismatch)
            inventory = dict(valid)
            inventory["resources"] = sorted(valid["resources"] + ["ghost.json"])
            inventory["inventory_digest"] = PROFILE_GATE["_canonical_digest"](
                {"modules": inventory["modules"], "resources": inventory["resources"], "profile_sha256": inventory["profile_sha256"]}
            )
            cases.append(inventory)
            for index, payload in enumerate(cases):
                with self.subTest(index=index):
                    analysis.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(SystemExit):
                        PROFILE_GATE["_verify_app_with_reader"](
                            app,
                            "external_candidate",
                            analysis,
                            root / f"receipt-{index}.json",
                            self._synthetic_archive_reader,
                        )

    def test_external_runtime_config_is_forbidden_even_if_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "license_runtime_config.json"
            validate = PROFILE_GATE["_valid_external_runtime_config"]
            self.assertTrue(all(validate(config).values()))
            for payload in ({}, {"schema_version": 1, "build_profile": "external_candidate",
                                  "environment": "production", "endpoint": "https://license.example.com"}):
                config.write_text(json.dumps(payload))
                self.assertFalse(all(validate(config).values()))
                self.assertIn(
                    {"kind": "resource", "value": config.name},
                    PROFILE_GATE["_resource_findings"]("external_candidate", root, [config.name]),
                )
                with self.assertRaises(SystemExit):
                    PROFILE_GATE["write_analysis_receipt"](
                        root / "analysis.json", "external_candidate", [],
                        ["nested/license_runtime_config.json"], "a" * 64,
                    )
            config.unlink()
            config.symlink_to(root / "missing.json")
            self.assertFalse(all(validate(config).values()))
            config.unlink()
            config.mkdir()
            self.assertFalse(all(validate(config).values()))

    def test_runtime_environment_or_arguments_cannot_switch_external_profile(self):
        sys.path.insert(0, str(APP))
        try:
            import run_external

            with tempfile.TemporaryDirectory() as temp:
                profile = Path(temp) / "build_profile.json"
                PROFILE_GATE["write_profile"](
                    profile,
                    "external_candidate",
                    version="1",
                    build="2",
                    source_commit=SOURCE_COMMIT,
                    source_tree=SOURCE_TREE,
                )
                with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", temp, create=True), patch.dict(
                    os.environ,
                    {"LULU_BUILD_PROFILE": "internal_test", "LULU_LICENSE_ENV": "mock"},
                    clear=False,
                ), patch.object(sys, "argv", ["Lulu", "--profile", "internal_test"]):
                    run_external._assert_packaged_profile()

                PROFILE_GATE["write_profile"](
                    profile,
                    "internal_test",
                    version="1",
                    build="2",
                    source_commit=SOURCE_COMMIT,
                    source_tree=SOURCE_TREE,
                )
                with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", temp, create=True), patch.dict(
                    os.environ,
                    {"LULU_BUILD_PROFILE": "external_candidate"},
                    clear=False,
                ):
                    with self.assertRaises(RuntimeError):
                        run_external._assert_packaged_profile()
        finally:
            sys.path.remove(str(APP))

    def test_external_member_scan_rejects_every_publishable_bypass_family(self):
        clean = PROFILE_GATE["scan_collected_members"](
            "external_candidate",
            [("src.main", str(APP / "src/main.py")), ("run_external", str(APP / "run_external.py"))],
            ["build_profile.json"],
        )
        self.assertEqual(clean["forbidden_count"], 0)

        for module in (
            "src.internal_main",
            "src.internal_bootstrap",
            "src.core.license.mock_transport",
            "src.core.license.external_service",
            "src.core.license._service_core",
            "src.core.license.core_service",
            "src.core.cli_runtime.license_projection",
            "src.ui.license_device_page",
            "src.ui.license_gate",
            "run_app",
            "run_internal",
        ):
            with self.subTest(module=module):
                result = PROFILE_GATE["scan_collected_members"](
                    "external_candidate", [(module, str(APP / "run_external.py"))], []
                )
                self.assertGreater(result["forbidden_count"], 0)

        for resource in ("src/core/license/transport.py", "src/ui/license_gate.py"):
            with self.subTest(resource=resource):
                result = PROFILE_GATE["scan_collected_members"]("external_candidate", [], [resource])
                self.assertGreater(result["forbidden_count"], 0)

    def test_external_member_scan_ignores_dependency_substrings_but_keeps_project_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dependency = Path(temp_dir) / "asyncio.py"
            dependency.write_text(
                "async def _run_internal_method(self):\n"
                "    return 'internal_test helper from third-party package'\n",
                encoding="utf-8",
            )
            dependency_result = PROFILE_GATE["scan_collected_members"](
                "external_candidate",
                [("filelock.asyncio", str(dependency))],
                [],
            )
            self.assertEqual(dependency_result["forbidden_count"], 0)

        adapters_result = PROFILE_GATE["scan_collected_members"](
            "external_candidate",
            [("src.core.collect.adapters", str(APP / "src/core/collect/adapters/__init__.py"))],
            [],
        )
        self.assertEqual(adapters_result["forbidden_count"], 0)

        with tempfile.TemporaryDirectory(dir=APP / "src") as temp_dir:
            project_source = Path(temp_dir) / "unexpected.py"
            project_source.write_text("value = 'internal_test'\n", encoding="utf-8")
            project_result = PROFILE_GATE["scan_collected_members"](
                "external_candidate",
                [("src.unexpected", str(project_source))],
                [],
            )
            self.assertGreater(project_result["forbidden_count"], 0)

    def test_external_member_scan_accepts_runtime_version_without_internal_profile_marker(self):
        version_source = APP / "src/version.py"
        self.assertNotIn(b"internal_test", version_source.read_bytes())
        result = PROFILE_GATE["scan_collected_members"](
            "external_candidate",
            [("src.version", str(version_source))],
            [],
        )
        self.assertEqual(result["forbidden_count"], 0)
        self.assertIn(
            b"internal_test",
            (APP / "packaging/profile_gate.py").read_bytes(),
        )

    def test_final_content_markers_only_scan_lulu_owned_modules(self):
        marker_fn = PROFILE_GATE["_external_module_markers"]
        resource_name = PROFILE_GATE["_resource_module_name"]

        self.assertEqual(marker_fn("filelock.asyncio"), ())
        self.assertEqual(marker_fn("torch._inductor.utils"), ())
        self.assertNotIn(b"internal_test", marker_fn("src.core.collect.adapters"))
        self.assertIn(b"run_internal", marker_fn("src.unexpected"))
        self.assertEqual(resource_name("torch/_inductor/utils.py"), None)
        self.assertEqual(resource_name("src/core/example.py"), "src.core.example")
        self.assertEqual(resource_name("src/core/example/__init__.py"), "src.core.example")

    def test_internal_member_scan_rejects_keyring_license_and_release_resources(self):
        clean = PROFILE_GATE["scan_collected_members"](
            "internal_test",
            [("src.internal_main", str(APP / "src/internal_main.py")), ("src.ui.main_window", str(APP / "src/ui/main_window.py"))],
            ["build_profile.json"],
        )
        self.assertEqual(clean["forbidden_count"], 0)
        dirty = PROFILE_GATE["scan_collected_members"](
            "internal_test",
            [("src.core.license.store", str(APP / "src/core/license/store.py")), ("keyring.backends.macOS", "")],
            ["license_runtime_config.json", "update_public_key.pem"],
        )
        self.assertEqual(dirty["forbidden_count"], 4)

    def test_trusted_member_scan_rejects_keyring_authorization_and_update_assets(self):
        clean = PROFILE_GATE["scan_collected_members"](
            "trusted_test",
            [
                ("src.trusted_main", str(APP / "src/trusted_main.py")),
                ("keyring.backends.macOS", ""),
            ],
            ["build_profile.json", "release_features.json"],
        )
        self.assertEqual(clean["forbidden_count"], 1)
        self.assertEqual(clean["findings"], [{"kind": "module", "value": "keyring.backends.macOS"}])
        dirty = PROFILE_GATE["scan_collected_members"](
            "trusted_test",
            [
                ("src.main", str(APP / "src/main.py")),
                ("src.core.license.external_service", ""),
                ("run_external", str(APP / "run_external.py")),
            ],
            [
                "update_public_key.pem",
                "update_public_keys.json",
            ],
        )
        self.assertEqual(dirty["forbidden_count"], 6)

    def test_trusted_member_scan_rejects_cli_ticket_and_keychain_runtime_members(self):
        rejected = PROFILE_GATE["scan_collected_members"](
            "trusted_test",
            [
                ("src.core.cli_runtime.factory", ""),
                ("src.core.license._service_core", ""),
                ("src.core.license.device", ""),
                ("src.core.license.models", str(APP / "src/core/license/models.py")),
                ("src.core.license.redaction", ""),
                ("src.core.license.store", str(APP / "src/core/license/store.py")),
                ("src.core.license.transport", ""),
                ("keyring.backends.macOS", ""),
            ],
            ["build_profile.json", "release_features.json"],
        )
        self.assertEqual(rejected["forbidden_count"], 3, rejected["findings"])
        self.assertEqual(
            {item["value"] for item in rejected["findings"]},
            {"src.core.cli_runtime.factory", "src.core.license.store", "keyring.backends.macOS"},
        )

        forbidden = PROFILE_GATE["scan_collected_members"](
            "trusted_test",
            [
                ("src.core.license.external_service", ""),
                ("src.core.license.service", ""),
                ("src.core.license.mock_transport", ""),
            ],
            [
                "update_public_key.pem",
                "update_public_keys.json",
            ],
        )
        self.assertEqual(forbidden["forbidden_count"], 6)

    def test_non_trusted_profiles_reject_trusted_release_feature_manifest(self):
        for profile in ("internal_test", "external_candidate"):
            with self.subTest(profile=profile):
                result = PROFILE_GATE["scan_collected_members"](
                    profile,
                    [],
                    ["build_profile.json", "release_features.json"],
                )
                self.assertTrue(
                    any(item["value"] == "release_features.json" for item in result["findings"])
                )

    def test_internal_root_is_private_and_symlink_base_fails_closed(self):
        sys.path.insert(0, str(APP))
        try:
            from src import internal_bootstrap

            unique = f"lulu-internal-test-unit-{os.getpid()}"
            base = Path("/private/tmp") / unique
            shutil.rmtree(base, ignore_errors=True)
            with patch.object(internal_bootstrap, "_BASE", base), patch.dict(os.environ, {}, clear=False):
                root = internal_bootstrap.lock_internal_root()
                self.assertTrue(root.is_dir())
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                self.assertTrue(str(root).startswith(str(base) + "/"))
                self.assertEqual(Path(os.environ["HOME"]), root / "home")
            shutil.rmtree(base)

            target = Path("/private/tmp") / f"{unique}-target"
            bad = Path("/private/tmp") / f"{unique}-link"
            target.mkdir(mode=0o700)
            bad.symlink_to(target, target_is_directory=True)
            try:
                with patch.object(internal_bootstrap, "_BASE", bad):
                    with self.assertRaises((OSError, RuntimeError)):
                        internal_bootstrap.lock_internal_root()
            finally:
                bad.unlink(missing_ok=True)
                target.rmdir()
        finally:
            sys.path.remove(str(APP))

    def test_internal_root_same_uid_namespace_replacement_fails_closed(self):
        sys.path.insert(0, str(APP))
        try:
            from src import internal_bootstrap

            base = Path("/private/tmp") / f"lulu-internal-replace-unit-{os.getpid()}"
            shutil.rmtree(base, ignore_errors=True)
            fixed_run = "a" * 32
            original = internal_bootstrap._mkdir_private_at
            replaced = False

            def swapping_mkdir(parent_fd, name):
                nonlocal replaced
                original(parent_fd, name)
                if name == "home" and not replaced:
                    replaced = True
                    base_fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.rename(fixed_run, fixed_run + "-moved", src_dir_fd=base_fd, dst_dir_fd=base_fd)
                        os.mkdir(fixed_run, 0o700, dir_fd=base_fd)
                    finally:
                        os.close(base_fd)

            try:
                with patch.object(internal_bootstrap, "_BASE", base), patch.object(
                    internal_bootstrap.secrets, "token_hex", return_value=fixed_run
                ), patch.object(internal_bootstrap, "_mkdir_private_at", side_effect=swapping_mkdir):
                    with self.assertRaisesRegex(RuntimeError, "初始化期间被替换"):
                        internal_bootstrap.lock_internal_root()
            finally:
                shutil.rmtree(base, ignore_errors=True)
        finally:
            sys.path.remove(str(APP))

    def test_internal_real_main_window_uses_isolated_roots_without_license_or_keyring_import(self):
        probe = r'''
import builtins, json, os, pathlib, shutil, sys
os.environ["QT_QPA_PLATFORM"]="offscreen"
real_import=builtins.__import__
seen=[]
def guarded(name,*args,**kwargs):
    if name == "keyring" or name.startswith("src.core.license"):
        seen.append(name)
    return real_import(name,*args,**kwargs)
builtins.__import__=guarded
from PySide6.QtWidgets import QApplication, QWidget
QApplication.exec=lambda self: 0
QWidget.show=lambda self: None
QWidget.raise_=lambda self: None
QWidget.activateWindow=lambda self: None
from src import internal_bootstrap
base=pathlib.Path(os.environ["LULU_TEST_PROBE_BASE"])
internal_bootstrap._BASE=base
from src.internal_main import main
import src.ui.macos as macos
macos.activate_regular_app=lambda: None
macos.unify_titlebar=lambda window: False
rc=main()
from src.config import _CONFIG_PATH
from src.core import library
from src.core.synthesis import voices
root=pathlib.Path(os.environ["HOME"]).parent
paths=[_CONFIG_PATH,library.LIBRARY_DIR,voices.VOICES_DIR]
print(json.dumps({"rc":rc,"imports":seen,"root":str(root),"paths":[str(p) for p in paths],"all_isolated":all(str(p).startswith(str(root)+"/") for p in paths)},sort_keys=True))
shutil.rmtree(base)
'''
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = "/private/tmp/lulu-profile-test-pyc"
        probe_base = Path("/private/tmp") / f"lulu-internal-main-probe-{os.getpid()}"
        env["LULU_TEST_PROBE_BASE"] = str(probe_base)
        try:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=APP,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["rc"], 0)
            self.assertEqual(payload["imports"], [])
            self.assertTrue(payload["all_isolated"])
        finally:
            shutil.rmtree(probe_base, ignore_errors=True)

    def test_frozen_external_license_transport_ignores_runtime_overrides(self):
        sys.path.insert(0, str(APP))
        try:
            from src.core.license.service import build_transport_from_environment
            from src.core.license.transport import HttpsLicenseTransport

            with tempfile.TemporaryDirectory() as temp:
                Path(temp, "license_runtime_config.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "environment": "production",
                            "endpoint": "https://license.example.com",
                            "build_profile": "external_candidate",
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", temp, create=True), patch.dict(
                    os.environ,
                    {"LULU_LICENSE_ENV": "mock", "LULU_LICENSE_ENDPOINT": "http://127.0.0.1"},
                    clear=False,
                ):
                    transport = build_transport_from_environment()
                self.assertIsInstance(transport, HttpsLicenseTransport)
                self.assertEqual(transport.endpoint, "https://license.example.com/")
        finally:
            sys.path.remove(str(APP))

    def test_build_entry_rejects_missing_unknown_and_profile_conflicts_before_packaging(self):
        script = APP / "packaging/build_app.sh"
        cases = [
            ([], {}, "必须显式提供 --identity"),
            (["mock"], {}, "未知参数"),
            (["--identity"], {}, "--identity 缺少值"),
            (["--venv"], {}, "--venv 缺少值"),
        ]
        for argv, additions, expected in cases:
            with self.subTest(argv=argv, additions=additions):
                env = os.environ.copy()
                for key in (
                    "LULU_LICENSE_ENV",
                    "LULU_LICENSE_TRANSPORT",
                    "LULU_LICENSE_ENDPOINT",
                    "LULU_UPDATE_RELEASE",
                    "LULU_UPDATE_PUBLIC_KEY_PATH",
                ):
                    env.pop(key, None)
                env.update(additions)
                result = subprocess.run(["bash", str(script), *argv], cwd=APP, env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("PyInstaller 打包", result.stdout)


if __name__ == "__main__":
    unittest.main()
