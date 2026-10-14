import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.core.updater import service
from src.core.updater.constants import KEY_ID


class BundledKeyTests(unittest.TestCase):
    def test_pyinstaller_resources_key_is_loaded_without_following_data_link(self):
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw).resolve() / 'Lulu.app'
            contents = app / 'Contents'
            resources = contents / 'Resources'
            frameworks = contents / 'Frameworks'
            macos = contents / 'MacOS'
            for path in (resources, frameworks, macos):
                path.mkdir(parents=True, exist_ok=True)
            (macos / 'Lulu').touch()
            pem = '-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----\n'
            (resources / 'update_public_key.pem').write_text(pem)
            (frameworks / 'update_public_key.pem').symlink_to('../Resources/update_public_key.pem')
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(frameworks), create=True), patch.object(sys, 'executable', str(macos / 'Lulu')), patch.object(service, '__file__', str(frameworks / 'src/core/updater/service.py')):
                self.assertEqual(service._default_public_keys(), {KEY_ID: pem})
                (resources / 'update_public_keys.json').write_text(json.dumps({'rotated': pem}))
                self.assertEqual(service._default_public_keys(), {'rotated': pem})
                (resources / 'update_public_keys.json').unlink()
                (resources / 'update_public_key.pem').write_text('damaged')
                self.assertEqual(service._default_public_keys(), {})
                (resources / 'update_public_key.pem').unlink()
                self.assertEqual(service._default_public_keys(), {})

    def test_bundle_resources_and_key_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            contents = root / 'Lulu.app/Contents'
            resources, frameworks, macos = [contents / name for name in ('Resources', 'Frameworks', 'MacOS')]
            for path in (resources, frameworks, macos):
                path.mkdir(parents=True, exist_ok=True)
            (macos / 'Lulu').touch()
            outside = root / 'outside'; outside.mkdir()
            (outside / 'update_public_key.pem').write_text('-----BEGIN PUBLIC KEY-----\ntest')
            (resources / 'update_public_key.pem').symlink_to(outside / 'update_public_key.pem')
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(frameworks), create=True), patch.object(sys, 'executable', str(macos / 'Lulu')), patch.object(service, '__file__', str(frameworks / 'src/core/updater/service.py')):
                self.assertEqual(service._default_public_keys(), {})
                (resources / 'update_public_key.pem').unlink(); resources.rmdir(); resources.symlink_to(outside)
                self.assertEqual(service._default_public_keys(), {})

    def test_unrelated_meipass_cannot_select_sibling_resources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); frameworks = root / 'Frameworks'; frameworks.mkdir()
            resources = root / 'Resources'; resources.mkdir()
            (resources / 'update_public_key.pem').write_text('-----BEGIN PUBLIC KEY-----\ntest')
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(frameworks), create=True), patch.object(sys, 'executable', '/different/Lulu.app/Contents/MacOS/Lulu'), patch.object(service, '__file__', str(frameworks / 'src/core/updater/service.py')):
                self.assertEqual(service._default_public_keys(), {})
