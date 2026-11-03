"""Owned temporary library and fake OS Trash only. Never invokes platform Trash."""
import json
import shutil
import unittest
from unittest.mock import patch
from tests import test_library_workspace_core as fixtures
from src.core import library, trash_cleanup
from src.core.agent_workspace.service import WorkspaceService


class ArchiveSystemTrashTests(unittest.TestCase):
    setUp = fixtures.WorkspaceCoreTests.setUp

    def fake_trash(self, path):
        target=self.root/'fake-system-trash'/path.name
        target.parent.mkdir(exist_ok=True)
        shutil.move(str(path),target)
        return str(target)

    def test_old_enabled_policy_never_expires_archive(self):
        eid=library.save_document('保留正文','长期资料');library.delete(eid)
        (library.LIBRARY_DIR/'.trash-policy.json').write_text('{"enabled":true,"days":7}')
        receipt=library.LIBRARY_DIR/'.trash'/(library.list_deleted()[0]['token']+'.json')
        data=json.loads(receipt.read_text());data['deleted_at']='2020-01-01T00:00:00';receipt.write_text(json.dumps(data))
        self.assertFalse(trash_cleanup.policy()['enabled'])
        self.assertFalse(json.loads((library.LIBRARY_DIR/'.trash-policy.json').read_text())['enabled'])
        with self.assertRaises(ValueError):trash_cleanup.policy(True)
        self.assertEqual(trash_cleanup.cleanup()['deleted'],[])
        library.restore_deleted(data['token']);self.assertEqual(library.load_markdown(eid),'保留正文')

    def test_active_delete_bundles_body_record_versions_and_preserves_others(self):
        eid=library.save_document('旧正文','标题');library.save_markdown(eid,'新正文')
        other=library.save_document('别的文稿','保留')
        with patch.object(trash_cleanup,'move_to_system_trash',side_effect=self.fake_trash) as move:
            result=WorkspaceService().system_delete(eid,True)
        self.assertTrue(result['system_trash']);self.assertEqual(move.call_count,1)
        bundle=next((self.root/'fake-system-trash').iterdir())
        self.assertEqual((bundle/'正文.md').read_text(),'新正文')
        self.assertEqual(json.loads((bundle/'record.json').read_text())['id'],eid)
        self.assertTrue(list((bundle/'修改历史').glob('*.md')))
        self.assertEqual(library.list_deleted(),[])
        self.assertEqual(library.load_markdown(other),'别的文稿')

    def test_platform_failure_restores_active_document_without_loss(self):
        eid=library.save_document('原文','标题')
        with patch.object(trash_cleanup,'move_to_system_trash',side_effect=OSError('拒绝移动')):
            with self.assertRaises(OSError):WorkspaceService().system_delete(eid,True)
        self.assertEqual(library.load_markdown(eid),'原文');self.assertEqual(library.list_deleted(),[])

    def test_archived_delete_failure_remains_restorable(self):
        eid=library.save_document('原文','标题');library.delete(eid);token=library.list_deleted()[0]['token']
        with patch.object(trash_cleanup,'move_to_system_trash',side_effect=OSError('磁盘断开')):
            with self.assertRaises(ValueError):WorkspaceService().purge_deleted(token,True)
        library.restore_deleted(token);self.assertEqual(library.load_markdown(eid),'原文')

    def test_original_audio_is_never_followed_or_removed(self):
        audio=self.root/'original.wav';audio.write_bytes(b'original audio')
        eid=library.save_voice(title='配音',audio_path=audio,text='文字',engine='test',voice_name='音色',config_summary='test')
        with patch.object(trash_cleanup,'move_to_system_trash',side_effect=self.fake_trash):WorkspaceService().system_delete(eid,True)
        self.assertEqual(audio.read_bytes(),b'original audio')

    def test_symlink_body_rejected_before_os_handoff(self):
        eid=library.save_document('原文','标题');library.delete(eid)
        token=library.list_deleted()[0]['token'];receipt=library.LIBRARY_DIR/'.trash'/(token+'.json')
        data=json.loads(receipt.read_text());body=library.LIBRARY_DIR/library.DOCS_DIR_NAME/data['markdown']
        outside=self.root/'outside.md';outside.write_text('不可移动');body.unlink();body.symlink_to(outside)
        with patch.object(trash_cleanup,'move_to_system_trash') as move:
            with self.assertRaises(ValueError):trash_cleanup.system_trash(token,True)
        move.assert_not_called();self.assertEqual(outside.read_text(),'不可移动')

    def test_interrupted_staging_recovers_all_material_without_os_handoff(self):
        eid=library.save_document('原文','标题');library.delete(eid);token=library.list_deleted()[0]['token']
        rename=__import__('os').rename;calls=[]
        def interrupt(source,target):
            rename(source,target);calls.append(str(source))
            if len(calls)==1:raise KeyboardInterrupt('模拟进程中断')
        with patch.object(trash_cleanup,'move_to_system_trash') as move:
            with patch('os.rename',side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):trash_cleanup.system_trash(token,True)
        move.assert_not_called()
        self.assertEqual(library.list_deleted()[0]['token'],token)
        library.restore_deleted(token);self.assertEqual(library.load_markdown(eid),'原文')

    def test_confirmation_required_before_archiving_or_os_handoff(self):
        eid=library.save_document('原文','标题')
        with patch.object(trash_cleanup,'move_to_system_trash') as move:
            with self.assertRaises(ValueError):WorkspaceService().system_delete(eid)
        move.assert_not_called();self.assertEqual(library.load_markdown(eid),'原文')

    def test_interrupted_delete_with_restore_conflict_stays_visible_and_never_overwrites(self):
        eid=library.save_document('原文','标题');library.delete(eid);row=library.list_deleted()[0]
        receipt=library.LIBRARY_DIR/'.trash'/(row['token']+'.json');data=json.loads(receipt.read_text())
        source=library.LIBRARY_DIR/data['json'];rename=__import__('os').rename
        def interrupt(old,new):
            rename(old,new);raise KeyboardInterrupt('模拟进程中断')
        with patch('os.rename',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):trash_cleanup.system_trash(row['token'],True)
        source.write_text('原位置新文件，不能覆盖')
        self.assertEqual([r['token'] for r in library.list_deleted()],[row['token']])
        self.assertEqual(source.read_text(),'原位置新文件，不能覆盖')
        source.unlink();library.restore_deleted(row['token']);self.assertEqual(library.load_markdown(eid),'原文')
