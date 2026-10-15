from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_contracts import (
    LocalFileTranscriptionResultV1,
    LocalTranscriptionSegmentV1,
)


class TranscriptionResultStoreTests(unittest.TestCase):
    def test_first_install_with_standard_parent_permissions_keeps_results_private(self):
        from src.core.transcription_result_store import TranscriptionResultStore
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o755)
            (root / "Lulu").mkdir(mode=0o755)
            layout = SharedProductStorageLayout.from_application_support_root(root)
            store = TranscriptionResultStore(layout)
            result = LocalFileTranscriptionResultV1("首装结果", (), "zh", "local_sensevoice", "/test.wav")
            store.write("first-install", result)
            self.assertEqual(store.read("first-install")["transcript"]["text"], "首装结果")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(layout.product_root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(layout.jobs.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((store.directory / "first-install.json").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
