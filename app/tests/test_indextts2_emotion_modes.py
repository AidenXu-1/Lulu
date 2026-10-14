"""配音页重构 · IndexTTS2 五种情感控制方式 接出验证。

- adapter `_build_payload`:emotion.control_mode/audio_path/text 按模式正确填充(含向后兼容回退)。
- worker `run_request`:按 control_mode 给 IndexTTS2.infer 透传不同情感入口
  (follow 不传 / emoaudio->emo_audio_prompt / emotext->emo_text / template|vector->emo_vector),
  并校验情感参考音频缺失、情感描述文本为空的失败路径。worker 用假 indextts 包记录 infer kwargs。
"""

import io
import json
import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import soundfile as sf

from src.core.synthesis import indextts2_worker
from src.core.synthesis.base import SynthesisRequest, VoiceProfile
from src.core.synthesis.indextts2_subprocess import _build_payload


class _Status:
    runtime_dir = Path("/tmp/lulu-runtime")
    model_dir = Path("/tmp/lulu-model")

    def snapshot(self):
        return {"ready": True}


def _payload(**engine_options):
    voice = VoiceProfile(id="v1", name="Voice", reference_audio_path="/tmp/ref.wav", reference_text="hi")
    req = SynthesisRequest(text="你好", voice=voice, output_path=Path("/tmp/out.wav"),
                           engine_options={"device": "cpu", **engine_options})
    with tempfile.TemporaryDirectory() as tmp:
        return _build_payload(req, _Status(), Path(tmp) / "tmp.wav", "job-1")


class BuildPayloadControlModeTests(unittest.TestCase):
    def test_explicit_modes_are_passed_through(self):
        self.assertEqual(_payload(emotion_mode="follow")["emotion"]["control_mode"], "follow")
        emo = _payload(emotion_mode="emoaudio", emotion_audio_path="/tmp/emo.wav")["emotion"]
        self.assertEqual(emo["control_mode"], "emoaudio")
        self.assertEqual(emo["audio_path"], "/tmp/emo.wav")
        emo = _payload(emotion_mode="emotext", emotion_text="委屈巴巴")["emotion"]
        self.assertEqual(emo["control_mode"], "emotext")
        self.assertEqual(emo["text"], "委屈巴巴")
        self.assertEqual(_payload(emotion_mode="template", emotion_preset="happy")["emotion"]["control_mode"], "template")

    def test_default_falls_back_by_vector_presence(self):
        # 无 emotion_mode + 有自定义向量 → vector;无向量 → template(向后兼容旧调用)
        self.assertEqual(_payload(emotion_vector=[0.55, 0, 0, 0, 0, 0, 0.15, 0.1])["emotion"]["control_mode"], "vector")
        self.assertEqual(_payload(emotion_preset="happy")["emotion"]["control_mode"], "template")

    def test_unknown_mode_falls_back(self):
        self.assertEqual(_payload(emotion_mode="garbage")["emotion"]["control_mode"], "template")


