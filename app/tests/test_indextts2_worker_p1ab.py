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

    def test_single_path_bridges_gr_progress_without_cli_coarse_55_percent(self):
        infer_body = """
        if self.gr_progress:
            self.gr_progress(0, desc="starting inference...")
            self.gr_progress(0.1, desc="text processing...")
            self.gr_progress(0.2, desc="speech synthesis 1/1...")
            self.gr_progress(0.9, desc="saving audio...")
        _write_wav(kwargs["output_path"])
        return kwargs["output_path"]
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime, vendor, model = self._fake_runtime(tmp, infer_body)
            ref = tmp / "ref.wav"
            _write_wav(ref)
            out = tmp / "out.wav"
            code, events = _events_from_run(self._request_json(tmp, runtime, vendor, model, ref, out))

        self.assertEqual(code, 0)
        progress = [event for event in events if event.get("type") == "progress"]
        fractions = [round(float(event["fraction"]), 3) for event in progress]
        messages = [event["message"] for event in progress]
        self.assertIn("starting inference...", messages)
        self.assertIn("text processing...", messages)
        self.assertIn("speech synthesis 1/1...", messages)
        self.assertNotIn(0.55, fractions)
        self.assertEqual(fractions[-1], 1.0)

    def test_warning_exception_with_valid_wav_is_recovered(self):
        infer_body = """
        if self.gr_progress:
            self.gr_progress(0.9, desc="saving audio...")
        _write_wav(kwargs["output_path"])
        raise RuntimeError("past_key_values is deprecated but wav exists at /Users/aiden/private/out.wav")
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime, vendor, model = self._fake_runtime(tmp, infer_body)
            ref = tmp / "ref.wav"
            _write_wav(ref)
            out = tmp / "out.wav"
            code, events = _events_from_run(self._request_json(tmp, runtime, vendor, model, ref, out))
            self.assertTrue(out.exists())

        self.assertEqual(code, 0)
        result = next(event for event in events if event.get("type") == "result")
        self.assertTrue(result["meta"].get("warning_recovered"))

    def test_warning_exception_without_wav_is_failure_and_scrubbed(self):
        infer_body = """
        raise RuntimeError("past_key_values is deprecated; RuntimeError: bad shape at /Users/aiden/private/model.bin")
        """
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime, vendor, model = self._fake_runtime(tmp, infer_body)
            ref = tmp / "ref.wav"
            _write_wav(ref)
            out = tmp / "out.wav"
            code, events = _events_from_run(self._request_json(tmp, runtime, vendor, model, ref, out))

        self.assertNotEqual(code, 0)
        error = next(event for event in events if event.get("type") == "error")
        self.assertIn("bad shape", error.get("detail_redacted", ""))
        self.assertNotIn("/Users/aiden", error.get("detail_redacted", ""))

    def test_adapter_does_not_duplicate_wav_completion_progress(self):
        original = subprocess_adapter.check_runtime
        subprocess_adapter.check_runtime = lambda: _Status()

        class _Engine(subprocess_adapter.IndexTTS2SubprocessEngine):
            def _synthesize_once(self, request, status, output_wav, job_id, progress, is_cancelled, **_kwargs):
                _write_wav(output_wav)
                progress("合成完成", 1.0)
                return {"duration_seconds": 0.1, "meta": {}}

        try:
            with tempfile.TemporaryDirectory() as tmp_name:
                tmp = Path(tmp_name)
                ref = tmp / "ref.wav"
                _write_wav(ref)
                voice = VoiceProfile(id="v1", name="V", reference_audio_path=str(ref), reference_text="")
                request = SynthesisRequest(
                    text="短文本。",
                    voice=voice,
                    output_path=tmp / "out.wav",
                    engine_options={"use_persistent_worker": False},
                )
                progress_events: list[tuple[str, float]] = []
                _Engine().synthesize(request, progress=lambda msg, frac: progress_events.append((msg, frac)))
        finally:
            subprocess_adapter.check_runtime = original

        self.assertEqual(progress_events.count(("合成完成", 1.0)), 1)

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

    def test_persistent_worker_reuses_model_and_shutdowns(self):
        original = subprocess_adapter.check_runtime
        subprocess_adapter.shutdown_persistent_worker()
        self.addCleanup(subprocess_adapter.shutdown_persistent_worker)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            runtime, vendor, model, counter = self._fake_persistent_runtime(tmp)

            class _PersistentStatus(_Status):
                runtime_dir = runtime
                model_dir = model
                vendor_dir = vendor
                python_executable = Path(sys.executable)

                def snapshot(self) -> dict:
                    snap = super().snapshot()
                    snap.update({"runtime_dir": str(runtime), "model_dir": str(model)})
                    return snap

            subprocess_adapter.check_runtime = lambda: _PersistentStatus()
            ref = tmp / "ref.wav"
            _write_wav(ref)
            voice = VoiceProfile(id="v1", name="V", reference_audio_path=str(ref), reference_text="")
            engine = subprocess_adapter.IndexTTS2SubprocessEngine()
            progress_events: list[tuple[str, float]] = []

            try:
                result1 = engine.synthesize(
                    SynthesisRequest(text="第一句。", voice=voice, output_path=tmp / "out1.wav"),
                    progress=lambda msg, frac: progress_events.append((msg, frac)),
                )
                result2 = engine.synthesize(
                    SynthesisRequest(text="第二句。", voice=voice, output_path=tmp / "out2.wav"),
                    progress=lambda msg, frac: progress_events.append((msg, frac)),
                )

                self.assertTrue(result1.output_path.exists())
                self.assertTrue(result2.output_path.exists())
                self.assertEqual(counter.read_text(encoding="utf-8"), "1")
                self.assertTrue(subprocess_adapter.persistent_worker_status()["alive"])
                manager = subprocess_adapter._PERSISTENT_MANAGER  # type: ignore[attr-defined]
                manager._last_used = time.monotonic() - 999  # type: ignore[attr-defined]
                self.assertTrue(manager.reap_idle())
                self.assertFalse(subprocess_adapter.persistent_worker_status()["alive"])
                result3 = engine.synthesize(
                    SynthesisRequest(text="第三句。", voice=voice, output_path=tmp / "out3.wav"),
                    progress=lambda msg, frac: progress_events.append((msg, frac)),
                )
                self.assertTrue(result3.output_path.exists())
                self.assertEqual(counter.read_text(encoding="utf-8"), "2")
                subprocess_adapter.shutdown_persistent_worker()
                self.assertFalse(subprocess_adapter.persistent_worker_status()["alive"])
                self.assertTrue(any("后台已常驻" in msg or "复用 IndexTTS2 后台" in msg for msg, _ in progress_events))
            finally:
                subprocess_adapter.check_runtime = original


if __name__ == "__main__":
    unittest.main()
