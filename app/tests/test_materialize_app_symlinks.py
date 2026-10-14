from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

_SCRIPT = Path(__file__).parents[1] / "packaging/materialize_app_symlinks.py"
_SPEC = importlib.util.spec_from_file_location("lulu_materialize_app_symlinks", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
MaterializationError = _MODULE.MaterializationError
materialize = _MODULE.materialize


class MaterializeAppSymlinksTests(unittest.TestCase):
    def test_materializes_internal_file_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Lulu.app"
            resources = app / "Contents/Resources"
            frameworks = app / "Contents/Frameworks"
            resources.mkdir(parents=True)
            (frameworks / "package").mkdir(parents=True)
            library = frameworks / "library.dylib"
            library.write_bytes(b"native")
            module = frameworks / "package/module.py"
            module.write_text("value = 1\n", encoding="utf-8")
            (resources / "library.dylib").symlink_to("../Frameworks/library.dylib")
            (resources / "package").symlink_to("../Frameworks/package")

            report = materialize(app)

            self.assertEqual(report["materialized_count"], 2)
            self.assertEqual(report["remaining_symlink_count"], 0)
            self.assertFalse((resources / "library.dylib").is_symlink())
            self.assertFalse((resources / "package").is_symlink())
            self.assertEqual((resources / "package/module.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(
                hashlib.sha256((resources / "library.dylib").read_bytes()).hexdigest(),
                hashlib.sha256(b"native").hexdigest(),
            )


    def test_rejects_symlink_that_escapes_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.write_text("secret", encoding="utf-8")
            app = root / "Lulu.app"
            resources = app / "Contents/Resources"
            resources.mkdir(parents=True)
            (resources / "escape").symlink_to(outside)

            with self.assertRaisesRegex(MaterializationError, "escapes app bundle"):
                materialize(app)

            self.assertTrue((resources / "escape").is_symlink())


if __name__ == "__main__":
    unittest.main()
