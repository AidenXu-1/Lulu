from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import pipeline


class PipelineArtifactSafetyTests(unittest.TestCase):
    def test_keep_media_failure_never_exposes_partial_final_or_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"complete-media")
            output = root / "output"
            output.mkdir()
            final = output / source.name

            def fail_after_partial(_source, target):
                Path(target).write_bytes(b"partial")
                raise OSError("fixture interrupted copy")

            events: list[str] = []
            with patch.object(pipeline.shutil, "copy2", side_effect=fail_after_partial):
                result = pipeline._save_media(
                    source,
                    str(output),
                    lambda message, _frac: events.append(message),
                )

            self.assertIsNone(result)
            self.assertFalse(final.exists())
            self.assertEqual(list(output.glob(f".{source.name}.*.part")), [])
            self.assertTrue(any("保留音视频失败" in event for event in events))

            saved = pipeline._save_media(source, str(output), lambda *_args: None)
            self.assertEqual(saved, str(final))
            self.assertEqual(final.read_bytes(), b"complete-media")

    def test_keep_media_failure_preserves_existing_same_name_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"new-media")
            output = root / "output"
            output.mkdir()
            existing = output / source.name
            existing.write_bytes(b"old-media")

            real_copy2 = shutil.copy2

            def fail_after_partial(_source, target):
                Path(target).write_bytes(b"partial")
                raise OSError("fixture interrupted copy")

            with patch.object(pipeline.shutil, "copy2", side_effect=fail_after_partial):
                self.assertIsNone(
                    pipeline._save_media(source, str(output), lambda *_args: None)
                )

            self.assertEqual(existing.read_bytes(), b"old-media")
            self.assertFalse((output / "source_1.mp4").exists())
            self.assertEqual(list(output.glob(f".{source.name}.*.part")), [])

            with patch.object(pipeline.shutil, "copy2", wraps=real_copy2):
                saved = pipeline._save_media(
                    source,
                    str(output),
                    lambda *_args: None,
                )
            self.assertEqual(saved, str(output / "source_1.mp4"))
            self.assertEqual(Path(saved).read_bytes(), b"new-media")


if __name__ == "__main__":
    unittest.main()
