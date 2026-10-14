from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.core import library


class AudioStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = patch.object(library, 'LIBRARY_DIR', self.root / 'library')
        guard.start()
        self.addCleanup(guard.stop)

    def test_managed_audio_and_preview_are_separate_from_downloads(self):
        audio = library.audio_storage_dir() / 'voice.wav'
        audio.write_bytes(b'original audio')
        preview = library.audio_storage_dir(preview=True) / 'preview.wav'
        preview.write_bytes(b'preview')
        entry = library.save_voice(title='配音', audio_path=audio, text='正文',
            engine='IndexTTS 2.5', voice_name='音色', config_summary='跟随音色')
        download = self.root / 'download'
        self.assertFalse(download.exists())
        self.assertEqual(Path(library._load_raw(entry)['audio_path']), audio)
        first = library.download_audio(audio, download, '配音.wav')
        second = library.download_audio(audio, download, '配音.wav')
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), audio.read_bytes())
        first.unlink()
        self.assertEqual(audio.read_bytes(), b'original audio')
        self.assertEqual(len(library.list_entries()), 1)

    def test_failed_download_preserves_source_and_existing_file(self):
        audio = library.audio_storage_dir() / 'voice.wav'
        audio.write_bytes(b'original')
        download = self.root / 'download'
        download.mkdir()
        old = download / 'voice.wav'
        old.write_bytes(b'old')
        with patch('shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                library.download_audio(audio, download, 'voice.wav')
        self.assertEqual(audio.read_bytes(), b'original')
        self.assertEqual(old.read_bytes(), b'old')
        self.assertEqual(list(download.iterdir()), [old])
