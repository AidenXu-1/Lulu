"""Generation-9 canonical App tree identity contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest

from src.core.agent_entry.canonical_app_tree import (
    CanonicalAppTreeError,
    canonical_app_tree_identity,
)


class Generation9CanonicalAppTreeTests(unittest.TestCase):
    def test_fixed_tree_has_one_stable_known_identity_and_excludes_only_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lulu canonical app tree ") as directory:
            app = Path(directory) / "Lulu.app"
            macos = app / "Contents/MacOS"
            resources = app / "Contents/Resources"
            signature = app / "Contents/_CodeSignature"
            for path in (app, app / "Contents", macos, resources, signature):
                path.mkdir(exist_ok=True)
                path.chmod(0o755)
            files = {
                macos / "Lulu": (b"gui", 0o755),
                resources / "data.txt": (b"data", 0o644),
                resources / "agent_product_manifest.json": (b"first manifest", 0o644),
                signature / "CodeResources": (b"seal", 0o644),
            }
            for path, (content, mode) in files.items():
                path.write_bytes(content)
                path.chmod(mode)

            first = canonical_app_tree_identity(app)
            second = canonical_app_tree_identity(app)

            self.assertEqual(first, second)
            self.assertEqual(first["schema_id"], "lulu.canonical-app-tree/v1")
            self.assertEqual(
                first["sha256"],
                "78878c91f787b583a757c7b012b76ef156ac3e927799cd145e0880338ff37bf6",
            )
            self.assertEqual(first["record_count"], 7)
            self.assertEqual(first["directory_count"], 4)
            self.assertEqual(first["regular_file_count"], 3)
            self.assertEqual(first["regular_bytes"], 11)
            self.assertEqual(
                first["excluded_paths"],
                ["Contents/Resources/agent_product_manifest.json"],
            )
            app_root = Path(__file__).resolve().parents[1]
            finalizer = runpy.run_path(
                str(app_root / "packaging/phase10_finalize_candidate.py")
            )
            finalizer_digest, finalizer_files, finalizer_bytes = finalizer[
                "_tree_digest"
            ](app)
            self.assertEqual(finalizer_digest, first["sha256"])
            self.assertEqual(finalizer_files, 3)
            self.assertEqual(finalizer_bytes, 11)

            os.utime(macos / "Lulu", (1, 1))
            (resources / "agent_product_manifest.json").write_bytes(b"second manifest")
            self.assertEqual(canonical_app_tree_identity(app), first)

    def test_signing_bytes_and_modes_are_bound_while_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lulu canonical mutations ") as directory:
            app = Path(directory) / "Lulu.app"
            resources = app / "Contents/Resources"
            signature = app / "Contents/_CodeSignature"
            signature.mkdir(parents=True)
            resources.mkdir(parents=True)
            manifest = resources / "agent_product_manifest.json"
            seal = signature / "CodeResources"
            manifest.write_bytes(b"manifest")
            seal.write_bytes(b"seal")
            manifest.chmod(0o644)
            seal.chmod(0o644)
            baseline = canonical_app_tree_identity(app)

            seal.write_bytes(b"changed seal")
            self.assertNotEqual(canonical_app_tree_identity(app)["sha256"], baseline["sha256"])
            seal.write_bytes(b"seal")
            seal.chmod(0o600)
            self.assertNotEqual(canonical_app_tree_identity(app)["sha256"], baseline["sha256"])
            seal.chmod(0o644)
            (resources / "forbidden-link").symlink_to("agent_product_manifest.json")
            with self.assertRaisesRegex(CanonicalAppTreeError, "APP_TREE_SYMLINK_FORBIDDEN"):
                canonical_app_tree_identity(app)

    def test_all_candidate_identity_consumers_expose_the_same_public_algorithm(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        profile = runpy.run_path(str(app_root / "packaging/profile_gate.py"))
        finalizer = runpy.run_path(
            str(app_root / "packaging/phase10_finalize_candidate.py")
        )
        self.assertIs(
            profile["canonical_app_tree_identity"],
            canonical_app_tree_identity,
        )
        self.assertIs(
            finalizer["canonical_app_tree_identity"],
            canonical_app_tree_identity,
        )

    def test_profile_receipt_rebinds_only_after_zero_link_materialization(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        profile = runpy.run_path(str(app_root / "packaging/profile_gate.py"))
        with tempfile.TemporaryDirectory(prefix="lulu profile final tree ") as directory:
            root = Path(directory)
            app = root / "Lulu.app"
            resources = app / "Contents/Resources"
            resources.mkdir(parents=True)
            (resources / "agent_product_manifest.json").write_text("{}\n", encoding="utf-8")
            (resources / "payload").write_text("final\n", encoding="utf-8")
            identity = canonical_app_tree_identity(app)
            receipt_path = root / "build-profile-receipt.json"
            receipt_path.write_text(
                json.dumps({
                    "schema": 2,
                    "profile": "external_candidate",
                    "ok": True,
                    "digests": {"app_tree_sha256": "pre-materialization"},
                    "app_tree": {"sha256": "pre-materialization"},
                }),
                encoding="utf-8",
            )
            materialization_path = root / "agent-symlink-materialization-receipt.json"
            materialization = {
                "schema_id": "lulu.agent-symlink-materialization/v1",
                "app_bundle": "Lulu.app",
                "materialized_count": 1,
                "remaining_symlink_count": 0,
                "records": [{"path": "Contents/Resources/payload", "target_type": "regular"}],
            }
            materialization_path.write_text(json.dumps(materialization), encoding="utf-8")

            rebound = profile["rebind_final_app_tree"](
                app=app,
                profile_receipt_path=receipt_path,
                materialization_receipt_path=materialization_path,
            )

            self.assertEqual(rebound["app_tree"], identity)
            self.assertEqual(rebound["digests"]["app_tree_sha256"], identity["sha256"])
            self.assertEqual(rebound["app_tree_binding"]["stage"], "post_materialization")
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8")), rebound
            )

            materialization["remaining_symlink_count"] = 1
            materialization_path.write_text(json.dumps(materialization), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "MATERIALIZATION_RECEIPT_INVALID"):
                profile["rebind_final_app_tree"](
                    app=app,
                    profile_receipt_path=receipt_path,
                    materialization_receipt_path=materialization_path,
                )

    def test_only_external_signed_stage_can_defer_identity_until_links_are_materialized(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        profile = runpy.run_path(str(app_root / "packaging/profile_gate.py"))
        with tempfile.TemporaryDirectory(prefix="lulu signed tree pending ") as directory:
            app = Path(directory) / "Lulu.app"
            resources = app / "Contents/Resources"
            resources.mkdir(parents=True)
            (resources / "agent_product_manifest.json").write_text("{}\n", encoding="utf-8")
            (resources / "target").write_text("data", encoding="utf-8")
            (resources / "link").symlink_to("target")

            pending = profile["candidate_app_tree_identity"](
                app, "external_candidate"
            )
            self.assertEqual(pending["state"], "pending_materialization")
            self.assertEqual(pending["sha256"], None)
            with self.assertRaisesRegex(
                CanonicalAppTreeError, "APP_TREE_SYMLINK_FORBIDDEN"
            ):
                profile["candidate_app_tree_identity"](app, "trusted_test")


if __name__ == "__main__":
    unittest.main()
