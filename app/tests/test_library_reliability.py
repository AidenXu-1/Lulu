from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core import library


class LibraryReliabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in [('LIBRARY_DIR', self.root), ('FOLDERS_FILE', self.root / 'folders.json')]:
            guard = patch.object(library, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.entry = library.save_document('上次完整正文', '正常文稿')
        self.body = library.markdown_path(self.entry)

    def test_partial_write_failure_preserves_complete_previous_body(self):
        real_temporary = tempfile.NamedTemporaryFile

        @contextmanager
        def interrupted(*args, **kwargs):
            with real_temporary(*args, **kwargs) as handle:
                real_write = handle.write
                def partial(content):
                    real_write(content[:2])
                    raise OSError('模拟磁盘写满')
                with patch.object(handle, 'write', side_effect=partial):
                    yield handle

        with patch.object(library.tempfile, 'NamedTemporaryFile', interrupted):
            with self.assertRaises(OSError):
                library.save_markdown(self.entry, '新正文必须完整保存')
        self.assertEqual(self.body.read_text(), '上次完整正文')
        self.assertEqual(library.load_markdown(self.entry), '上次完整正文')
        self.assertEqual(list(self.body.parent.glob('*.tmp')), [])

    def test_replace_failure_preserves_previous_body(self):
        replace = Path.replace
        def fail(source, target):
            if Path(target) == self.body:
                raise OSError('模拟替换失败')
            return replace(source, target)
        with patch.object(Path, 'replace', fail):
            with self.assertRaises(OSError):
                library.save_markdown(self.entry, '新正文')
        self.assertEqual(library.load_markdown(self.entry), '上次完整正文')


    def test_bad_entry_shape_is_isolated_and_original_file_is_preserved(self):
        for content in ('[]', 'null', '{"meta": []}', '{"text": []}'):
            with self.subTest(content=content):
                bad = self.root / 'damaged.json'
                bad.write_text(content)
                errors = []
                entries = library.list_entries(errors=errors)
                self.assertIn(self.entry, [e['id'] for e in entries])
                self.assertEqual(errors, ['damaged.json'])
                self.assertEqual(bad.read_text(), content)
                self.assertIsNone(library.load('damaged'))
                library.list_folders('transcript')

    def test_invalid_utf8_does_not_hide_healthy_entries_or_overwrite_backup(self):
        damaged = library.save_document('可恢复的索引正文', '乱码文稿')
        body = library.markdown_path(damaged)
        body.write_bytes(b'\xff\xfeinvalid')
        index = self.root / f'{damaged}.json'
        before = index.read_bytes()
        errors = []
        self.assertEqual([e['id'] for e in library.list_entries(errors=errors)], [self.entry])
        self.assertEqual(errors, [f'{damaged}.json'])
        self.assertEqual(body.read_bytes(), b'\xff\xfeinvalid')
        self.assertEqual(index.read_bytes(), before)
        self.assertIsNone(library.load(damaged))


if __name__ == '__main__':
    unittest.main()
