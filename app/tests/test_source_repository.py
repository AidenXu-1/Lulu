"""A source archive builds locally without trusting a host checkout or App."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
SOURCE = runpy.run_path(str(PACKAGING / "source_repository.py"))


class SourceRepositoryTests(unittest.TestCase):
    def fixture(self, root):
        root.mkdir(parents=True, exist_ok=True)
        files = {".gitignore": b".venv/\napp/build/\n", "README.md": b"Lulu source\n",
                 "build.sh": b"#!/bin/sh\nexit 0\n"}
        entries = []
        for name, raw in files.items():
            (root / name).write_bytes(raw)
            entries.append({"path": name, "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": "100755" if name.endswith(".sh") else "100644"})
        manifest = {"schema": 2, "files": entries}
        self.manifest(root, manifest)
        return manifest

    def manifest(self, root, payload):
        (root / "SOURCE_MANIFEST.json").write_text(json.dumps(payload, sort_keys=True))

    def git(self, root, *args):
        return SOURCE["_git"](root, *args)

    def test_archive_build_snapshot_is_complete_deterministic_and_local(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "first", Path(d) / "second"
            for root in (a, b):
                self.fixture(root)
                (root / ".venv").mkdir()
                (root / ".venv/private-environment").write_text("not source")
                SOURCE["prepare_repository"](root)
                self.assertEqual(self.git(root, "status", "--porcelain"), "")
                self.assertEqual(self.git(root, "ls-files").splitlines(),
                                 [".gitignore", "README.md", "SOURCE_MANIFEST.json", "build.sh"])
                self.assertEqual((root / "build.sh").stat().st_mode & 0o777, 0o755)
                self.assertEqual(self.git(root, "remote"), "")
                SOURCE["prepare_repository"](root)
            self.assertEqual(self.git(a, "rev-parse", "HEAD"), self.git(b, "rev-parse", "HEAD"))
            gate = runpy.run_path(str(PACKAGING / "product_baseline_gate.py"))
            receipt = gate["verify_product_baseline"](repo=a)
            self.assertEqual(receipt["candidate_commit"], receipt["baseline_commit"])

    def test_changed_or_missing_file_is_rejected_before_git_creation(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                root = Path(d); self.fixture(root)
                if missing:
                    (root / "README.md").unlink()
                else:
                    (root / "README.md").write_text("changed source")
                with self.assertRaises(SystemExit):
                    SOURCE["prepare_repository"](root)
                self.assertFalse((root / ".git").exists())

    def test_traversal_duplicate_and_invalid_modes_are_rejected(self):
        for kind in ("traversal", "duplicate", "mode", "self", "schema"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                root = Path(d); m = self.fixture(root)
                if kind == "traversal": m["files"][0]["path"] = "../outside"
                if kind == "duplicate": m["files"].append(dict(m["files"][0]))
                if kind == "mode": m["files"][0]["mode"] = "120000"
                if kind == "self": m["files"][0]["path"] = "SOURCE_MANIFEST.json"
                if kind == "schema": m["schema"] = 1
                self.manifest(root, m)
                with self.assertRaises(SystemExit): SOURCE["prepare_repository"](root)
                self.assertFalse((root / ".git").exists())

    def test_symlink_and_hardlink_inputs_are_rejected(self):
        import os
        for link in ("symbolic", "hard"):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as d:
                root = Path(d) / "archive"; self.fixture(root)
                external = Path(d) / "outside"; external.write_bytes((root / "README.md").read_bytes())
                (root / "README.md").unlink()
                if link == "symbolic": (root / "README.md").symlink_to(external)
                else: os.link(external, root / "README.md")
                with self.assertRaises(SystemExit): SOURCE["prepare_repository"](root)
                self.assertFalse((root / ".git").exists())

    def test_archive_inside_another_checkout_gets_its_own_identity(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d); self.git(parent, "init", "--quiet", "--template=")
            root = parent / "download"; self.fixture(root)
            SOURCE["prepare_repository"](root)
            self.assertEqual(self.git(root, "rev-parse", "--show-toplevel"), str(root.resolve()))
            self.assertNotEqual(self.git(root, "rev-parse", "--git-dir"), str(parent / ".git"))

    def test_existing_checkout_is_not_replaced_or_reinterpreted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); self.git(root, "init", "--quiet", "--template=")
            (root / "user-file").write_text("keep local work")
            SOURCE["prepare_repository"](root)
            self.assertFalse((root / "SOURCE_MANIFEST.json").exists())
            self.assertEqual((root / "user-file").read_text(), "keep local work")
            self.assertEqual(SOURCE["default_baseline"](root, "published"), "published")

    def test_later_source_changes_are_not_silently_recommitted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); self.fixture(root); SOURCE["prepare_repository"](root)
            commit = self.git(root, "rev-parse", "HEAD")
            (root / "README.md").write_text("user change")
            SOURCE["prepare_repository"](root)
            self.assertEqual(self.git(root, "rev-parse", "HEAD"), commit)
            profile = runpy.run_path(str(PACKAGING / "profile_gate.py"))
            with self.assertRaises(SystemExit): profile["source_identity"](root)


if __name__ == "__main__":
    unittest.main()
