"""Local FFmpeg decoding only; no playback, credentials, network or user data."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
import wave
import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEventLoop, QTimer, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from src.ui.audio_waveform import AudioWaveform
from src.ui.voice_wave_slider import VoiceWaveSlider


class AudioWaveformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reader = AudioWaveform()

    def tearDown(self):
        self.reader.shutdown()
        self.reader.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 8
        while self.reader.state == 'loading' and time.monotonic() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(15, loop.quit)
            loop.exec()
        self.assertNotEqual(self.reader.state, 'loading')

    def wav(self, name, *, sound=True):
        # 30 seconds crosses the decoder's peak-bin reduction boundary.
        samples = np.zeros(8000 * 30, dtype='<i2')
        if sound:
            samples[8000 * 15:] = (np.sin(np.arange(8000 * 15) * .25) * 20000).astype('<i2')
        path = self.root / name
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(samples.tobytes())
        return path

    def test_full_file_peaks_preserve_silence_and_order_without_writing_source(self):
        path = self.wav('真实声音.wav')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.reader.load_file(path)
        self.wait()
        self.assertEqual(self.reader.state, 'ready')
        self.assertEqual(len(self.reader.peaks), 160)
        self.assertEqual(max(self.reader.peaks[:70]), 0)
        self.assertGreater(min(self.reader.peaks[90:]), .9)
        self.assertLessEqual(len(self.reader._raw_peaks), 4096)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_silence_and_same_path_replacement_are_not_fabricated_or_cached_stale(self):
        path = self.wav('替换.wav', sound=False)
        self.reader.load_file(path)
        self.wait()
        self.assertEqual(max(self.reader.peaks), 0)
        self.wav('替换.wav', sound=True)
        self.reader.load_file(path)
        self.wait()
        self.assertGreater(max(self.reader.peaks), .9)

    def test_missing_and_invalid_files_have_no_waveform(self):
        self.reader.load_file(self.root / 'missing.wav')
        self.assertEqual(self.reader.state, 'missing')
        path = self.root / 'broken.wav'
        path.write_bytes(b'broken')
        self.reader.load_file(path)
        self.wait()
        self.assertEqual(self.reader.state, 'failed')
        self.assertEqual(self.reader.peaks, [])

    def test_switch_file_cancels_old_decode_and_native_slider_uses_same_reader(self):
        self.reader.load_file(self.wav('old.wav'))
        previous = self.reader._process
        self.reader.load_file(self.wav('new.wav', sound=False))
        self.reader._read(previous)
        self.wait()
        self.assertEqual(max(self.reader.peaks), 0)
        slider = VoiceWaveSlider()
        try:
            self.assertIsInstance(slider.decoder, AudioWaveform)
            slider.decoder.peaks = [.25, 1]
            slider.decoder.state = 'ready'
            slider.decoder.changed.emit()
            self.assertEqual(slider.peaks, [.25, 1])
        finally:
            slider.stop_decode()
            slider.deleteLater()
