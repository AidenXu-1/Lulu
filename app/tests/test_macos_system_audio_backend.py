from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core.recording import RecordingRequest, RecordingSource
from src.core.recording.macos_system_audio import (
    MacOSSystemAudioSession,
    RecordingBackendError,
)


class _FakeProcess:
    def __init__(self, returncode: int = 0, timeout: bool = False):
        self.returncode = returncode
        self.timeout = timeout
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.timeout = False

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.timeout = False


class MacOSSystemAudioSessionTests(unittest.TestCase):
    def _session(self, root: Path, process: _FakeProcess):
        output = root / "system.m4a"
        stop_file = root / "system.stop"
        request = RecordingRequest(source=RecordingSource.SYSTEM_AUDIO, output_path=output)
        return MacOSSystemAudioSession(request, process, stop_file, stdout_queue=__import__("queue").Queue())

    def test_silent_output_is_rejected_and_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = self._session(root, _FakeProcess())
            session.request.output_path.write_bytes(b"silent")
            with mock.patch(
                "src.core.recording.macos_system_audio._decode_to_wav",
                return_value=True,
            ), mock.patch(
                "src.core.recording.macos_system_audio._wav_stats",
                return_value={"duration": 2.0, "rms": 0.0, "peak": 0.0},
            ):
                with self.assertRaises(RecordingBackendError):
                    session.stop()

            self.assertFalse(session.request.output_path.exists())

    def test_missing_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp), _FakeProcess())
            with self.assertRaises(RecordingBackendError):
                session.stop()

    def test_cancel_cleans_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = _FakeProcess(returncode=None)
            session = self._session(root, process)
            session.request.output_path.write_bytes(b"partial")

            session.cancel()

            self.assertTrue(process.terminated)
            self.assertFalse(session.request.output_path.exists())

    def test_timeout_kills_process_and_cleans_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = _FakeProcess(returncode=None, timeout=True)
            session = self._session(root, process)
            session.request.output_path.write_bytes(b"partial")

            with self.assertRaises(RecordingBackendError):
                session.stop()

            self.assertTrue(process.terminated)
            self.assertFalse(session.request.output_path.exists())


if __name__ == "__main__":
    unittest.main()
