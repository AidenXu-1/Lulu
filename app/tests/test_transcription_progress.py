from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from src.core import pipeline
from src.core.engines.local_whisper_mlx import LocalWhisperMLXEngine
from src.core.models import Transcript


class TranscriptionProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        local_data = patch('src.core.models_manager.app_data_dir', return_value=Path(temp.name).resolve())
        local_data.start()
        self.addCleanup(local_data.stop)

    def test_pipeline_maps_real_engine_progress_into_transcription_stage(self):
        seen = []
        cancel_probe = lambda: False

        class Engine:
            display_name = "测试本地引擎"

            def transcribe(self, _wav, progress, is_cancelled=None):
                self.cancel_probe = is_cancelled
                progress("真实处理到一半", 0.5)
                return Transcript(text="完成")

        engine = Engine()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.wav"
            source.write_bytes(b"fixture")
            with patch.object(pipeline, "resolve_source", return_value=source), patch.object(
                pipeline, "to_wav_16k_mono", return_value=source
            ), patch.object(pipeline, "get_engine", return_value=engine):
                pipeline.transcribe_source(
                    str(source),
                    progress=lambda message, fraction: seen.append((message, fraction)),
                    is_cancelled=cancel_probe,
                )

        fraction = next(value for message, value in seen if message == "真实处理到一半")
        self.assertAlmostEqual(fraction, 0.635)
        self.assertIs(engine.cancel_probe, cancel_probe)


if __name__ == "__main__":
    unittest.main()
