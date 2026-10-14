import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile


class UpdateZipCompressionTests(unittest.TestCase):
    def test_smaller_archive_preserves_tree_and_handles_highly_repetitive_files(self):
        from src.core.updater.archive import build_update_zip, preflight_zip, _records_from_entries
        spec = importlib.util.spec_from_file_location('zip_compressor', Path(__file__).parents[1]/'packaging/compress_update_zip.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); app = root/'Lulu.app'
            macos = app/'Contents/MacOS'; macos.mkdir(parents=True)
            main = macos/'Lulu'; main.write_bytes(b'executable-fixture'); main.chmod(0o755)
            import plistlib
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'Lulu'}))
            resources=app/'Contents/Resources'; resources.mkdir()
            (resources/'compressible.txt').write_bytes(b''.join((str(i%1700)+' text fixture\n').encode() for i in range(90000)))
            (resources/'zeros').write_bytes(b'\0'*(1024*1024))
            stored, compressed = root/'stored.zip', root/'compressed.zip'
            before, _ = build_update_zip(app, stored)
            module.compress_update_zip(stored, compressed)
            self.assertEqual(_records_from_entries(preflight_zip(compressed)), before)
            self.assertLess(compressed.stat().st_size, stored.stat().st_size)
            with zipfile.ZipFile(compressed) as archive:
                self.assertEqual(archive.getinfo('Lulu.app/Contents/Resources/zeros').compress_type, zipfile.ZIP_STORED)
            with self.assertRaises(RuntimeError):
                module.compress_update_zip(stored, compressed)
