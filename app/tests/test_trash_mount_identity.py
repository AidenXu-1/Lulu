"""Cross-mount trash recovery, entirely within temporary libraries."""
import json
import os
import unittest
from unittest.mock import patch

from tests import test_library_workspace_core as core_tests
from src.core import library, trash_cleanup


class TrashMountIdentityTests(unittest.TestCase):
    setUp = core_tests.WorkspaceCoreTests.setUp

    def deleted(self, *, external=False, legacy=False):
        eid = library.save_document('原稿', '挂载变化')
        if external:
            library.markdown_path(eid).write_text('外部修改', encoding='utf-8')
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            library.delete(eid)
        row = next(r for r in library.list_deleted() if r['id'] == eid)
        path = library.LIBRARY_DIR / '.trash' / (row['token'] + '.json')
        data = json.loads(path.read_text())
        old_device = data['json_identity'][0] + 100
        data['root_device'] = old_device
        for key in ('json_identity', 'markdown_identity'):
            if data[key]:
                data[key][0] = old_device
        if legacy:
            data.pop('volume_uuid'); data.pop('root_device')
        path.write_text(json.dumps(data), encoding='utf-8')
        return row, path, data

    def test_same_volume_new_mount_preview_and_restore_external_edits(self):
        row, path, _ = self.deleted(external=True)
        before = path.read_bytes()
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            self.assertEqual(library.deleted_preview(row['token'])['text'], '外部修改')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(library.restore_deleted(row['token']), row['id'])
        self.assertEqual(library.load_markdown(row['id']), '外部修改')

    def test_new_mount_purge_and_interrupted_purge_retry(self):
        row, path, _ = self.deleted()
        unlink = os.unlink
        count = []
        def fail_after_one(name, *args, **kwargs):
            if str(name).startswith('.delete-'):
                if count:
                    raise OSError('模拟中断')
                count.append(name)
            return unlink(name, *args, **kwargs)
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            with patch('os.unlink', side_effect=fail_after_one):
                with self.assertRaises(OSError):
                    trash_cleanup.purge(row['token'], True)
            data = json.loads(path.read_text())
            data['root_device'] += 50
            for key in ('json_identity', 'markdown_identity'):
                if data[key]: data[key][0] += 50
            for _, identity in data['purge_history']: identity[0] += 50
            path.write_text(json.dumps(data))
            trash_cleanup.purge(row['token'], True)
        self.assertEqual(library.list_deleted(), [])

    def test_legacy_cross_checks_preview_does_not_rewrite_receipt(self):
        row, path, _ = self.deleted(legacy=True)
        before = path.read_bytes()
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            self.assertEqual(library.deleted_preview(row['token'])['text'], '原稿')
            self.assertEqual(path.read_bytes(), before)
            library.restore_deleted(row['token'])
        self.assertEqual(library.load_markdown(row['id']), '原稿')

    def test_legacy_conflicting_contents_remain_untouched(self):
        row, path, data = self.deleted(external=True, legacy=True)
        before = path.read_bytes()
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            for action in (library.deleted_preview, library.restore_deleted,
                           lambda token: trash_cleanup.purge(token, True)):
                with self.assertRaises(ValueError): action(row['token'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((library.LIBRARY_DIR/'docs'/data['markdown']).read_text(), '外部修改')

    def test_uuid_mismatch_unavailable_or_mixed_volume_never_downgrades(self):
        row, path, data = self.deleted()
        current = library.LIBRARY_DIR.stat().st_dev
        for key in ('json_identity', 'markdown_identity'):
            if data[key]: data[key][0] = current
        data['root_device'] = current
        path.write_text(json.dumps(data))
        for volume in ('volume-b', ''):
            with patch.object(library, '_trash_volume_uuid', return_value=volume):
                for action in (library.deleted_preview, library.restore_deleted,
                               lambda token: trash_cleanup.purge(token, True)):
                    with self.assertRaises(ValueError): action(row['token'])
        data['markdown_identity'][0] += 1
        path.write_text(json.dumps(data))
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            with self.assertRaises(ValueError): library.deleted_preview(row['token'])

    def test_inode_replacement_and_symlink_still_rejected_after_mount_change(self):
        row, path, data = self.deleted()
        staged = library.LIBRARY_DIR / data['json']
        original = staged.read_bytes()
        staged.rename(staged.with_suffix('.saved'))
        staged.write_bytes(original)
        with patch.object(library, '_trash_volume_uuid', return_value='volume-a'):
            for action in (library.deleted_preview, library.restore_deleted,
                           lambda token: trash_cleanup.purge(token, True)):
                with self.assertRaises(ValueError): action(row['token'])
        self.assertTrue(path.exists())
