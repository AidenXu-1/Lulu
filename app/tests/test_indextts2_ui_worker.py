import unittest
from unittest.mock import patch
from pathlib import Path

from src.core.synthesis.base import SynthesisResult, VoiceProfile
from src.ui import worker as ui_worker


class IndexTTS2UIWorkerTests(unittest.TestCase):
    def test_explicit_cancel_during_prepare_is_not_reported_as_failure(self):
        cancelled, failed = [], []
        def prepare(*args, **kwargs):
            worker._cancel = True
            raise RuntimeError('background closed during preparation')
        with patch.object(ui_worker, 'get_voice', return_value=VoiceProfile(
            id='v', name='Voice', reference_audio_path='/unused.wav')), \
             patch.object(ui_worker, 'IndexTTS2SubprocessEngine') as engine:
            engine.return_value.synthesize.side_effect = prepare
            worker = ui_worker.IndexTTS2SynthesisWorker('测试', 'v', '/unused-output.wav', 'natural')
            worker.cancelled.connect(lambda: cancelled.append(True))
            worker.failed.connect(failed.append)
            worker.run()
        self.assertEqual(cancelled, [True])
        self.assertEqual(failed, [])

    def test_worker_passes_custom_vector_payload(self):
        captured = {}
        original_get_voice = ui_worker.get_voice
        original_engine = ui_worker.IndexTTS2SubprocessEngine

        class _Engine:
            def synthesize(self, request, progress=None, is_cancelled=None):
                captured["engine_options"] = dict(request.engine_options)
                return SynthesisResult(
                    output_path=Path("/tmp/out.wav"),
                    wav_path=Path("/tmp/out.wav"),
                    duration_seconds=1.0,
                    engine="local_indextts2",
                    voice_id=request.voice.id,
                    text=request.text,
                )

        try:
            ui_worker.get_voice = lambda _voice_id: VoiceProfile(
                id="voice-1",
                name="Voice",
                reference_audio_path="/tmp/reference.wav",
                reference_text="hello",
            )
            ui_worker.IndexTTS2SubprocessEngine = _Engine
            worker = ui_worker.IndexTTS2SynthesisWorker(
                "hello",
                "voice-1",
                "/tmp/out.wav",
                "happy",
                {},
                "vivid",
                [0.55, 0, 0, 0, 0, 0, 0.15, 0.1],
                1.1,
                "基于「开心 + 鲜明」微调",
                "happy",
                "vivid",
                {"level": "risky", "label": "偏长可尝试", "char_count": 140},
                ["第一段。", "第二段。"],
                language="JA", speed=1.2,
            )
            worker.run()
        finally:
            ui_worker.get_voice = original_get_voice
            ui_worker.IndexTTS2SubprocessEngine = original_engine

        self.assertEqual(captured["engine_options"]["emotion_vector"], [0.55, 0, 0, 0, 0, 0, 0.15, 0.1])
        self.assertEqual(captured["engine_options"]["emotion_weight"], 1.1)
        self.assertEqual(captured["engine_options"]["emotion_summary"], "基于「开心 + 鲜明」微调")
        self.assertEqual(captured["engine_options"]["emotion_source_intensity"], "vivid")
        self.assertEqual(captured["engine_options"]["text_length"]["level"], "risky")
        self.assertEqual(captured["engine_options"]["text_length"]["label"], "偏长可尝试")
        self.assertEqual(captured["engine_options"]["text_segments"], ["第一段。", "第二段。"])
        self.assertEqual(captured["engine_options"]["language"], "JA")
        self.assertEqual(captured["engine_options"]["speed"], 1.2)

    def test_worker_preserves_segment_failure_for_user_visible_error(self):
        failed_messages = []
        original_get_voice = ui_worker.get_voice
        original_engine = ui_worker.IndexTTS2SubprocessEngine

        class _Engine:
            def synthesize(self, request, progress=None, is_cancelled=None):
                raise RuntimeError(
                    "第 2/3 段生成失败:stderr input text: 我的私密正文 "
                    "/Users/aiden/private/model.bin token sk-secret-123"
                )

        try:
            ui_worker.get_voice = lambda _voice_id: VoiceProfile(
                id="voice-1",
                name="Voice",
                reference_audio_path="/tmp/reference.wav",
                reference_text="hello",
            )
            ui_worker.IndexTTS2SubprocessEngine = _Engine
            worker = ui_worker.IndexTTS2SynthesisWorker(
                "hello",
                "voice-1",
                "/tmp/out.wav",
                "happy",
                {},
                "standard",
                text_segments=["第一段。", "第二段。", "第三段。"],
            )
            worker.failed.connect(failed_messages.append)
            worker.run()
        finally:
            ui_worker.get_voice = original_get_voice
            ui_worker.IndexTTS2SubprocessEngine = original_engine

        self.assertEqual(len(failed_messages), 1)
        self.assertIn("第 2/3 段生成失败", failed_messages[0])
        self.assertIn("input text: [已隐藏正文]", failed_messages[0])
        self.assertNotIn("本地环境尚未准备", failed_messages[0])
        self.assertNotIn("/Users/aiden", failed_messages[0])
        self.assertNotIn("我的私密正文", failed_messages[0])
        self.assertNotIn("sk-secret", failed_messages[0])


if __name__ == "__main__":
    unittest.main()
