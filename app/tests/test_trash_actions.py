"""Trash disposal and selection use temporary libraries, never the installed App."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests import test_library_workspace_core as core_tests
from src.core import library, trash_cleanup


class TrashDisposalTests(unittest.TestCase):
    setUp = core_tests.WorkspaceCoreTests.setUp

    def deleted(self, title='文稿'):
        eid = library.save_document('第一版', title)
        library.save_markdown(eid, '第二版')
        library.delete(eid)
        return next(r for r in library.list_deleted() if r['id'] == eid)

    def date(self, row, value):
        p = library.LIBRARY_DIR / '.trash' / (row['token'] + '.json')
        data = json.loads(p.read_text()); data['deleted_at'] = value
        p.write_text(json.dumps(data)); return p

    def test_confirmed_purge_removes_body_history_and_keeps_other_documents(self):
        row = self.deleted(); other = library.save_document('保留', '其他')
        with self.assertRaises(ValueError): trash_cleanup.purge(row['token'])
        self.assertEqual(len(library.list_deleted()), 1)
        trash_cleanup.purge(row['token'], True)
        self.assertEqual(library.list_deleted(), [])
        self.assertEqual(list((library.LIBRARY_DIR/'.versions'/row['id']).glob('*.md')), [])
        self.assertFalse(list(library.LIBRARY_DIR.rglob('.delete-*')))
        self.assertEqual(library.load_markdown(other), '保留')

    def test_voice_original_is_preserved(self):
        audio=self.root/'voice.wav'; audio.write_bytes(b'original audio')
        eid=library.save_voice(title='配音',audio_path=audio,text='文字',engine='test',voice_name='音色',config_summary='test')
        library.delete(eid); trash_cleanup.purge(library.list_deleted()[0]['token'],True)
        self.assertEqual(audio.read_bytes(), b'original audio')

    def test_disabled_and_exact_seven_day_boundary_and_bad_dates(self):
        now=datetime.now(timezone.utc); old=self.deleted('到期'); new=self.deleted('未到期'); invalid=self.deleted('异常')
        self.date(old,(now-timedelta(days=7)).isoformat())
        self.date(new,(now-timedelta(days=7)+timedelta(seconds=1)).isoformat())
        self.date(invalid,'bad date')
        self.assertFalse(trash_cleanup.policy()['enabled'])
        self.assertEqual(trash_cleanup.cleanup(now)['deleted'], [])
        trash_cleanup.policy(True)
        result=trash_cleanup.cleanup(now)
        self.assertEqual(result['deleted'],[old['token']]); self.assertEqual(len(result['failures']),1)
        trash_cleanup.policy(False)
        self.assertEqual(trash_cleanup.cleanup(now+timedelta(days=30))['deleted'], [])
        self.assertEqual(len(library.list_deleted()),2)

    def test_preflight_rejects_symlink_and_preserves_all_targets(self):
        row=self.deleted(); receipt=library.LIBRARY_DIR/'.trash'/(row['token']+'.json')
        data=json.loads(receipt.read_text()); md=library.LIBRARY_DIR/'docs'/data['markdown']
        outside=self.root/'outside.md'; outside.write_text('不可删除')
        md.unlink(); md.symlink_to(outside)
        with self.assertRaises(ValueError):trash_cleanup.purge(row['token'],True)
        self.assertTrue((library.LIBRARY_DIR/data['json']).exists())
        self.assertEqual(outside.read_text(),'不可删除')
        self.assertFalse(json.loads(receipt.read_text()).get('purging'))

    def test_restore_conflict_and_unknown_history_preserve_files(self):
        row=self.deleted(); target=library.LIBRARY_DIR/(row['id']+'.json'); target.write_text('新的文件')
        with self.assertRaises(ValueError):trash_cleanup.purge(row['token'],True)
        target.unlink()
        (library.LIBRARY_DIR/'.versions'/row['id']/'unknown.txt').write_text('keep')
        with self.assertRaises(ValueError):trash_cleanup.purge(row['token'],True)
        self.assertEqual(len(library.list_deleted()),1)

    def test_interrupted_purge_stays_visible_and_can_retry(self):
        row=self.deleted(); unlink=os.unlink; deleted=[]
        def fail_after_one(path,*args,**kwargs):
            if str(path).startswith('.delete-'):
                if deleted: raise OSError('模拟磁盘错误')
                deleted.append(str(path))
            return unlink(path,*args,**kwargs)
        with patch('os.unlink',side_effect=fail_after_one):
            with self.assertRaises(OSError):trash_cleanup.purge(row['token'],True)
        self.assertEqual(library.list_deleted()[0]['token'],row['token'])
        with self.assertRaises(ValueError):library.restore_deleted(row['token'])
        trash_cleanup.purge(row['token'],True)
        self.assertEqual(library.list_deleted(),[])

    def test_receipt_traversal_and_hardlink_are_rejected(self):
        row=self.deleted(); receipt=library.LIBRARY_DIR/'.trash'/(row['token']+'.json')
        original=receipt.read_text(); data=json.loads(original); data['id']='../outside'
        receipt.write_text(json.dumps(data))
        with self.assertRaises(ValueError):trash_cleanup.purge(row['token'],True)
        receipt.write_text(original); data=json.loads(original)
        source=library.LIBRARY_DIR/data['json']; os.link(source,self.root/'linked.json')
        with self.assertRaises(ValueError):trash_cleanup.purge(row['token'],True)
        self.assertTrue(source.exists())

    def test_invalid_policy_never_cleans(self):
        row=self.deleted(); self.date(row,'2020-01-01T00:00:00')
        path=library.LIBRARY_DIR/'.trash-policy.json';path.write_text('{"enabled":"true","days":7}')
        with self.assertRaises(ValueError):trash_cleanup.cleanup()
        self.assertEqual(len(library.list_deleted()),1)
