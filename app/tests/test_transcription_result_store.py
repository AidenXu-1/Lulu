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

    def test_write_is_owner_only_canonical_atomic_and_path_private(self) -> None:
        from src.core.transcription_result_store import TranscriptionResultStore

        with TemporaryDirectory(prefix="lulu-result-store-") as directory:
            application_support = Path(directory).resolve()
            os.chmod(application_support, 0o700)
            layout = SharedProductStorageLayout.from_application_support_root(
                application_support
            )
            store = TranscriptionResultStore(layout)
            result = LocalFileTranscriptionResultV1(
                text="本地结果。",
                segments=(
                    LocalTranscriptionSegmentV1("本地结果。", 0.0, 1.25),
                ),
                language="zh",
                engine="local_sensevoice",
                source_path="/private/user/source-secret.wav",
            )

            receipt = store.write("job-result-001", result)

            results_directory = application_support / "Lulu" / "jobs" / "results"
            target = results_directory / "job-result-001.json"
            self.assertEqual(stat.S_IMODE(results_directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertTrue(stat.S_ISREG(target.lstat().st_mode))
            raw = target.read_bytes()
            self.assertEqual(raw, json.dumps(
                json.loads(raw),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n")
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(receipt, {
                "result_ref": f"lulu-result:job-result-001:sha256:{digest}",
                "content_hash": digest,
            })
            stored = store.read("job-result-001")
            self.assertEqual(stored["schema_id"], "lulu.transcription-result/v1")
            self.assertEqual(stored["job_id"], "job-result-001")
            self.assertEqual(stored["transcript"]["text"], "本地结果。")
            self.assertNotIn("source_path", stored["transcript"])
            self.assertNotIn("source-secret", raw.decode("utf-8"))
            self.assertEqual(list(results_directory.glob(".*.tmp")), [])

    def test_symlinked_results_directory_fails_without_touching_target(self) -> None:
        from src.core.transcription_result_store import (
            TranscriptionResultStore,
            TranscriptionResultStoreError,
        )

        with TemporaryDirectory(prefix="lulu-result-store-link-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            layout = SharedProductStorageLayout.from_application_support_root(root)
            (root / "Lulu" / "jobs").mkdir(parents=True, mode=0o700)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            (root / "Lulu" / "jobs" / "results").symlink_to(
                outside,
                target_is_directory=True,
            )
            result = LocalFileTranscriptionResultV1(
                text="blocked",
                segments=(),
                language=None,
                engine="local_sensevoice",
                source_path="/private/source.wav",
            )

            with self.assertRaises(TranscriptionResultStoreError):
                TranscriptionResultStore(layout).write("job-result-002", result)

            self.assertEqual(tuple(outside.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
