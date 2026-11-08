"""Long recording statistics must use bounded reads and retain exact levels."""
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave
from src.core.recording import audio_tools, macos_system_audio


class RecordingAudioBoundsTests(unittest.TestCase):
    def test_both_recording_paths_stream_long_audio_with_exact_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'two-minutes.wav'
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(48000)
                block = struct.pack('<hhhh', -1000, 1000, -3000, 3000) * 12000
                for _ in range(120): output.writeframesraw(block)
            original = wave.open
            def bounded_open(*args, **kwargs):
                reader = original(*args, **kwargs)
                read = reader.readframes
                def bounded_read(count):
                    self.assertLessEqual(count, 65536, 'Whole recording must never be read into memory')
                    return read(count)
                reader.readframes = bounded_read
                return reader
            with patch.object(wave, 'open', side_effect=bounded_open):
                for module in (audio_tools, macos_system_audio):
                    stats = module._wav_stats(path)
                    self.assertEqual(stats['duration'], 120)
                    self.assertEqual(stats['sample_rate'], 48000)
                    self.assertEqual(stats['peak'], 3000)
                    self.assertAlmostEqual(stats['rms'], math.sqrt(5000000))
