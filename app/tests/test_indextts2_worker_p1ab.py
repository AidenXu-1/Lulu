import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
import wave
from unittest.mock import patch
from pathlib import Path

from src.core.synthesis import indextts2_worker
from src.core.synthesis import indextts2_subprocess as subprocess_adapter
from src.core.synthesis.base import SynthesisError, SynthesisRequest, VoiceProfile


class _Status:
    ready = True
    runtime_ready = True
    model_ready = True
    config_ready = True
    runtime_dir = Path("/tmp/lulu-runtime")
    model_dir = Path("/tmp/lulu-model")
    detail_redacted = ""

    def snapshot(self) -> dict:
        return {
            "ready": self.ready,
            "runtime_ready": self.runtime_ready,
            "model_ready": self.model_ready,
            "config_ready": self.config_ready,
        }


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 2205)


def _events_from_run(request_path: Path) -> tuple[int, list[dict]]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = indextts2_worker.run_request(request_path)
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    return code, events


class IndexTTS2WorkerP1abTests(unittest.TestCase):
    def setUp(self):
        device = patch.object(subprocess_adapter, 'detect_device', return_value='cpu')
        device.start()
        self.addCleanup(device.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        path = patch.object(sys, 'path', list(sys.path))
        path.start()
        self.addCleanup(path.stop)

    def tearDown(self):
        for name in list(sys.modules):
            if name == "indextts" or name.startswith("indextts."):
                sys.modules.pop(name, None)

    def _fake_runtime(self, tmp: Path, infer_body: str) -> tuple[Path, Path, Path]:
        runtime = tmp / "runtime"
        python = runtime / "python" / "bin" / "python3.11"
        python.parent.mkdir(parents=True)
        python.write_text("# fake python", encoding="utf-8")
        vendor = runtime / "vendor" / "index-tts"
        package = vendor / "indextts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        method_body = textwrap.indent(textwrap.dedent(infer_body).strip() + "\n", " " * 8)
        (package / "infer_v2.py").write_text(
            (
                "import wave\n\n"
                "def _write_wav(path):\n"
                "    with wave.open(str(path), \"wb\") as wav:\n"
                "        wav.setnchannels(1)\n"
                "        wav.setsampwidth(2)\n"
                "        wav.setframerate(22050)\n"
                "        wav.writeframes(b\"\\\\0\\\\0\" * 2205)\n\n"
                "class IndexTTS2:\n"
                "    def __init__(self, *args, **kwargs):\n"
                "        self.gr_progress = None\n\n"
                "    def infer(self, **kwargs):\n"
                f"{method_body}"
            ),
            encoding="utf-8",
        )
        model = tmp / "model"
        model.mkdir()
        return runtime, vendor, model

    def _fake_persistent_runtime(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        runtime = tmp / "runtime"
        python = Path(sys.executable)
        vendor = runtime / "vendor" / "index-tts"
        package = vendor / "indextts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        counter = tmp / "init_count.txt"
        (package / "infer_v2.py").write_text(
            (
                "import os, wave\n"
                f"COUNTER = {str(counter)!r}\n\n"
                "def _write_wav(path):\n"
                "    with wave.open(str(path), \"wb\") as wav:\n"
                "        wav.setnchannels(1)\n"
                "        wav.setsampwidth(2)\n"
                "        wav.setframerate(22050)\n"
                "        wav.writeframes(b\"\\\\0\\\\0\" * 2205)\n\n"
                "class IndexTTS2:\n"
                "    def __init__(self, *args, **kwargs):\n"
                "        self.gr_progress = None\n"
                "        current = 0\n"
                "        if os.path.exists(COUNTER):\n"
                "            current = int(open(COUNTER).read().strip() or '0')\n"
                "        open(COUNTER, 'w').write(str(current + 1))\n\n"
                "    def infer(self, **kwargs):\n"
                "        if self.gr_progress:\n"
                "            self.gr_progress(0.2, desc=\"speech synthesis 1/1...\")\n"
                "            self.gr_progress(0.9, desc=\"saving audio...\")\n"
                "        _write_wav(kwargs[\"output_path\"])\n"
                "        return kwargs[\"output_path\"]\n"
            ),
            encoding="utf-8",
        )
        model = tmp / "model"
        model.mkdir()
        (model / "config.yaml").write_text("fake: true\n", encoding="utf-8")
        return runtime, vendor, model, counter

    def _request_json(self, tmp: Path, runtime: Path, vendor: Path, model: Path, ref: Path, out: Path) -> Path:
        payload = {
            "job_id": "job-test",
            "runtime_dir": str(runtime),
            "runtime_python": str(runtime / "python" / "bin" / "python3.11"),
            "vendor_dir": str(vendor),
            "nltk_data_dir": str(runtime / "python" / "nltk_data"),
            "model_dir": str(model),
            "text": "短文本。",
            "voice": {"reference_audio_path": str(ref)},
            "output": {"temp_wav_path": str(out)},
            "emotion": {"control_mode": "template", "vector": [0.0] * 8, "weight": 0.8},
        }
        request = tmp / "request.json"
        request.write_text(json.dumps(payload), encoding="utf-8")
        return request


    def test_forced_persistent_worker_exit_does_not_load_model_twice(self):
        original = subprocess_adapter.check_runtime
        original_generate = subprocess_adapter._PERSISTENT_MANAGER.generate  # type: ignore[attr-defined]
        subprocess_adapter.check_runtime = lambda: _Status()
        calls: list[str] = []

        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, *_args, **_kwargs):
                calls.append("one-shot")
                raise AssertionError("resource-pressure crash must not reload the model")

        def _forced_exit(*_args, **_kwargs):
            raise subprocess_adapter.PersistentWorkerCrashed(
                "IndexTTS2 后台进程已退出。",
                returncode=-9,
            )

        subprocess_adapter._PERSISTENT_MANAGER.generate = _forced_exit  # type: ignore[attr-defined]
        try:
            with tempfile.TemporaryDirectory() as tmp_name:
                tmp = Path(tmp_name)
                ref = tmp / "ref.wav"
                _write_wav(ref)
                voice = VoiceProfile(id="v1", name="V", reference_audio_path=str(ref), reference_text="")
                with self.assertRaises(SynthesisError) as caught:
                    _Engine().synthesize(
                        SynthesisRequest(
                            text="短文本。",
                            voice=voice,
                            output_path=tmp / "out.wav",
                            engine_options={"use_persistent_worker": True},
                        )
                    )
        finally:
            subprocess_adapter.check_runtime = original
            subprocess_adapter._PERSISTENT_MANAGER.generate = original_generate  # type: ignore[attr-defined]

        self.assertEqual(calls, [])
        self.assertIn("forced_exit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
