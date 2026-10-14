import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_agent_workspace
from src.core import library
from src.core.agent_workspace import recovery, documents
from src.core.agent_workspace.service import WorkspaceService
from src.core.agent_workspace.store import WorkspaceStore
from src.core.job_ledger.contracts import JobState


class RecoveryTests(unittest.TestCase):
    setUp=test_agent_workspace.AgentWorkspaceTests.setUp

    def fixture(self):
        self.chat=self.service.new_conversation(self.eid,'recover-chat')
        self.service.store.append(self.chat['id'],'user','需要保留的要求')
        self.service.save_draft(self.chat['id'],{'text':'尚未发送的草稿'},0)
        return self.service.backup()

    def test_verified_snapshot_recovers_corruption_without_rolling_back_manuscript(self):
        backup=self.fixture()
        doc=documents.snapshot(self.eid)
        self.service.save(self.eid,'备份之后的新正文',doc['title'],doc['revision'],doc['content_hash'],'new-doc')
        damaged=b'not a sqlite database'
        recovery.database_path().write_bytes(damaged)
        with self.assertRaises(RuntimeError):WorkspaceStore()
        service=WorkspaceService()
        state=service.storage_status()
        self.assertEqual(state['state'],'unavailable')
        result=service.restore_backup(backup['id'],state['fingerprint'],backup['sha256'])
        self.assertEqual(service.store.get(self.chat['id'])['draft']['text'],'尚未发送的草稿')
        self.assertEqual(documents.snapshot(self.eid)['text'],'备份之后的新正文')
        self.assertEqual((recovery.workspace_root()/result['preserved_as']/'conversations.sqlite3').read_bytes(),damaged)
        self.assertEqual(service.store.messages(self.chat['id'])[0]['content'],'需要保留的要求')

    def test_missing_and_zeroed_databases_do_not_silently_become_empty(self):
        backup=self.fixture()
        recovery.database_path().unlink()
        with self.assertRaisesRegex(RuntimeError,'未创建空库'):WorkspaceStore()
        state=self.service.storage_status()
        self.service.restore_backup(backup['id'],state['fingerprint'],backup['sha256'])
        recovery.database_path().write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError,'未重建空库'):WorkspaceStore()

    def test_restore_refuses_changes_after_preview_and_modified_backups(self):
        backup=self.fixture()
        state=self.service.storage_status()
        self.service.store.append(self.chat['id'],'user','刚刚又写了一句')
        with self.assertRaises(library.RevisionConflict):
            self.service.restore_backup(backup['id'],state['fingerprint'],backup['sha256'])
        state=self.service.storage_status()
        (recovery.folder()/(backup['id']+'.sqlite3')).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.service.restore_backup(backup['id'],state['fingerprint'],backup['sha256'])
        self.assertEqual(self.service.store.messages(self.chat['id'])[-1]['content'],'刚刚又写了一句')

    def test_restoring_an_older_healthy_snapshot_can_be_undone_from_same_api(self):
        older=self.fixture()
        self.service.store.append(self.chat['id'],'user','较新的对话')
        result=self.service.restore_backup(older['id'],recovery.fingerprint(),older['sha256'])
        rollback=recovery._entry(result['rollback_backup_id'])
        self.service.restore_backup(rollback['id'],recovery.fingerprint(),rollback['sha256'])
        self.assertEqual(self.service.store.messages(self.chat['id'])[-1]['content'],'较新的对话')

    def test_auto_slots_rotate_but_manual_backups_and_damaged_evidence_remain(self):
        manual=self.fixture()
        for n in range(5):
            self.service.store.append(self.chat['id'],'user',str(n))
            recovery.backup(automatic=True)
        entries=recovery.status()['backups']
        self.assertEqual(sum(b.get('automatic',False) for b in entries),3)
        self.assertEqual(recovery._entry(manual['id'])['sha256'],manual['sha256'])
        old=recovery.folder()/'auto-0.sqlite3'
        old.write_bytes(b'keep-damaged-backup')
        with self.assertRaises(ValueError):recovery.backup(automatic=True)
        self.assertEqual(old.read_bytes(),b'keep-damaged-backup')

    def test_backup_failure_does_not_misreport_successful_user_save(self):
        self.fixture()
        recovery._attempted.clear()
        with patch.object(recovery,'backup',side_effect=OSError('disk full')),patch.object(recovery.time,'time',return_value=10**12):
            self.service.store.append(self.chat['id'],'user','必须已经保存')
        self.assertEqual(self.service.store.messages(self.chat['id'])[-1]['content'],'必须已经保存')
        self.assertIn('备份未完成',self.service.storage_status()['warning'])

    def test_restore_refuses_active_generation(self):
        backup=self.fixture()
        runtime=SimpleNamespace(_authorized_job_control=lambda _:SimpleNamespace(list=lambda:[
            SimpleNamespace(job_kind='workspace_conversation',state=JobState.RUNNING)]))
        service=WorkspaceService(runtime=runtime)
        with self.assertRaisesRegex(RuntimeError,'正在运行'):
            service.restore_backup(backup['id'],service.storage_status()['fingerprint'],backup['sha256'])

    def test_restore_finishes_after_each_interrupted_replace_without_losing_original(self):
        backup=self.fixture()
        real_replace=recovery.os.replace
        # Includes archive receipt, pending intent, DB replacement and marker.
        for fail_at in range(1,5):
            with self.subTest(fail_at=fail_at):
                recovery.database_path().write_bytes(b'broken-'+str(fail_at).encode())
                damaged=recovery.database_path().read_bytes()
                stamp=recovery.fingerprint()
                calls=[]
                def fail(source,destination):
                    calls.append((source,destination))
                    if len(calls)==fail_at:raise OSError('interrupted restore')
                    return real_replace(source,destination)
                with patch.object(recovery.os,'replace',side_effect=fail):
                    with self.assertRaises(OSError):self.service.restore_backup(backup['id'],stamp,backup['sha256'])
                if (recovery.workspace_root()/'restore-pending.json').exists():
                    recovery.finish_pending_restore()
                else:
                    self.assertEqual(recovery.database_path().read_bytes(),damaged)
                    self.service.restore_backup(backup['id'],recovery.fingerprint(),backup['sha256'])
                self.assertEqual(WorkspaceStore().messages(self.chat['id'])[0]['content'],'需要保留的要求')
                preserved=list(recovery.workspace_root().glob('recovery-*/conversations.sqlite3'))
                self.assertTrue(any(p.read_bytes()==damaged for p in preserved))

    def test_future_schema_and_symlinks_fail_without_reinitialization(self):
        self.fixture()
        with sqlite3.connect(recovery.database_path()) as db:db.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(RuntimeError,'更新版本'):WorkspaceStore()
        path=recovery.database_path()
        held=path.with_suffix('.held');path.rename(held);path.symlink_to(held)
        with self.assertRaises(ValueError):WorkspaceStore()
        self.assertFalse(self.service.storage_status()['fingerprint'])


if __name__=='__main__':unittest.main()
