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
from src.core.engines.local_qwen3 import LocalQwen3Engine
from src.core.engines.local_sensevoice import LocalSenseVoiceEngine
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

    def test_sensevoice_uses_native_progress_callback_and_checks_cancel(self):
        postprocess = types.ModuleType("funasr.utils.postprocess_utils")
        postprocess.rich_transcription_postprocess = lambda value: value

        class Model:
            def generate(self, **kwargs):
                kwargs["progress_callback"](25, 100)
                return [{"text": "你好"}]

        engine = LocalSenseVoiceEngine(device="cpu")
        engine._model = Model()
        seen = []
        with patch.dict(sys.modules, {"funasr.utils.postprocess_utils": postprocess}):
            result = engine.transcribe(
                Path("fixture.wav"),
                lambda message, fraction: seen.append((message, fraction)),
                is_cancelled=lambda: False,
            )
        self.assertEqual(result.text, "你好")
        self.assertIn(0.25, [fraction for _message, fraction in seen])

        with patch.dict(sys.modules, {"funasr.utils.postprocess_utils": postprocess}):
            with self.assertRaises(InterruptedError):
                engine.transcribe(
                    Path("fixture.wav"),
                    lambda *_args: None,
                    is_cancelled=lambda: True,
                )

    def test_sensevoice_extracts_first_supported_raw_language_token_before_postprocess(self):
        language_tokens = ("<|zh|>", "<|en|>", "<|yue|>", "<|ja|>", "<|ko|>")
        postprocess = types.ModuleType("funasr.utils.postprocess_utils")

        def clean(value):
            for token in language_tokens:
                value = value.replace(token, "")
            return value

        postprocess.rich_transcription_postprocess = clean
        cases = (
            ("<|zh|>你好", "zh", "你好"),
            ("<|en|>Hello", "en", "Hello"),
            ("<|yue|>你好", "yue", "你好"),
            ("<|ja|>こんにちは", "ja", "こんにちは"),
            ("<|ko|>안녕하세요", "ko", "안녕하세요"),
            ("<|unknown|>plain text", None, "<|unknown|>plain text"),
            ("<|en|>Hello<|zh|>你好", "en", "Hello你好"),
        )

        for raw, expected_language, expected_text in cases:
            with self.subTest(raw=raw):
                class Model:
                    def generate(self, **_kwargs):
                        return [{"key": "fixture", "text": raw}]

                engine = LocalSenseVoiceEngine(device="cpu")
                engine._model = Model()
                with patch.dict(
                    sys.modules,
                    {"funasr.utils.postprocess_utils": postprocess},
                ):
                    result = engine.transcribe(Path("fixture.wav"))

                self.assertEqual(result.text, expected_text)
                self.assertEqual(result.language, expected_language)

    def test_sensevoice_falls_back_to_existing_truthy_language_field(self):
        postprocess = types.ModuleType("funasr.utils.postprocess_utils")
        postprocess.rich_transcription_postprocess = lambda value: value.replace(
            "<|zh|>", ""
        )
        cases = (
            ({"key": "fixture", "text": "bonjour", "lang": "fr"}, "fr"),
            ({"key": "fixture", "text": "<|zh|>你好", "lang": "fr"}, "zh"),
        )

        for payload, expected_language in cases:
            with self.subTest(payload=payload):
                class Model:
                    def generate(self, **_kwargs):
                        return [payload]

                engine = LocalSenseVoiceEngine(device="cpu")
                engine._model = Model()
                with patch.dict(
                    sys.modules,
                    {"funasr.utils.postprocess_utils": postprocess},
                ):
                    result = engine.transcribe(Path("fixture.wav"))

                self.assertEqual(result.language, expected_language)

    def test_qwen_reports_completed_audio_chunks_and_offsets_timestamps(self):
        class Stamp:
            text = "字"
            start_time = 0.1
            end_time = 0.5

        class Model:
            def __init__(self):
                self.calls = 0

            def transcribe(self, **_kwargs):
                self.calls += 1
                return [types.SimpleNamespace(
                    text=f"段{self.calls}", language="Chinese", time_stamps=[Stamp()]
                )]

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            sf.write(wav, np.zeros(250, dtype=np.float32), 100)
            engine = LocalQwen3Engine(device="cpu")
            engine._model = Model()
            seen = []
            with patch("src.core.engines.local_qwen3._PROGRESS_CHUNK_SECONDS", 1.0):
                result = engine.transcribe(
                    wav,
                    lambda _message, fraction: seen.append(fraction),
                    is_cancelled=lambda: False,
                )

        self.assertEqual(result.text, "段1段2段3")
        self.assertEqual(engine._model.calls, 3)
        self.assertGreater(result.segments[-1].end, 2.0)
        self.assertTrue(any(0.0 < value < 1.0 for value in seen))


if __name__ == "__main__":
    unittest.main()
