import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.core import pipeline


class TranscriptionTempCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        guard = patch.object(pipeline.tempfile, 'gettempdir', return_value=str(self.root))
        guard.start()
        self.addCleanup(guard.stop)

    def directory(self, name, owner=None):
        path = self.root / f'bzl_{name}'
        path.mkdir()
        (path / 'audio.wav').write_bytes(b'processing')
        if owner is not None:
            (path / '.owner-pid').write_text(str(owner))
        past = time.time() - 7200
        os.utime(path, (past, past))
        return path


    def test_live_owner_directory_is_kept(self):
        path = self.directory('live', os.getpid())
        self.assertEqual(pipeline.cleanup_stale_temp(), 0)
        self.assertTrue(path.exists())

    def test_confirmed_dead_owner_old_directory_is_removed(self):
        path = self.directory('dead', 12345)
        with patch.object(pipeline.os, 'kill', side_effect=ProcessLookupError) as probe:
            self.assertEqual(pipeline.cleanup_stale_temp(), 1)
            probe.assert_called_once_with(12345, 0)
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
