"""Archive boundaries: no broad pruning, no overwrite, retained bytes/links intact."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import sys
from unittest.mock import patch

_PATH = Path(__file__).resolve().parents[1] / 'packaging/tts_runtimes/slim_indextts25.py'
_SPEC = importlib.util.spec_from_file_location('slim_indextts25', _PATH)
slim = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slim)


class SlimTests(unittest.TestCase):
    def test_three_profiles_only_remove_reviewed_web_assets(self):
        spec = importlib.util.spec_from_file_location('slim_tts_runtime', _PATH.with_name('slim_tts_runtime.py'))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'slim_indextts25': slim}):
            spec.loader.exec_module(module)
        wasm = slim.SITE + 'gradio/templates/frontend/static/ffmpeg/ffmpeg-core.wasm'
        for profile in ('vox', 'qwen'):
            self.assertTrue(module.prune(profile, wasm))
            for name in ('gradio/routes.py', 'gradio/templates/frontend/static/ffmpeg_extra.js',
                         'pyarrow/libarrow.dylib', 'datasets/arrow_dataset.py', 'voxcpm/model/voxcpm2.py'):
                self.assertFalse(module.prune(profile, slim.SITE + name))
        self.assertFalse(module.prune('index2', wasm))
        members = [tarfile.TarInfo(name) for name in (
            'python/lib/sample.py', 'python/lib/__pycache__/sample.cpython-311.pyc',
            'python/lib/__pycache__/sample.cpython-311.opt-1.pyc',
            'python/lib/__pycache__/sourceless.cpython-311.pyc',
            'python/lib/sourceless.pyc')]
        self.assertEqual(module.source_backed_caches(members), {
            'python/lib/__pycache__/sample.cpython-311.pyc',
            'python/lib/__pycache__/sample.cpython-311.opt-1.pyc'})

    def test_output_cannot_escape_directory(self):
        with self.assertRaisesRegex(ValueError, 'basename'):
            slim.repack(Path('unused'), Path('unused'), filename='../outside.tar.gz')

    def test_production_assets_preserved(self):
        for name in ('numpy/testing/utils.py', 'scipy/special/tests/helper.py',
                     'unidic_lite/dicdir/sys.dic', 'torch/include/test.h',
                     'pip/__init__.py', 'numpy/tests_like/runtime.py',
                     'numpy-1.0.dist-info/LICENSE.txt'):
            with self.subTest(name=name):
                self.assertFalse(slim.omit(slim.SITE + name))
        self.assertFalse(slim.omit('vendor/index-tts/tests/asset'))
        self.assertTrue(slim.omit(slim.SITE + 'numpy/core/tests/data.bin'))

    def test_repack_keeps_content_metadata_and_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'source.tar.gz'
            with tarfile.open(src, 'w:gz') as archive:
                for name, data in [
                    ('python/bin/python3.11', b'interpreter'),
                    (slim.SITE + 'numpy/tests/example.py', b'test'),
                    (slim.SITE + 'numpy/testing/helper.py', b'helper'),
                    (slim.SITE + 'a-1.dist-info/METADATA', b'Name: a\nVersion: 1\n'),
                    (slim.SITE + 'vendor/_vendor/a-1.dist-info/METADATA', b'Name: a\nVersion: 0\n'),
                ]:
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o755
                    archive.addfile(member, io.BytesIO(data))
                link = tarfile.TarInfo('python/bin/python')
                link.type = tarfile.SYMTYPE
                link.linkname = 'python3.11'
                archive.addfile(link)
            original = src.read_bytes()
            with patch.object(slim, 'BASELINE_SHA256', slim.sha(src)):
                report = slim.repack(src, root / 'new')
                with self.assertRaises(FileExistsError):
                    slim.repack(src, root / 'new')
            self.assertEqual(src.read_bytes(), original)
            self.assertEqual(report['distribution_count'], 1)
            self.assertTrue(report['retained_content_verified'])
            self.assertEqual(report['removed_bytes'], 4)
            retained = json.loads((root / 'new/retained.json').read_text())
            self.assertEqual(retained['python/bin/python']['link'], 'python3.11')
            self.assertEqual(retained['python/bin/python3.11']['mode'], 0o755)

    def test_unreviewed_input_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'unknown'
            src.write_bytes(b'unknown')
            with self.assertRaisesRegex(ValueError, 'baseline'):
                slim.repack(src, root / 'out')
            self.assertFalse((root / 'out').exists())
