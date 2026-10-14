from __future__ import annotations

import os
from pathlib import Path
import runpy
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(APP / "packaging/privacy_gate.py"))


class ReleasePrivacyGateTests(unittest.TestCase):
    def test_toc_filter_removes_cache_but_keeps_contact_and_authorship(self):
        items = [
            ("av/licenses/__pycache__/AUTHORS.cpython-311.pyc", "/tmp/AUTHORS.pyc", "DATA"),
            ("src/ui/assets/author_contact_wechat.jpg", "/tmp/contact.jpg", "DATA"),
            ("src/ui/assets/license_contact_wechat.png", "/tmp/contact-qr.png", "DATA"),
            ("AUTHORSHIP.json", "/tmp/AUTHORSHIP.json", "DATA"),
        ]
        kept = GATE["drop_cache_toc_entries"](items)
        self.assertEqual(
            [item[0] for item in kept],
            [
                "src/ui/assets/author_contact_wechat.jpg",
                "src/ui/assets/license_contact_wechat.png",
                "AUTHORSHIP.json",
            ],
        )

    def test_local_wheel_provenance_is_removed_without_losing_package_metadata(self):
        entries = [
            ("deno-2.9.5.dist-info/direct_url.json", "/private-build/wheel-origin", "DATA"),
            ("deno-2.9.5.dist-info/METADATA", "/metadata", "DATA"),
            ("deno-2.9.5.dist-info/licenses/LICENSE", "/license", "DATA"),
            ("yt_dlp_ejs/yt/solver/core.min.js", "/script", "DATA"),
            ("some_runtime/direct_url.json", "/runtime", "DATA"),
        ]
        self.assertEqual(GATE["drop_non_runtime_toc_entries"](entries), entries[1:])

    def test_contact_assets_public_key_and_pem_labels_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "author_contact_wechat.jpg").write_bytes(b"public author contact")
            (root / "license_contact_wechat.png").write_bytes(b"public support QR")
            (root / "AUTHORSHIP.json").write_text('{"author":"兆基"}\n', encoding="utf-8")
            (root / "update_public_key.pem").write_text(
                "-----BEGIN PUBLIC KEY-----\nQUJDREVGR0g=\n-----END PUBLIC KEY-----\n",
                encoding="ascii",
            )
            (root / "qt-labels.bin").write_bytes(
                b"-----BEGIN PRIVATE KEY-----\x00-----END PRIVATE KEY-----"
            )
            self.assertEqual(GATE["scan_bundle_privacy"](root), [])

    def test_private_build_state_is_rejected_without_echoing_private_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            builder_home = "/Users/private-builder"
            (root / "module.pyc").write_bytes(b"cache")
            (root / "runtime.db").write_bytes(b"database")
            (root / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
            (root / "trace.log").write_text("local log\n", encoding="utf-8")
            (root / "compiled.bin").write_bytes(
                f"source={builder_home}/workspace/app.py".encode("utf-8")
            )
            (root / "private.pem").write_text(
                "-----BEGIN PRIVATE KEY-----\n"
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
                "-----END PRIVATE KEY-----\n",
                encoding="ascii",
            )
            findings = GATE["scan_bundle_privacy"](
                root,
                forbidden_markers=(builder_home,),
            )
            kinds = {item["kind"] for item in findings}
            self.assertEqual(
                kinds,
                {"builder_path", "cache_artifact", "private_key_payload", "sensitive_file"},
            )
            serialized = repr(findings)
            self.assertNotIn(builder_home, serialized)
            self.assertNotIn("do-not-copy", serialized)

    def test_cache_path_detection_is_cross_platform(self):
        for value in (
            "pkg/__pycache__/x.pyc",
            r"pkg\__pycache__\x.pyc",
            "pkg/x.pyo",
        ):
            with self.subTest(value=value):
                self.assertTrue(GATE["is_cache_artifact"](value))
        self.assertFalse(GATE["is_cache_artifact"]("pkg/AUTHORS.py"))

    def test_non_runtime_filter_removes_only_registered_test_data(self):
        items = [
            ("sklearn/datasets/tests/data/openml/id_2/data-v1.csv", "/tmp/a", "DATA"),
            ("torch/fx/passes/tests/test_pass_manager.py", "/tmp/b", "DATA"),
            ("src/core/synthesis/indextts2_worker.py", "/tmp/c", "DATA"),
            ("transformers/models/test_model/modeling.py", "/tmp/d", "DATA"),
        ]
        kept = GATE["drop_non_runtime_toc_entries"](items)
        self.assertEqual(
            [item[0] for item in kept],
            [
                "src/core/synthesis/indextts2_worker.py",
                "transformers/models/test_model/modeling.py",
            ],
        )

    def test_final_bundle_rejects_development_material_and_extra_product_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "src/core/synthesis/indextts2_worker.py"
            allowed.parent.mkdir(parents=True)
            allowed.write_text("print('runtime')\n", encoding="utf-8")
            extra = root / "src/ui/main_window.py"
            extra.parent.mkdir(parents=True)
            extra.write_text("print('source')\n", encoding="utf-8")
            tests = root / "sklearn/datasets/tests/data.csv"
            tests.parent.mkdir(parents=True)
            tests.write_text("fixture\n", encoding="utf-8")

            findings = GATE["scan_bundle_privacy"](root)
            self.assertEqual(
                findings,
                [
                    {"kind": "non_runtime_artifact", "value": "sklearn/datasets/tests/data.csv"},
                    {"kind": "unexpected_product_source", "value": "src/ui/main_window.py"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
