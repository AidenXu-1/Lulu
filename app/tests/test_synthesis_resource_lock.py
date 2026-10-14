from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.core.synthesis.resource_lock import voxcpm2_resource
from src.core.resource_control.contracts import ResourceControlError


class SynthesisResourceLockTests(unittest.TestCase):
    def test_competing_process_and_release_after_exit(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            code = "from pathlib import Path; from src.core.synthesis.resource_lock import voxcpm2_resource; import sys; c=voxcpm2_resource(Path(sys.argv[1])); c.__enter__(); print('locked', flush=True); sys.stdin.read()"
            child = subprocess.Popen([sys.executable, '-B', '-c', code, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaises(ResourceControlError):
                    with voxcpm2_resource(root):
                        self.fail('two workers hold the same resource')
                child.terminate()
                child.wait(timeout=5)
                with voxcpm2_resource(root):
                    pass
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
                child.stdin.close(); child.stdout.close()

    def test_symlink_lock_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root/'synthesis-locks').mkdir(mode=0o700)
            target = root/'target'; target.write_bytes(b'keep')
            (root/'synthesis-locks'/'local_voxcpm2.lock').symlink_to(target)
            with self.assertRaises(OSError):
                with voxcpm2_resource(root): pass
            self.assertEqual(target.read_bytes(), b'keep')

    def test_voice_list_does_not_create_a_library(self):
        from src.core.synthesis import voices
        with TemporaryDirectory() as folder:
            absent = Path(folder)/'absent'
            with patch.object(voices, 'VOICES_DIR', absent), patch.object(voices, 'INDEX', absent/'voices.json'):
                self.assertEqual(voices.list_voices(), [])
                self.assertFalse(absent.exists())
