import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.core.updater import service
from src.core.updater.constants import KEY_ID


class ProductionKeyPackagingTests(unittest.TestCase):
    """Run the real build script's key staging step in a disposable directory."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.key = Path(cls.temp.name) / 'public.pem'
        generated = subprocess.run(
            ['/usr/bin/openssl', 'genpkey', '-algorithm', 'RSA',
             '-pkeyopt', 'rsa_keygen_bits:3072'], capture_output=True, check=True)
        public = subprocess.run(['/usr/bin/openssl', 'pkey', '-pubout'],
                                input=generated.stdout, capture_output=True, check=True)
        cls.key.write_bytes(public.stdout)
        source = (Path(__file__).resolve().parents[1] / 'packaging/build_app.sh').read_text()
        cls.step = source[source.index('\nrm -f update_public_key.pem\n'):
                          source.index('echo "==> 3/5')]

    def stage(self, directory, flag, key):
        env = dict(os.environ, BUILD_IDENTITY='production', LULU_UPDATE_RELEASE=flag,
                   LULU_UPDATE_PUBLIC_KEY_PATH=str(key))
        return subprocess.run(['/bin/bash', '-eu', '-o', 'pipefail', '-c', self.step],
                              cwd=directory, env=env, capture_output=True, text=True)

    def test_local_production_requires_key_even_without_release_flag(self):
        for flag in ('0', '1'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp:
                result = self.stage(temp, flag, '')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('LULU_UPDATE_PUBLIC_KEY_PATH', result.stderr)

    def test_local_production_stages_valid_key_even_without_release_flag(self):
        for flag in ('0', '1'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temp:
                result = self.stage(temp, flag, self.key)
                self.assertEqual(result.returncode, 0, result.stderr)
                staged = Path(temp) / 'update_public_key.pem'
                self.assertTrue(staged.is_file())
                self.assertEqual(staged.read_bytes(), self.key.read_bytes())
                self.assertEqual(staged.stat().st_mode & 0o777, 0o600)

    def test_local_production_rejects_link_or_invalid_key(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'link.pem'; link.symlink_to(self.key)
            invalid = Path(temp) / 'invalid.pem'; invalid.write_text('damaged')
            for key in (link, invalid):
                with self.subTest(key=key.name):
                    result = self.stage(temp, '0', key)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((Path(temp) / 'update_public_key.pem').exists())








