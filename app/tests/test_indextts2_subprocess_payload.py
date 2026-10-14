import tempfile
import unittest
from pathlib import Path

from src.core.synthesis.base import SynthesisRequest, VoiceProfile
from src.core.synthesis.indextts2_subprocess import _build_payload


class _Status:
    runtime_dir = Path("/tmp/lulu-runtime")
    model_dir = Path("/tmp/lulu-model")

    def snapshot(self):
        return {"ready": True}


class IndexTTS2SubprocessPayloadTests(unittest.TestCase):
    def test_25_request_preserves_language_speed_and_verified_model_version(self):
        class Status25(_Status):
            model_version = "2.5"
        request = SynthesisRequest(text="Hello", voice=VoiceProfile(
            id="v", name="V", reference_audio_path="/tmp/ref.wav"),
            output_path=Path("/tmp/output.wav"),
            engine_options={"language": "EN", "speed": 1.25, "device": "cpu"})
        payload = _build_payload(request, Status25(), Path("/tmp/staging.wav"), "v25")
        self.assertEqual(payload["model_version"], "2.5")
        self.assertEqual(payload["speech"], {"language": "EN", "speed": 1.25, "random_emotion": False})

    def test_custom_emotion_vector_takes_priority_over_preset(self):
        voice = VoiceProfile(
            id="voice-1",
            name="Voice",
            reference_audio_path="/tmp/reference.wav",
            reference_text="hello",
        )
        vector = [0.55, 0, 0, 0, 0, 0, 0.15, 0.1]
        request = SynthesisRequest(
            text="hello",
            voice=voice,
            output_path=Path("/tmp/out.wav"),
            engine_options={
                "emotion_preset": "happy",
                "emotion_intensity": "vivid",
                "emotion_vector": vector,
                "emotion_weight": 1.1,
                "emotion_summary": "基于「开心 + 鲜明」微调",
                "emotion_source_preset": "happy",
                "emotion_source_intensity": "vivid",
                "device": "cpu",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            payload = _build_payload(request, _Status(), Path(tmp) / "tmp.wav", "job-1")
        self.assertEqual(payload["emotion"]["vector"], vector)
        self.assertEqual(payload["emotion"]["weight"], 1.1)
        self.assertEqual(payload["emotion"]["mode"], "custom_vector")
        self.assertEqual(payload["emotion"]["summary"], "基于「开心 + 鲜明」微调")
        self.assertEqual(payload["emotion"]["source_intensity"], "vivid")

    def test_invalid_custom_emotion_vector_is_rejected(self):
        voice = VoiceProfile(
            id="voice-1",
            name="Voice",
            reference_audio_path="/tmp/reference.wav",
            reference_text="hello",
        )
        request = SynthesisRequest(
            text="hello",
            voice=voice,
            output_path=Path("/tmp/out.wav"),
            engine_options={
                "emotion_preset": "happy",
                "emotion_vector": [0.2] * 8,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _build_payload(request, _Status(), Path(tmp) / "tmp.wav", "job-1")


if __name__ == "__main__":
    unittest.main()