class WorkerEmotionCliTests(unittest.TestCase):
    def tearDown(self):
        for name in list(os.sys.modules):
            if name == "indextts" or name.startswith("indextts."):
                os.sys.modules.pop(name, None)

    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        path = patch.object(os.sys, 'path', list(os.sys.path))
        path.start()
        self.addCleanup(path.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "python" / "bin").mkdir(parents=True)
        vendor = self.runtime / "vendor" / "index-tts"
        package = vendor / "indextts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.model = self.root / "model"; self.model.mkdir()
        self.ref = self.root / "ref.wav"; sf.write(str(self.ref), np.zeros(1600, dtype="float32"), 16000)
        self.emo = self.root / "emo.wav"; sf.write(str(self.emo), np.zeros(1600, dtype="float32"), 16000)
        self.kwargs_file = self.root / "infer_kwargs.json"
        self.home_file = self.root / "home.txt"
        binpath = self.runtime / "python" / "bin" / "python3.11"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(binpath, 0o755)
        (package / "infer_v2.py").write_text(
            textwrap.dedent(
                f"""
                import json
                import os
                import wave

                KWARGS_FILE = {str(self.kwargs_file)!r}
                HOME_FILE = {str(self.home_file)!r}

                def _write_wav(path):
                    with wave.open(str(path), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(22050)
                        wav.writeframes(b"\\0\\0" * 2205)

                class IndexTTS2:
                    def __init__(self, *args, **kwargs):
                        self.gr_progress = None

                    def infer(self, **kwargs):
                        serializable = {{k: v for k, v in kwargs.items() if k != "output_path"}}
                        with open(KWARGS_FILE, "w", encoding="utf-8") as fp:
                            fp.write(json.dumps(serializable, ensure_ascii=False, sort_keys=True))
                        with open(HOME_FILE, "w", encoding="utf-8") as fp:
                            fp.write(os.environ.get("HOME", ""))
                        _write_wav(kwargs["output_path"])
                        return kwargs["output_path"]
                """
            ),
            encoding="utf-8",
        )

    def _run(self, emotion):
        temp_wav = self.root / "temp.wav"
        payload = {
            "job_id": "t",
            "runtime_dir": str(self.runtime),
            "runtime_python": str(self.runtime / "python" / "bin" / "python3.11"),
            "vendor_dir": str(self.runtime / "vendor" / "index-tts"),
            "nltk_data_dir": str(self.runtime / "python" / "nltk_data"),
            "model_dir": str(self.model),
            "voice": {"reference_audio_path": str(self.ref)},
            "output": {"temp_wav_path": str(temp_wav)},
            "text": "你好世界", "emotion": emotion, "device": "cpu",
            "options": {"timeout_seconds": 60},
        }
        req = self.root / "req.json"; req.write_text(json.dumps(payload), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            rc = indextts2_worker.run_request(req)
        kwargs = json.loads(self.kwargs_file.read_text(encoding="utf-8")) if self.kwargs_file.exists() else {}
        return rc, kwargs

    def test_template_uses_emotion_vector(self):
        rc, kwargs = self._run({"control_mode": "template", "vector": [0.5] + [0.0] * 7, "weight": 0.8})
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs.get("emo_vector"), [0.5] + [0.0] * 7)
        self.assertEqual(kwargs.get("emo_alpha"), 0.8)
        self.assertNotIn("emo_audio_prompt", kwargs)
        self.assertNotIn("emo_text", kwargs)

    def test_emoaudio_passes_audio_path(self):
        rc, kwargs = self._run({"control_mode": "emoaudio", "audio_path": str(self.emo), "weight": 0.7})
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs.get("emo_audio_prompt"), str(self.emo))
        self.assertEqual(kwargs.get("emo_alpha"), 0.7)
        self.assertNotIn("emo_vector", kwargs)

    def test_emoaudio_missing_file_fails(self):
        rc, kwargs = self._run({"control_mode": "emoaudio", "audio_path": str(self.root / "nope.wav")})
        self.assertNotEqual(rc, 0)
        self.assertEqual(kwargs, {})   # 未走到 infer

    def test_emotext_passes_text(self):
        rc, kwargs = self._run({"control_mode": "emotext", "text": "委屈巴巴", "weight": 0.6})
        self.assertEqual(rc, 0)
        self.assertTrue(kwargs.get("use_emo_text"))
        self.assertEqual(kwargs.get("emo_text"), "委屈巴巴")
        self.assertEqual(kwargs.get("emo_alpha"), 0.6)
        self.assertNotIn("emo_vector", kwargs)

    def test_emotext_empty_fails(self):
        rc, kwargs = self._run({"control_mode": "emotext", "text": "   "})
        self.assertNotEqual(rc, 0)
        self.assertEqual(kwargs, {})

    def test_follow_passes_no_emotion_control(self):
        rc, kwargs = self._run({"control_mode": "follow", "vector": [0.5] + [0.0] * 7, "weight": 0.8})
        self.assertEqual(rc, 0)
        for key in ("emo_vector", "emo_audio_prompt", "emo_text", "emo_alpha"):
            self.assertNotIn(key, kwargs)

    def test_worker_redirects_cli_home_to_temp_dir(self):
        rc, _kwargs = self._run({"control_mode": "follow"})
        self.assertEqual(rc, 0)
        home = self.home_file.read_text(encoding="utf-8").strip()
        self.assertEqual(home, str((self.root / "indextts_home").resolve()))
        self.assertNotIn(str(Path.home() / "Library" / "Application Support" / "IndexTTS"), home)


if __name__ == "__main__":
    unittest.main()
