import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(APP / "packaging/product_baseline_gate.py"))
CAPABILITY_GATE_PATH = APP / "packaging/product_capability_gate.py"


class ProductBaselineGateTests(unittest.TestCase):
    def _commit(self, repo: Path, message: str, name: str) -> str:
        (repo / name).write_text(message + "\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(repo), "add", name], check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repo),
                "-c",
                "user.name=Lulu Product Gate",
                "-c",
                "user.email=lulu-product-gate@example.invalid",
                "commit",
                "-qm",
                message,
            ],
            check=True,
        )
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


    def test_capability_gate_test_names_resolve_without_duplicates(self):
        names = runpy.run_path(str(CAPABILITY_GATE_PATH))["CAPABILITY_TESTS"]
        self.assertEqual(len(names), len(set(names)))
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(names)
        self.assertEqual(loader.errors, [], "Packaging must not reference renamed or missing tests")
        self.assertEqual(suite.countTestCases(), len(names))


if __name__ == "__main__":
    unittest.main()
