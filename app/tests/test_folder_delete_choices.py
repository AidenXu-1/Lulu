"""Folder deletion uses isolated materials and the existing recovery path."""
import unittest
from unittest.mock import patch

from tests.test_library_transactions import LibraryTransactionTests
from src.core import library
from src.core.agent_workspace.service import handle


class FolderDeleteChoicesTests(unittest.TestCase):
    def setUp(self):
        LibraryTransactionTests.setUp(self)
        self.folder = library.create_folder('transcript', '删除目标')
        self.first = library.save_document('正文一', '一', folder_id=self.folder)
        self.second = library.save_document('正文二', '二', folder_id=self.folder)
        self.other = library.save_document('不在文件夹里', '保留')

    def remove(self, **kwargs):
        return handle('delete_folder', dict(folder_id=self.folder, **kwargs))

    def test_both_choices_and_restore_without_dangling_folder(self):
        result = self.remove(delete_materials=True)
        self.assertEqual(set(result['removed_ids']), {self.first, self.second})
        self.assertEqual(result['failed'], [])
        self.assertEqual(library.load_markdown(self.other), '不在文件夹里')
        self.assertEqual(library.list_folders('transcript'), [])
        for receipt in library.list_deleted():
            handle('restore_deleted', {'token': receipt['token']})
            self.assertIsNone(library._load_raw(receipt['id'])['folder_id'])
        self.assertEqual(library.load_markdown(self.first), '正文一')
        self.assertEqual(library.load_markdown(self.second), '正文二')

    def test_omitted_choice_preserves_every_member(self):
        self.assertTrue(self.remove()['materials_preserved'])
        self.assertEqual(library.list_deleted(), [])
        self.assertIsNone(library._load_raw(self.first)['folder_id'])
        self.assertEqual(library.load_markdown(self.second), '正文二')

    def test_partial_failure_keeps_folder_and_retry_only_remaining(self):
        original = library.delete
        def fail_one(eid):
            if eid == self.second:
                raise OSError('磁盘暂时不可写')
            return original(eid)
        with patch.object(library, 'delete', side_effect=fail_one):
            result = self.remove(delete_materials=True)
        self.assertIsNone(result['deleted'])
        self.assertEqual(result['removed_ids'], [self.first])
        self.assertEqual(result['failed'][0]['id'], self.second)
        self.assertEqual(library._load_raw(self.second)['folder_id'], self.folder)
        self.assertEqual(library.list_folders('transcript')[0]['id'], self.folder)
        result = self.remove(delete_materials=True)
        self.assertEqual(result['removed_ids'], [self.second])
        self.assertEqual(len(library.list_deleted()), 2)

    def test_folder_write_failure_retains_recoverable_members_and_retry(self):
        with patch.object(library, '_save_folders', side_effect=OSError('磁盘暂时不可写')):
            result = self.remove(delete_materials=True)
        self.assertIsNone(result['deleted'])
        self.assertEqual(len(library.list_deleted()), 2)
        result = self.remove(delete_materials=True)
        self.assertEqual(result['removed_ids'], [])
        self.assertEqual(result['deleted'], self.folder)

    def test_invalid_choice_and_broken_record_do_not_delete_anything(self):
        for value in ('false', 1, None):
            with self.assertRaises(ValueError):
                self.remove(delete_materials=value)
        (library.LIBRARY_DIR / 'broken.json').write_text('{bad')
        with self.assertRaisesRegex(ValueError, '无法读取'):
            self.remove(delete_materials=True)
        self.assertEqual(library.list_deleted(), [])
        self.assertEqual(library.load_markdown(self.first), '正文一')

    def test_audio_record_recoverable_and_shared_original_untouched(self):
        folder = library.create_folder('voice', '音频')
        audio = library.LIBRARY_DIR / 'shared.wav'
        audio.write_bytes(b'audio fixture')
        def voice(target):
            return library.save_voice(title='声音', audio_path=audio, text='配音稿',
                                      engine='test', voice_name='test', config_summary='test', folder_id=target)
        removed, kept = voice(folder), voice(None)
        result = handle('delete_folder', {'folder_id': folder, 'delete_materials': True})
        self.assertEqual(result['removed_ids'], [removed])
        self.assertIsNotNone(library.load_voice(kept))
        self.assertEqual(audio.read_bytes(), b'audio fixture')
        receipt = library.list_deleted()[0]
        handle('restore_deleted', {'token': receipt['token']})
        self.assertIsNone(library.load_voice(removed)['folder_id'])
        self.assertEqual(audio.read_bytes(), b'audio fixture')
