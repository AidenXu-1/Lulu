from __future__ import annotations

import os
from pathlib import Path
import runpy
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(APP / "packaging/privacy_gate.py"))


class ReleasePrivacyGateTests(unittest.TestCase):

    def test_local_wheel_provenance_is_removed_without_losing_package_metadata(self):
        entries = [
            ("deno-2.9.5.dist-info/direct_url.json", "/private-build/wheel-origin", "DATA"),
            ("deno-2.9.5.dist-info/METADATA", "/metadata", "DATA"),
            ("deno-2.9.5.dist-info/licenses/LICENSE", "/license", "DATA"),
            ("yt_dlp_ejs/yt/solver/core.min.js", "/script", "DATA"),
            ("some_runtime/direct_url.json", "/runtime", "DATA"),
        ]
        self.assertEqual(GATE["drop_non_runtime_toc_entries"](entries), entries[1:])


if __name__ == "__main__":
    unittest.main()
