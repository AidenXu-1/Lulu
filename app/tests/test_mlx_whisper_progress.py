from __future__ import annotations

import sys
import unittest
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import numpy as np

from src.core.engines.local_whisper_mlx import LocalWhisperMLXEngine
from src.core.engines.mlx_whisper_progress import (
    _ProgressController,
    transcribe_with_audio_progress,
)












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








class WhisperCancellationTests(unittest.TestCase):
    def test_both_engines_stop_before_next_decode_and_release_resource(self):
        from src.core.engines.asr_resource import maintenance
        for model_id in ("local_whisper_mlx", "local_whisper_mlx_4bit"):
            for mode in ("normal", "silent", "retry", "unchanged_percent"):
                with self.subTest(model=model_id, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    package, module = WhisperBridgeTests._fake_modules()
                    cancelled = threading.Event()
                    windows = []
                    segment = object()
                    def upstream(audio, *, path_or_hf_repo):
                        model = module.ModelHolder.get_model(path_or_hf_repo, None)
                        with module.tqdm.tqdm(total=1000000) as bar:
                            for i in range(8):
                                if i == 1:
                                    cancelled.set()
                                model.decode(segment if mode == "retry" else object(), None)
                                windows.append(i)
                                if mode != "silent":
                                    bar.update(1 if mode == "unchanged_percent" else 3000)
                        return {"text": "不应保存", "segments": []}
                    package.transcribe = upstream
                    descriptor = module.ModelHolder.__dict__["get_model"]
                    tqdm = module.tqdm
                    with (patch.dict(sys.modules, {"mlx_whisper": package, "mlx_whisper.transcribe": module}),
                          patch("src.core.models_manager.app_data_dir", return_value=Path(tmp).resolve()),
                          patch("src.core.models_manager.is_downloaded", return_value=True),
                          patch("src.core.models_manager.model_path", return_value=Path(tmp)),
                          patch("soundfile.read", return_value=(np.ones(16000, dtype=np.float32), 16000))):
                        with self.assertRaises(InterruptedError):
                            LocalWhisperMLXEngine(model_id).transcribe(Path("/fixture.wav"),
                                                                      is_cancelled=cancelled.is_set)
                        self.assertEqual(windows, [0])
                        self.assertIs(module.ModelHolder.__dict__["get_model"], descriptor)
                        self.assertIs(module.tqdm, tqdm)
                        with maintenance():
                            pass

    def test_progress_interrupted_error_is_never_swallowed(self):
        def cancel(_):
            raise InterruptedError("已取消")
        with self.assertRaises(InterruptedError):
            _ProgressController(cancel, window_frames=3000).start(6000)

    def test_progress_fallback_keeps_cancellation_boundary(self):
        package, module = WhisperBridgeTests._fake_modules()
        del module.N_FRAMES
        cancelled = threading.Event()
        windows = []
        def upstream(audio, *, path_or_hf_repo):
            model = module.ModelHolder.get_model(path_or_hf_repo, None)
            for i in range(8):
                if i == 1:
                    cancelled.set()
                model.decode(object(), None)
                windows.append(i)
        package.transcribe = upstream
        with patch.dict(sys.modules, {"mlx_whisper": package, "mlx_whisper.transcribe": module}):
            with self.assertRaises(InterruptedError):
                transcribe_with_audio_progress(None, path_or_hf_repo="fixture",
                                               on_audio_position=lambda _: None,
                                               is_cancelled=cancelled.is_set)
        self.assertEqual(windows, [0])

    def test_unknown_cancellation_contract_refuses_before_inference(self):
        package, module = WhisperBridgeTests._fake_modules(compatible=False)
        with patch.dict(sys.modules, {"mlx_whisper": package, "mlx_whisper.transcribe": module}), \
                patch.object(package, "transcribe") as infer:
            with self.assertRaisesRegex(RuntimeError, "取消"):
                transcribe_with_audio_progress(None, path_or_hf_repo="fixture",
                                               on_audio_position=lambda _: None,
                                               is_cancelled=lambda: False)
            infer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
