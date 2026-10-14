import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from src.core.synthesis import indextts2_subprocess as adapter
from src.core.synthesis.base import SynthesisRequest, VoiceProfile
from src.ui import worker as ui_worker


class _Status:
    ready = True
    runtime_ready = True
    model_ready = True
    config_ready = True
    runtime_dir = Path("/tmp/lulu-runtime")
    model_dir = Path("/tmp/lulu-model")
    python_executable = Path(sys.executable)
    detail_redacted = ""

    def snapshot(self) -> dict:
        return {
            "ready": True,
            "runtime_ready": True,
            "model_ready": True,
            "config_ready": True,
        }


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 2205)


def _sleeping_process() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import sys,time; sys.stdin.readline(); time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        bufsize=1,
    )


def _request(tmp: Path, *, timeout_seconds: float = 0.1) -> SynthesisRequest:
    ref = tmp / "ref.wav"
    _write_wav(ref)
    return SynthesisRequest(
        text="测试文本",
        voice=VoiceProfile(
            id="voice-test",
            name="测试音色",
            reference_audio_path=str(ref),
            reference_text="参考文本",
        ),
        output_path=tmp / "out.wav",
        output_format="wav",
        engine_options={"timeout_seconds": timeout_seconds, "use_persistent_worker": False},
    )


class IndexTTS2P1PrivacyTimeoutTests(unittest.TestCase):
    def test_memory_guard_setup_failure_stops_one_shot_worker(self):
        proc = _sleeping_process()
        try:
            with tempfile.TemporaryDirectory() as tmp_name, \
                 patch.object(adapter, '_start_worker', return_value=proc), \
                 patch.object(adapter, '_ProcessMemoryMonitor', side_effect=RuntimeError('guard unavailable')):
                tmp = Path(tmp_name)
                with self.assertRaisesRegex(RuntimeError, 'guard unavailable'):
                    adapter.IndexTTS2SubprocessEngine()._synthesize_once(
                        _request(tmp), _Status(), tmp / 'out.wav', 'guard-init', lambda *_: None, lambda: False)
                self.assertIsNotNone(proc.poll())
        finally:
            adapter._terminate_process(proc)

    def test_memory_guard_setup_failure_drops_persistent_worker(self):
        manager = adapter.IndexTTS2PersistentWorkerManager()
        proc = _sleeping_process()
        try:
            with tempfile.TemporaryDirectory() as tmp_name, \
                 patch.object(adapter, '_start_persistent_worker', return_value=proc), \
                 patch.object(adapter, '_ProcessMemoryMonitor', side_effect=RuntimeError('guard unavailable')):
                tmp = Path(tmp_name)
                payload = adapter._build_payload(_request(tmp), _Status(), tmp / 'temp.wav', 'guard-init')
                with self.assertRaisesRegex(RuntimeError, 'guard unavailable'):
                    manager.generate(payload, _Status(), lambda *_: None, lambda: False, output_wav=tmp / 'out.wav')
                self.assertFalse(manager.status()['alive'])
                self.assertIsNotNone(proc.poll())
        finally:
            manager.shutdown()
            adapter._terminate_process(proc)

    def test_ui_worker_does_not_print_raw_exception_to_stderr(self):
        original_get_voice = ui_worker.get_voice
        original_engine = ui_worker.IndexTTS2SubprocessEngine

        class _Engine:
            def synthesize(self, *_args, **_kwargs):
                raise RuntimeError(
                    "input text: 我的私密正文 /Users/aiden/private/ref.wav "
                    "Authorization: Bearer sk-secret-123"
                )

        ui_worker.get_voice = lambda _voice_id: VoiceProfile(
            id="v",
            name="v",
            reference_audio_path="/tmp/ref.wav",
            reference_text="ref",
        )
        ui_worker.IndexTTS2SubprocessEngine = _Engine
        thread = ui_worker.IndexTTS2SynthesisWorker("我的私密正文", "v", "/tmp/out.wav", "natural")
        failures: list[str] = []
        thread.failed.connect(failures.append)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                thread.run()
        finally:
            ui_worker.get_voice = original_get_voice
            ui_worker.IndexTTS2SubprocessEngine = original_engine

        self.assertEqual(err.getvalue(), "")
        self.assertTrue(failures)
        self.assertNotIn("我的私密正文", failures[-1])
        self.assertNotIn("/Users/aiden", failures[-1])
        self.assertNotIn("sk-secret", failures[-1])

    def test_one_shot_worker_timeout_kills_silent_process(self):
        original_start_worker = adapter._start_worker
        adapter._start_worker = lambda *_args, **_kwargs: _sleeping_process()
        try:
            with tempfile.TemporaryDirectory() as tmp_name:
                tmp = Path(tmp_name)
                engine = adapter.IndexTTS2SubprocessEngine()
                with self.assertRaisesRegex(Exception, "生成超时"):
                    engine._synthesize_once(
                        _request(tmp),
                        _Status(),
                        tmp / "out.wav",
                        "job-timeout",
                        lambda _msg, _frac: None,
                        lambda: False,
                    )
        finally:
            adapter._start_worker = original_start_worker

    def test_persistent_worker_timeout_kills_silent_process_and_recovers(self):
        original_start_persistent = adapter._start_persistent_worker
        adapter._start_persistent_worker = lambda *_args, **_kwargs: _sleeping_process()
        manager = adapter.IndexTTS2PersistentWorkerManager(idle_timeout_seconds=600)
        try:
            with tempfile.TemporaryDirectory() as tmp_name:
                tmp = Path(tmp_name)
                request = _request(tmp, timeout_seconds=0.1)
                temp_wav = tmp / "temp.wav"
                payload = adapter._build_payload(request, _Status(), temp_wav, "job-persistent-timeout")
                with self.assertRaisesRegex(Exception, "生成超时"):
                    manager.generate(
                        payload,
                        _Status(),
                        lambda _msg, _frac: None,
                        lambda: False,
                        output_wav=tmp / "out.wav",
                    )
                self.assertFalse(manager.status()["alive"])
        finally:
            manager.shutdown()
            adapter._start_persistent_worker = original_start_persistent


if __name__ == "__main__":
    unittest.main()
