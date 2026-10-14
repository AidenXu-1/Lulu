import subprocess
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.synthesis.indextts2_results import unique_output_path
from src.core.synthesis import voxcpm2_subprocess
from src.ui import collection_page as collection_page_module
from src.ui import voice_page as voice_page_module
from src.ui.collection_page import CollectionPage, _LoginWorker
from src.ui.voice_page import VoicePage


class _RunningWorker:
    def isRunning(self):
        return True


class _LifecycleWorker:
    def __init__(self):
        self.cancelled = False
        self.interrupted = False
        self.waited = []

    def isRunning(self):
        return True

    def request_cancel(self):
        self.cancelled = True

    def requestInterruption(self):
        self.interrupted = True

    def wait(self, timeout):
        self.waited.append(timeout)
        return True


class ReleaseBlockerTests(unittest.TestCase):
    def test_voxcpm2_forced_kill_waits_for_child_reap(self):
        class StubbornProcess:
            pid = 43211
            returncode = None

            def __init__(self):
                self.waits = []

            def poll(self):
                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout):
                self.waits.append(timeout)
                if len(self.waits) == 1:
                    raise subprocess.TimeoutExpired("fixture", timeout)
                self.returncode = -9
                return self.returncode

        process = StubbornProcess()
        with patch("os.killpg"):
            voxcpm2_subprocess._terminate(process)

        self.assertEqual(process.waits, [5, 5])
        self.assertEqual(process.returncode, -9)

    def test_voxcpm2_worker_is_in_packaging_datas(self):
        spec = Path(__file__).parents[1] / "packaging" / "Lulu.spec"
        text = spec.read_text(encoding="utf-8")
        self.assertIn('"voxcpm2_worker.py"', text)

    def test_cross_engine_busy_gate_detects_other_local_engine(self):
        fake = SimpleNamespace(
            _worker=None,
            _indextts2_worker=_RunningWorker(),
            _index_name="IndexTTS 2.5",
        )
        self.assertEqual(
            VoicePage._local_synthesis_busy_label(fake, exclude="voxcpm2"),
            "IndexTTS 2.5",
        )
        fake._index_name = "IndexTTS 2"
        self.assertEqual(VoicePage._local_synthesis_busy_label(fake, exclude="voxcpm2"), "IndexTTS 2")

    def test_second_local_engine_click_fails_closed(self):
        old_alert = voice_page_module.dialogs.alert
        alerts = []
        try:
            voice_page_module.dialogs.alert = lambda *_args: alerts.append(_args)  # type: ignore[assignment]
            fake = SimpleNamespace(
                _worker=_RunningWorker(),
                _indextts2_worker=None,
                _indextts2_release_worker=None,
                _index_name="IndexTTS 2.5",
            )
            fake._local_synthesis_busy_label = lambda exclude="": VoicePage._local_synthesis_busy_label(fake, exclude=exclude)
            fake._guard_local_synthesis_start = lambda engine: VoicePage._guard_local_synthesis_start(fake, engine)

            self.assertFalse(VoicePage._guard_local_synthesis_start(fake, "indextts2"))

            self.assertEqual(len(alerts), 1)
            self.assertIn("VoxCPM2 正在生成", alerts[0][2])
            self.assertIsNone(fake._indextts2_worker)
        finally:
            voice_page_module.dialogs.alert = old_alert  # type: ignore[assignment]

    def test_same_second_output_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "Lulu配音_20260711_120000.wav"
            first.write_bytes(b"first")
            second = unique_output_path(first)
            self.assertEqual(second.name, "Lulu配音_20260711_120000_2.wav")
            second.write_bytes(b"second")
            self.assertEqual(first.read_bytes(), b"first")

    def test_library_save_failure_keeps_generated_audio_discoverable(self):
        old_save = voice_page_module.library.save_voice
        try:
            voice_page_module.library.save_voice = lambda **_kwargs: (_ for _ in ()).throw(OSError("read only"))  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "voice.wav"
                output.write_bytes(b"audio")
                ok, message = VoicePage._save_voice_safely(object(), output, title="test")
                self.assertFalse(ok)
                self.assertTrue(output.exists())
                self.assertIn(str(output.resolve()), message)
                self.assertIn("音频已生成", message)
        finally:
            voice_page_module.library.save_voice = old_save  # type: ignore[assignment]

    def test_collection_shutdown_cancels_and_waits_all_page_workers(self):
        workers = [_LifecycleWorker() for _ in range(4)]
        fake = SimpleNamespace(
            _worker=workers[0],
            _full_worker=workers[1],
            _login_worker=workers[2],
            _bitable_worker=workers[3],
        )

        CollectionPage.shutdown_workers(fake, timeout_ms=500)

        for worker in workers:
            self.assertTrue(worker.cancelled)
            self.assertTrue(worker.interrupted)
            self.assertEqual(worker.waited, [500])
        self.assertIsNone(fake._worker)
        self.assertIsNone(fake._full_worker)
        self.assertIsNone(fake._login_worker)
        self.assertIsNone(fake._bitable_worker)

    def test_collection_shutdown_stops_real_running_login_thread(self):
        old_login = collection_page_module.login
        started = threading.Event()
        try:
            def _cooperative_login(_browser_path, progress, is_cancelled):
                started.set()
                while not is_cancelled():
                    time.sleep(0.005)
                return False

            collection_page_module.login = _cooperative_login  # type: ignore[assignment]
            worker = _LoginWorker(None)
            worker.start()
            self.assertTrue(started.wait(1.0))
            fake = SimpleNamespace(
                _worker=None,
                _full_worker=None,
                _login_worker=worker,
                _bitable_worker=None,
            )

            CollectionPage.shutdown_workers(fake, timeout_ms=1000)

            self.assertFalse(worker.isRunning())
            self.assertIsNone(fake._login_worker)
        finally:
            collection_page_module.login = old_login  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
