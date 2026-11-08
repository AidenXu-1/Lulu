"""Different device startup delays must not shift real speech in the final mix."""
import array
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import wave

from src.core.recording import audio_tools
from src.ui import recorder


class MixedRecordingAlignmentTests(unittest.TestCase):
    def test_both_recording_surfaces_forward_the_actual_capture_origins(self):
        from src.ui.main_window import MainWindow
        from src.ui.voice_page import VoicePage
        subject = SimpleNamespace(
            _ensure_recorder=lambda: SimpleNamespace(capture_started_monotonic=107.52),
            _mixed_system_result=SimpleNamespace(metadata={'capture_started_monotonic': 100}))
        paths = (Path('mic.m4a'), Path('system.m4a'), Path('out.m4a'))
        with patch('src.core.recording.mix_recording_tracks') as mix:
            MainWindow._mix_recording_tracks(subject, *paths)
            mix.assert_called_once_with(*paths, microphone_started_at=107.52, system_started_at=100)
            mix.reset_mock()
            VoicePage._mix_voice_recording_tracks(None, *paths,
                microphone_started_at=107.52, system_started_at=100)
            mix.assert_called_once_with(*paths, microphone_started_at=107.52, system_started_at=100)

    def test_delayed_device_pulses_align_on_the_shared_capture_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def track(name, seconds, pulse_at):
                path = root / name
                samples = array.array('h', [0]) * round(seconds * 16000)
                for index in range(round(pulse_at * 16000), round((pulse_at + .15) * 16000)):
                    samples[index] = round(3000 * math.sin(index * 2 * math.pi * 440 / 16000))
                with wave.open(str(path), 'wb') as stream:
                    stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                    stream.writeframes(samples.tobytes())
                return path
            early = track('early.wav', 8.52, 8.02)
            late = track('late.wav', 1, .5)
            for mic, system, mic_start, system_start in ((late, early, 107.52, 100), (early, late, 100, 107.52)):
                output = root / 'mixed.m4a'
                audio_tools.mix_recording_tracks(mic, system, output,
                    microphone_started_at=mic_start, system_started_at=system_start)
                decoded = subprocess.check_output([audio_tools._ffmpeg_exe(), '-v', 'error', '-i', str(output),
                    '-f', 's16le', '-ar', '16000', '-ac', '1', 'pipe:1'])
                samples = array.array('h', decoded)
                self.assertLess(max(map(abs, samples[:7*16000])), 100)
                self.assertGreater(max(map(abs, samples[8*16000:round(8.2*16000)])), 4500)
                self.assertGreater(len(samples), round(8.4*16000))

    def test_missing_or_invalid_capture_clock_never_silently_aligns_file_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'present.wav'; path.write_bytes(b'fixture')
            for invalid in (None, float('nan'), float('inf'), -1, True):
                with self.subTest(invalid=invalid), patch.object(audio_tools.subprocess, 'run') as run:
                    with self.assertRaises(audio_tools.RecordingAudioError):
                        audio_tools.mix_recording_tracks(path, path, path.with_suffix('.m4a'),
                            microphone_started_at=invalid, system_started_at=100)
                    run.assert_not_called()


class MicrophoneCaptureClockTests(unittest.TestCase):
    def setUp(self):
        self.native = MagicMock()
        original = recorder.QMediaRecorder
        for name in ('QMediaCaptureSession', 'QAudioInput', 'QMediaRecorder'):
            replacement = patch.object(recorder, name)
            mocked = replacement.start(); self.addCleanup(replacement.stop)
            if name == 'QMediaRecorder':
                mocked.return_value = self.native
                for enum in ('RecorderState', 'Quality', 'Error'):
                    setattr(mocked, enum, getattr(original, enum))
        self.subject = recorder.MicRecorder()
        self.subject.start('/tmp/timing-fixture-not-recorded.m4a')

    def test_media_duration_tracks_actual_capture_start_and_ignores_late_ui_delivery(self):
        with patch.object(recorder.time, 'monotonic', side_effect=[200.08, 201.05, 205.7]):
            for duration in (1000, 2000, 3000):
                self.subject._on_duration(duration)
        self.assertAlmostEqual(self.subject.capture_started_monotonic, 199.05)
        self.subject.start('/tmp/timing-second-fixture-not-recorded.m4a')
        self.assertIsNone(self.subject.capture_started_monotonic)
        self.subject._on_duration(0)
        self.assertIsNone(self.subject.capture_started_monotonic)

    def test_final_duration_uses_stop_request_time_instead_of_delayed_encoder_callback(self):
        with patch.object(recorder.time, 'monotonic', return_value=110):
            self.subject.stop()
        with patch.object(recorder.time, 'monotonic', return_value=115):
            self.subject._on_duration(2500)
        self.assertAlmostEqual(self.subject.capture_started_monotonic, 107.5)
