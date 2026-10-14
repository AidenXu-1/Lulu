import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from src.core.synthesis.base import SynthesisError, SynthesisRequest, VoiceProfile
from src.core.synthesis import indextts2_subprocess as subprocess_adapter


class _Status:
    ready = True
    runtime_ready = True
    model_ready = True
    config_ready = True
    runtime_dir = Path("/tmp/lulu-runtime")
    model_dir = Path("/tmp/lulu-model")
    detail_redacted = ""

    def snapshot(self):
        return {"ready": True}


def _write_tone(path: Path, seconds: float = 0.04) -> None:
    sample_rate = 16000
    data = np.full(int(sample_rate * seconds), 0.1, dtype="float32")
    sf.write(str(path), data, sample_rate)


class IndexTTS2SegmentedEngineTests(unittest.TestCase):
    def setUp(self):
        self._original_check_runtime = subprocess_adapter.check_runtime
        subprocess_adapter.check_runtime = lambda: _Status()

    def tearDown(self):
        subprocess_adapter.check_runtime = self._original_check_runtime

    def _request(self, ref: Path, out: Path, segments: list[str]) -> SynthesisRequest:
        voice = VoiceProfile(
            id="voice-1",
            name="Voice",
            reference_audio_path=str(ref),
            reference_text="hello",
        )
        return SynthesisRequest(
            text="\n".join(segments),
            voice=voice,
            output_path=out,
            engine_options={"text_segments": segments, "emotion_preset": "natural"},
        )

    def test_segmented_engine_stitches_fake_parts(self):
        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, request, status, output_wav, job_id, progress, is_cancelled, **_kwargs):
                _write_tone(output_wav)
                progress("fake done", 1.0)
                return {"duration_seconds": 0.04, "meta": {"segment_text": request.text}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            _write_tone(ref)
            out = tmp_path / "out.wav"
            result = _Engine().synthesize(self._request(ref, out, ["第一段。", "第二段。"]))
            self.assertTrue(result.wav_path.exists())

        self.assertTrue(result.meta["segment_generation"])
        self.assertEqual(result.meta["segment_count"], 2)
        self.assertGreater(result.duration_seconds, 0)

    def test_segment_failure_mentions_failed_index_and_scrubs_path(self):
        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, request, status, output_wav, job_id, progress, is_cancelled, **_kwargs):
                if "第二段" in request.text:
                    raise SynthesisError("第 2/2 段生成失败:bad file /Users/aiden/private/model.bin")
                _write_tone(output_wav)
                return {"duration_seconds": 0.04, "meta": {}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            _write_tone(ref)
            with self.assertRaises(SynthesisError) as caught:
                _Engine().synthesize(self._request(ref, tmp_path / "out.wav", ["第一段。", "第二段。"]))

        self.assertIn("第 2/2 段生成失败", str(caught.exception))
        self.assertNotIn("/Users/aiden", str(caught.exception))

    def test_segment_failure_preserves_tail_after_warning_head(self):
        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, request, status, output_wav, job_id, progress, is_cancelled, **_kwargs):
                if "第四段" in request.text:
                    raise SynthesisError(
                        "第 4/4 段生成失败:IndexTTS2 生成失败:"
                        + "GPT2InferenceModel has generative capabilities " * 30
                        + "RuntimeError: final chunk failed at /Users/aiden/private/model.bin"
                    )
                _write_tone(output_wav)
                return {"duration_seconds": 0.04, "meta": {}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            _write_tone(ref)
            with self.assertRaises(SynthesisError) as caught:
                _Engine().synthesize(
                    self._request(ref, tmp_path / "out.wav", ["第一段。", "第二段。", "第三段。", "第四段。"])
                )

        msg = str(caught.exception)
        self.assertIn("第 4/4 段生成失败", msg)
        self.assertIn("RuntimeError: final chunk failed", msg)
        self.assertNotIn("/Users/aiden", msg)

    def test_segment_cancel_propagates_cancelled_state(self):
        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, request, status, output_wav, job_id, progress, is_cancelled, **_kwargs):
                if "第二段" in request.text:
                    raise subprocess_adapter._Cancelled()
                _write_tone(output_wav)
                return {"duration_seconds": 0.04, "meta": {}}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref = tmp_path / "ref.wav"
            _write_tone(ref)
            with self.assertRaises(Exception) as caught:
                _Engine().synthesize(self._request(ref, tmp_path / "out.wav", ["第一段。", "第二段。"]))

        self.assertTrue(subprocess_adapter.was_cancelled(caught.exception))


if __name__ == "__main__":
    unittest.main()
