from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import numpy as np

from src.core.engines.local_whisper_mlx import LocalWhisperMLXEngine
from src.core.engines.mlx_whisper_progress import (
    _ProgressController,
    transcribe_with_audio_progress,
)


class ProgressControllerTests(unittest.TestCase):
    def test_regular_and_silent_windows_report_real_monotonic_position(self):
        events: list[float] = []
        controller = _ProgressController(events.append, window_frames=3000)
        first = object()
        silent = object()
        last = object()

        controller.start(9000)
        controller.before_decode(first)
        controller.update(3000)
        controller.before_decode(silent)
        controller.before_decode(last)  # 上一个窗口没有 pbar.update，说明静音跳过。
        controller.update(3000)
        controller.finish(success=True)

        self.assertEqual(len(events), 4)
        self.assertAlmostEqual(events[0], 0.0)
        self.assertAlmostEqual(events[1], 1 / 3)
        self.assertAlmostEqual(events[2], 2 / 3)
        self.assertAlmostEqual(events[3], 1.0)
        self.assertEqual(events, sorted(events))

    def test_temperature_fallback_on_same_window_never_double_counts(self):
        events: list[float] = []
        controller = _ProgressController(events.append, window_frames=3000)
        segment = object()

        controller.start(6000)
        controller.before_decode(segment)
        controller.before_decode(segment)
        controller.before_decode(segment)
        controller.update(3000)
        controller.finish(success=False)

        self.assertEqual(events, [0.0, 0.5])

    def test_failed_window_is_not_reported_as_finished(self):
        events: list[float] = []
        controller = _ProgressController(events.append, window_frames=3000)
        controller.start(6000)
        controller.before_decode(object())
        controller.finish(success=False)

        self.assertEqual(events, [0.0])

    def test_progress_callback_failure_cannot_break_controller(self):
        calls = 0

        def broken_callback(_fraction: float) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("界面已销毁")

        controller = _ProgressController(broken_callback, window_frames=3000)
        controller.start(3000)
        controller.before_decode(object())
        controller.update(3000)
        controller.finish(success=True)

        self.assertEqual(calls, 2)


class WhisperBridgeTests(unittest.TestCase):
    @staticmethod
    def _fake_modules(*, compatible: bool = True):
        transcribe_module = ModuleType("mlx_whisper.transcribe")
        package = ModuleType("mlx_whisper")

        class FakeModel:
            def decode(self, _segment, _options):
                return object()

        if compatible:
            class ModelHolder:
                @classmethod
                def get_model(cls, _path, _dtype):
                    return FakeModel()

            class FakeTqdmModule:
                @staticmethod
                def tqdm(*_args, **_kwargs):
                    raise AssertionError("兼容桥应该替换这个入口")

            transcribe_module.ModelHolder = ModelHolder
            transcribe_module.tqdm = FakeTqdmModule()
            transcribe_module.N_FRAMES = 3000
            exec(
                """
def transcribe(audio, *, path_or_hf_repo):
    model = ModelHolder.get_model(path_or_hf_repo, None)
    with tqdm.tqdm(total=6000, unit='frames', disable=True) as pbar:
        model.decode(object(), None)
        pbar.update(3000)
        model.decode(object(), None)
        # 第二个窗口模拟上游静音 continue：没有 pbar.update。
    return {'text': '保持原结果', 'segments': [], 'language': 'zh'}
""",
                transcribe_module.__dict__,
            )
        else:
            exec(
                """
def transcribe(audio, *, path_or_hf_repo):
    return {'text': '降级仍可转录', 'segments': [], 'language': 'zh'}
""",
                transcribe_module.__dict__,
            )

        package.transcribe = transcribe_module.transcribe
        return package, transcribe_module

    def test_bridge_keeps_upstream_result_and_restores_module_objects(self):
        package, module = self._fake_modules()
        original_descriptor = module.ModelHolder.__dict__["get_model"]
        original_tqdm = module.tqdm
        events: list[float] = []

        with patch.dict(
            sys.modules,
            {"mlx_whisper": package, "mlx_whisper.transcribe": module},
        ):
            result = transcribe_with_audio_progress(
                object(),
                path_or_hf_repo="fixture-model",
                on_audio_position=events.append,
            )

        self.assertEqual(result["text"], "保持原结果")
        self.assertEqual(events, [0.0, 0.5, 1.0])
        self.assertIs(module.ModelHolder.__dict__["get_model"], original_descriptor)
        self.assertIs(module.tqdm, original_tqdm)

    def test_incompatible_upstream_falls_back_without_progress_or_retry(self):
        package, module = self._fake_modules(compatible=False)
        events: list[float] = []

        with patch.dict(
            sys.modules,
            {"mlx_whisper": package, "mlx_whisper.transcribe": module},
        ):
            result = transcribe_with_audio_progress(
                object(),
                path_or_hf_repo="fixture-model",
                on_audio_position=events.append,
            )

        self.assertEqual(result["text"], "降级仍可转录")
        self.assertEqual(events, [])


class LocalWhisperProgressContractTests(unittest.TestCase):
    def test_engine_exposes_audio_position_without_changing_result_mapping(self):
        events: list[tuple[str, float]] = []

        def fake_transcribe(_audio, *, path_or_hf_repo, on_audio_position):
            self.assertEqual(path_or_hf_repo, "/fixture/model")
            on_audio_position(0.0)
            on_audio_position(0.47)
            on_audio_position(1.0)
            return {
                "text": " 测试文稿 ",
                "segments": [{"text": " 测试文稿 ", "start": 0.0, "end": 1.0}],
                "language": "zh",
            }

        with (
            patch("soundfile.read", return_value=(np.zeros(16000, dtype=np.float32), 16000)),
            patch("src.core.models_manager.model_path", return_value=Path("/fixture/model")),
            patch(
                "src.core.engines.local_whisper_mlx.transcribe_with_audio_progress",
                side_effect=fake_transcribe,
            ),
        ):
            result = LocalWhisperMLXEngine().transcribe(
                Path("/fixture/audio.wav"),
                lambda message, fraction: events.append((message, fraction)),
            )

        self.assertEqual(result.text, "测试文稿")
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertIn(("正在转录(Whisper MLX) · 已处理音频 47%", 0.47), events)
        self.assertEqual(events[-1], ("完成", 1.0))


if __name__ == "__main__":
    unittest.main()
