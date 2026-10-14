"""Independent GUI-only post-build authority tests."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


APP_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = APP_ROOT / "packaging/phase10_post_build_authority.py"


def _write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    path.chmod(mode)


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _semantic_json_sha256(value: dict[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fixture(module: dict[str, object], root: Path) -> tuple[Path, Path]:
    app = root / "dist/phase10-trusted-test-20260823.4/Lulu-Trusted-Test.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    gui = contents / "MacOS/Lulu-Trusted-Test"
    _write(gui, b"gui-only-executable\n", 0o700)
    _write(
        contents / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleExecutable": "Lulu-Trusted-Test",
                "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                "CFBundleShortVersionString": "0.3.2",
                "CFBundleVersion": "20260823.4",
                "LuluBuildIdentity": "trusted-test",
                "LuluBuildProfile": "trusted_test",
            }
        ),
    )
    product_source = _json_bytes(
        {"source_commit": "1" * 40, "source_tree": "2" * 40}
    )
    _write(resources / "product_source.json", product_source)
    profile = {
        "schema": 1,
        "profile": "trusted_test",
        "version": "0.3.2",
        "build": "20260823.4",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "app_name": "Lulu Trusted Test",
        "package_surface": "gui_only/v1",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "product_source_sha256": _semantic_json_sha256(
            json.loads(product_source)
        ),
    }
    _write(resources / "build_profile.json", _json_bytes(profile))
    tree_sha = module["_tree_digest"](app)
    evidence = {
        "schema_id": "lulu.phase10-gui-only-candidate-evidence/v1",
        "schema_revision": 1,
        "evidence_scope": "trusted_test_gui_only_non_release",
        "package_surface": "gui_only/v1",
        "bundle_role": "dist",
        "bundle_path": str(app),
        "candidate_identity": {
            "version": "0.3.2",
            "build": "20260823.4",
            "channel": "trusted-test",
            "release_state": "trusted_test_non_release",
            "bundle_id": "com.lulu.transcribe.trusted-test",
            "product": "Lulu Trusted Test",
        },
        "gui_entry": "Contents/MacOS/Lulu-Trusted-Test",
        "signature": {
            "kind": "ad_hoc", "codesign_verified": True,
            "team_id": None, "protected_capabilities": "unavailable",
        },
        "forbidden_surface_absent": dict(module["_ABSENT_ORACLE"]),
        "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
        "diagnostic_only": True,
        "promotion_allowed": False,
        "retained_install_allowed": False,
        "bundle_tree_sha256": tree_sha,
        "bundle_file_count": 4,
        "bundle_regular_bytes": 1,
        "gui_executable_sha256": hashlib.sha256(gui.read_bytes()).hexdigest(),
        "build_profile_sha256": hashlib.sha256((resources / "build_profile.json").read_bytes()).hexdigest(),
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
    }
    evidence_path = app.parent / "candidate-evidence.json"
    _write(evidence_path, _json_bytes(evidence))
    return app, evidence_path


def _refresh_evidence(module: dict[str, object], app: Path) -> None:
    resources = app / "Contents/Resources"
    evidence_path = app.parent / "candidate-evidence.json"
    evidence = json.loads(evidence_path.read_bytes())
    evidence["bundle_tree_sha256"] = module["_tree_digest"](app)
    evidence["build_profile_sha256"] = hashlib.sha256(
        (resources / "build_profile.json").read_bytes()
    ).hexdigest()
    _write(evidence_path, _json_bytes(evidence))


class Phase10PostBuildAuthorityTests(unittest.TestCase):
    def _module(self) -> dict[str, object]:
        return runpy.run_path(str(AUTHORITY_PATH))

    def _prepared(self, root: Path):
        module = self._module()
        app, evidence = _fixture(module, root)
        runtime = module["_measure_app"].__globals__
        runtime["_FIXED_APP"] = app
        runtime["_FIXED_DIST_ROOT"] = app.parent
        runtime["_FIXED_CANDIDATE_EVIDENCE"] = evidence
        runtime["_FIXED_ROOT"] = root / "authority"
        module.update(
            _FIXED_APP=app,
            _FIXED_DIST_ROOT=app.parent,
            _FIXED_CANDIDATE_EVIDENCE=evidence,
            _FIXED_ROOT=root / "authority",
        )
        signature = {
            "codesign_verified_deep_strict": True,
            "kind": "ad_hoc",
            "team_id": None,
            "gui_cdhash": "4" * 40,
        }
        runtime["_signature_facts"] = lambda _app: dict(signature)
        module["_signature_facts"] = runtime["_signature_facts"]
        return module, app

    def _issue_chain(self, module: dict[str, object]):
        build = module["issue_build_completed_receipt"]()
        carrier = module["produce_carrier"](build)
        security = module["review_carrier_security"](carrier)
        test = module["review_carrier_test"](carrier)
        artifact = module["finalize_authority_artifact"](carrier, security, test)
        security_artifact = module["review_authority_artifact_security"](artifact)
        test_artifact = module["review_authority_artifact_test"](artifact)
        authority = module["issue_post_build_authority"](
            artifact, security_artifact, test_artifact
        )
        return authority

    def test_product_source_semantic_digest_excludes_trailing_lf(self) -> None:
        with TemporaryDirectory() as directory:
            module, app = self._prepared(Path(directory).resolve())
            resources = app / "Contents/Resources"
            product_source_path = resources / "product_source.json"
            product_source = json.loads(product_source_path.read_bytes())
            profile_path = resources / "build_profile.json"
            profile = json.loads(profile_path.read_bytes())
            profile["product_source_sha256"] = _semantic_json_sha256(product_source)
            _write(profile_path, _json_bytes(profile))
            _refresh_evidence(module, app)

            receipt = module["issue_build_completed_receipt"]()
            self.assertEqual(type(receipt).__name__, "_BuildCompletedReceipt")

    def test_equivalent_json_format_keeps_semantic_digest_but_changes_raw_tree(self) -> None:
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            first_module, _first_app = self._prepared(Path(first_directory).resolve())
            first = first_module["_measure_app"]()

            second_module, second_app = self._prepared(Path(second_directory).resolve())
            formatted = (
                '{\n  "source_tree": "' + "2" * 40 + '",\n'
                '  "label": "Lulú",\n'
                '  "source_commit": "' + "1" * 40 + '"\n}\n'
            ).encode("utf-8")
            product_source_path = second_app / "Contents/Resources/product_source.json"
            _write(product_source_path, formatted)
            product_source = json.loads(formatted)
            profile_path = second_app / "Contents/Resources/build_profile.json"
            profile = json.loads(profile_path.read_bytes())
            profile["product_source_sha256"] = _semantic_json_sha256(product_source)
            _write(profile_path, _json_bytes(profile))
            _refresh_evidence(second_module, second_app)
            second = second_module["_measure_app"]()

            self.assertNotEqual(first.product_source_sha256, second.product_source_sha256)
            reformatted = (
                '{"source_commit":"' + "1" * 40 + '",'
                '"label":"Lulú","source_tree":"' + "2" * 40 + '"}\n'
            ).encode("utf-8")
            _write(product_source_path, reformatted)
            _refresh_evidence(second_module, second_app)
            third = second_module["_measure_app"]()
            self.assertEqual(second.product_source_sha256, third.product_source_sha256)
            self.assertNotEqual(second.app_content_tree_sha256, third.app_content_tree_sha256)

    def test_product_source_semantic_drift_fails_before_first_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            module, app = self._prepared(Path(directory).resolve())
            path = app / "Contents/Resources/product_source.json"
            payload = json.loads(path.read_bytes())
            payload["source_commit"] = "9" * 40
            _write(path, _json_bytes(payload))
            _refresh_evidence(module, app)

            with self.assertRaises(module["PostBuildAuthorityError"]) as raised:
                module["issue_build_completed_receipt"]()
            self.assertEqual(str(raised.exception), "PRODUCT_SOURCE_MISMATCH")
            self.assertEqual(module["_REGISTRIES"][0], {})
            self.assertFalse(Path(module["_FIXED_ROOT"]).exists())

    def test_product_source_raw_drift_still_requires_candidate_tree_update(self) -> None:
        with TemporaryDirectory() as directory:
            module, app = self._prepared(Path(directory).resolve())
            path = app / "Contents/Resources/product_source.json"
            payload = json.loads(path.read_bytes())
            reformatted = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
            self.assertEqual(
                _semantic_json_sha256(payload),
                json.loads(
                    (app / "Contents/Resources/build_profile.json").read_bytes()
                )["product_source_sha256"],
            )
            _write(path, reformatted)

            with self.assertRaises(module["PostBuildAuthorityError"]) as raised:
                module["issue_build_completed_receipt"]()
            self.assertEqual(str(raised.exception), "CANDIDATE_CONTENT_TREE_MISMATCH")
            self.assertEqual(module["_REGISTRIES"][0], {})
            self.assertFalse(Path(module["_FIXED_ROOT"]).exists())

    def test_product_source_parser_rejects_invalid_boundaries_before_receipt(self) -> None:
        malformed = {
            "non_utf8": b"\xff",
            "bom": b'\xef\xbb\xbf{"source_commit":"x"}',
            "duplicate_nested_key": b'{"nested":{"x":1,"x":2}}',
            "non_object": b"[]",
            "non_finite_number": b'{"value":NaN}',
            "positive_exponent_overflow": b'{"value":1e9999}',
            "negative_exponent_overflow": b'{"value":-1e9999}',
        }
        for label, raw in malformed.items():
            with self.subTest(label=label), TemporaryDirectory() as directory:
                module, app = self._prepared(Path(directory).resolve())
                _write(app / "Contents/Resources/product_source.json", raw)
                with self.assertRaises(module["PostBuildAuthorityError"]) as raised:
                    module["issue_build_completed_receipt"]()
                self.assertEqual(str(raised.exception), "PRODUCT_SOURCE_INVALID")
                self.assertEqual(module["_REGISTRIES"][0], {})
                self.assertFalse(Path(module["_FIXED_ROOT"]).exists())

    def test_semantic_digest_defense_rejects_non_finite_values(self) -> None:
        module = self._module()
        digest = module["_product_source_semantic_sha256"]
        for label, value in {
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "nan": float("nan"),
        }.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                digest({"value": value})

    def test_public_authority_api_has_no_caller_product_source_override(self) -> None:
        module = self._module()
        issue = module["issue_build_completed_receipt"]
        self.assertEqual(tuple(inspect.signature(issue).parameters), ())
        for keyword, value in {
            "raw_digest": "0" * 64,
            "product_source_sha256": "0" * 64,
            "product_source_path": "/tmp/caller.json",
            "product_source_payload": {},
            "product_source_reader": lambda: {},
            "environment": {"LULU_PRODUCT_SOURCE_JSON": "{}"},
        }.items():
            with self.subTest(keyword=keyword), self.assertRaises(TypeError):
                issue(**{keyword: value})

    def test_gui_only_authority_is_independent_of_cli_ticket_runtime(self) -> None:
        source = AUTHORITY_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(AUTHORITY_PATH))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        module = self._module()
        self.assertEqual(module["_FIXED_IDENTITY"]["build"], "20260823.4")
        self.assertEqual(module["_TASK_ID"], "TASK-20260823-696D55")
        self.assertEqual(module["_FIXED_IDENTITY"]["package_surface"], "gui_only/v1")
        self.assertEqual(module["_DIAGNOSTIC_IDENTITY"], {
            "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
            "diagnostic_only": True,
            "promotion_allowed": False,
            "retained_install_allowed": False,
        })
        self.assertFalse(any(name.startswith("src.core.cli_runtime") for name in imports))

    def test_fixed_nine_step_chain_emits_gui_only_schemas_without_cli_fields(self) -> None:
        with TemporaryDirectory() as directory:
            module, _app = self._prepared(Path(directory).resolve())
            authority = self._issue_chain(module)
            public = module["post_build_public_fingerprint"](authority)
            self.assertEqual(public["schema"], "lulu.phase10-gui-only-post-build-public/v1")
            self.assertEqual(public["package_surface"], "gui_only/v1")
            expected_diagnostic = {
                "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
                "diagnostic_only": True,
                "promotion_allowed": False,
                "retained_install_allowed": False,
            }
            self.assertEqual(
                {key: public[key] for key in expected_diagnostic},
                expected_diagnostic,
            )
            self.assertRegex(public["gui_executable_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(public["gui_cdhash"], "4" * 40)
            for path in sorted((Path(directory) / "authority").iterdir()):
                raw = path.read_text(encoding="utf-8")
                payload = json.loads(raw)
                self.assertEqual(
                    {key: payload[key] for key in expected_diagnostic},
                    expected_diagnostic,
                )
                self.assertNotIn("canonical_cli", raw)
                self.assertNotIn('"agent_cli"', raw)
                self.assertNotIn("installation_ticket", raw)
                self.assertNotIn("keychain", payload)

    def test_candidate_diagnostic_identity_drift_stops_before_first_receipt(self) -> None:
        mutations = {
            "diagnostic_contract_id": "TASK-ATTACK/gui-not-visible-stage/v1",
            "diagnostic_only": False,
            "promotion_allowed": True,
            "retained_install_allowed": True,
        }
        for key, value in mutations.items():
            with self.subTest(key=key), TemporaryDirectory() as directory:
                module, app = self._prepared(Path(directory).resolve())
                evidence_path = app.parent / "candidate-evidence.json"
                evidence = json.loads(evidence_path.read_bytes())
                evidence[key] = value
                _write(evidence_path, _json_bytes(evidence))
                with self.assertRaises(module["PostBuildAuthorityError"]):
                    module["issue_build_completed_receipt"]()
                self.assertEqual(module["_REGISTRIES"][0], {})
                self.assertFalse(Path(module["_FIXED_ROOT"]).exists())

    def test_old_cli_authority_and_caller_mappings_cannot_mint_receipts(self) -> None:
        with TemporaryDirectory() as directory:
            module, _app = self._prepared(Path(directory).resolve())
            public_functions = (
                module["produce_carrier"], module["review_carrier_security"],
                module["review_carrier_test"], module["post_build_public_fingerprint"],
            )
            forged = {
                "schema": "lulu.r16-post-build-authority/v1",
                "canonical_cli_sha256": "0" * 64,
                "path": "/tmp/caller.app",
            }
            for function in public_functions:
                with self.subTest(function=function.__name__), self.assertRaises(TypeError):
                    function(forged)
            with self.assertRaises(TypeError):
                module["issue_build_completed_receipt"](forged)

    def test_each_reviewer_remeasures_and_drift_stops_before_next_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            module, app = self._prepared(Path(directory).resolve())
            build = module["issue_build_completed_receipt"]()
            carrier = module["produce_carrier"](build)
            gui = app / "Contents/MacOS/Lulu-Trusted-Test"
            gui.write_bytes(b"drift\n")
            gui.chmod(0o700)
            with self.assertRaises(module["PostBuildAuthorityError"]):
                module["review_carrier_security"](carrier)

    def test_create_only_conflict_preserves_winner(self) -> None:
        with TemporaryDirectory() as directory:
            module, _app = self._prepared(Path(directory).resolve())
            root = Path(module["_FIXED_ROOT"])
            root.mkdir(mode=0o700)
            winner = root / "gui-only-carrier.json"
            _write(winner, b"winner\n")
            before = winner.read_bytes()
            build = module["issue_build_completed_receipt"]()
            with self.assertRaises(module["PostBuildAuthorityError"]):
                module["produce_carrier"](build)
            self.assertEqual(winner.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [winner.name])

    def test_load_fixed_authority_remeasures_and_issues_opaque_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            module, _app = self._prepared(Path(directory).resolve())
            authority = self._issue_chain(module)
            first = module["post_build_public_fingerprint"](authority)
            loaded = module["load_fixed_post_build_authority"]()
            second = module["post_build_public_fingerprint"](loaded)
            self.assertEqual(first, second)

    def test_build_wrapper_keeps_exact_nine_step_public_sequence(self) -> None:
        script = (APP_ROOT / "packaging/build_trusted_test.sh").read_text(encoding="utf-8")
        ordered = [
            "issue_build_completed_receipt", "produce_carrier",
            "review_carrier_security", "review_carrier_test",
            "finalize_authority_artifact", "review_authority_artifact_security",
            "review_authority_artifact_test", "issue_post_build_authority",
            "post_build_public_fingerprint",
        ]
        positions = [script.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
