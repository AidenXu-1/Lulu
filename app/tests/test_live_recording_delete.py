"""Temporary recordings and a fake Trash only; no user's files or credentials."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.job_ledger.contracts import JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.live_recording.contracts import LiveError, validate_request
from src.core.live_recording.service import KIND, LiveService, move_to_trash, uid, utc
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


def register_recording(service, root):
    rid, hid, jid, sid = uid('recording'), uid('host'), uid('live'), uid('segment')
    folder = root / rid
    folder.mkdir()
    source = folder / 'part-000000.ts'
    source.write_bytes(b'isolated original media')
    (folder / 'export.mp4').write_bytes(b'isolated export')
    data = folder / '直播数据'
    data.mkdir()
    (data / '直播数据.csv').write_text('sampled_at,viewer_count\nfixture,12\n')
    policy = {'host_id': hid, 'recording_id': rid, 'directory': str(folder), 'segment_seconds': 1800,
              'source': 'https://live.douyin.com/123'}
    job = service.control._store.create_job(job_id=jid, attempt_id=uid('attempt'), lineage_id=uid('lineage'),
        job_kind=KIND, input_ref='source:sha256:' + 'a'*64, frozen_policy=policy, created_by='gui',
        installation_id='isolated', canonical_operation_id='OP_GUI_LIVE_RECORDING_V1', idempotency_key=jid,
        intent_payload_digest=hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
        created_at=utc(), effect_fingerprint='effect:local-live-media', account_ref='account:none',
        target_ref='target:local-live-media', uncertain_response_state='none')
    queued = service.control._store.transition(jid, expected_revision=job.job_revision,
                                               new_state=JobState.QUEUED, changed_at=utc())
    running, lease = service.control.claim_worker(jid, expected_revision=queued.job_revision,
        worker_instance_id='fixture-live', operation_receipt=None, changed_at=utc())
    service.control.complete_worker(jid, expected_revision=running.job_revision, lease_token=lease,
        new_state=JobState.SUCCEEDED, result_ref=None, changed_at=utc())
    now = time.time() - 60
    service.index.write('INSERT INTO hosts(id,source,name,created,status) VALUES(?,?,?,?,?)',
                        (hid, 'https://live.douyin.com/' + rid, '本地主播', now, 'paused'))
    service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended,reason) VALUES(?,?,?,?,?,?,?,?)',
                        (rid, hid, '本地主播', jid, str(folder), now, now+30, 'offline'))
    service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
        (sid, rid, str(source), now, 30, source.stat().st_size, 1, 1, hashlib.sha256(source.read_bytes()).hexdigest()))
    return rid, folder, sid


class RecordingTrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        control = JobControlService(JobLedgerStore(self.root / 'ledger', FakeIntegrityAnchorStore()))
        self.service = LiveService(self.root, control, 'isolated', provider=SimpleNamespace(),
            secret_read=lambda: self.fail('credential access'), secret_write=lambda _: self.fail('credential write'),
            disk_free=lambda _: 100*1024**3)
        self.output = self.root / 'recordings'
        self.output.mkdir()
        self.trash = self.root / 'fake-trash'
        self.trash.mkdir()
        self.service.index.set_setting('directory', str(self.output))
        self.session = self.service.handle('open', {'nonce': 'a'*32})['session']
        self.rid, self.folder, self.sid = register_recording(self.service, self.output)
        self.moves = []
        self.patcher = patch('src.core.live_recording.service.move_to_trash', self.fake_trash)
        self.patcher.start()

    def tearDown(self):
        self.service.monitors.clear()
        self.service.exports.clear()
        self.service.data_checks.clear()
        self.patcher.stop()
        self.service.shutdown()
        self.temp.cleanup()

    def fake_trash(self, folder):
        self.moves.append(folder)
        return str(folder.rename(self.trash / folder.name))

    def delete(self, **overrides):
        return self.service.handle('delete_recording', {'session': self.session, 'recording_id': self.rid, **overrides})

    def use_upgraded_ledger(self):
        row = self.service.index.one('SELECT * FROM recordings WHERE id=?', (self.rid,))
        job = self.service.control.show(row['job_id'])
        self.service.historical_recordings = {job.job_id: {k:job.frozen_policy[k]
            for k in ('recording_id', 'host_id', 'directory')}}
        self.service.control = JobControlService(JobLedgerStore(self.root/'new-ledger', FakeIntegrityAnchorStore()))

    def test_transcription_check_keeps_controls_free_and_blocks_only_its_recording_delete(self):
        entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
        replies, errors = [], []
        original = self.service.index.one('SELECT sha256 FROM segments WHERE id=?', (self.sid,))['sha256']
        def slow(_):
            entered.set()
            release.wait(4)
            return original
        def check():
            try:
                replies.append(self.service.handle('transcribe', {'session': self.session, 'segment_id': self.sid}))
            except Exception as exc:
                errors.append(exc)
        def control():
            self.service.handle('heartbeat', {'session': self.session})
            self.service.handle('pause_all', {'session': self.session})
            controlled.set()
        with patch('src.core.live_recording.service.fingerprint', slow):
            worker = threading.Thread(target=check)
            worker.start()
            controller = threading.Thread(target=control)
            try:
                self.assertTrue(entered.wait(2))
                controller.start()
                self.assertTrue(controlled.wait(1), 'File validation blocked heartbeat and pause')
                with self.assertRaisesRegex(LiveError, '文案'):
                    self.delete()
                with self.assertRaises(LiveError):
                    self.service.handle('transcribe', {'session': self.session, 'segment_id': self.sid})
                stopped = self.service.handle('stop', {'session': self.session})
                self.assertTrue(stopped['active'])
                self.assertTrue(self.service.closing)
            finally:
                release.set()
                worker.join(5)
                controller.join(5)
        self.assertFalse(replies)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LiveError)
        self.assertTrue(self.folder.exists())
        self.assertFalse(self.service.busy())

    def test_transcription_check_releases_reservation_after_changed_file(self):
        with (self.folder/'part-000000.ts').open('ab') as file:
            file.write(b'changed')
        with self.assertRaisesRegex(LiveError, '已变化'):
            self.service.handle('transcribe', {'session': self.session, 'segment_id': self.sid})
        self.assertFalse(self.service.busy())
        self.assertTrue(self.delete()['trashed'])

    def test_upgraded_recording_uses_original_authenticated_registration(self):
        self.use_upgraded_ledger()
        self.assertTrue(self.delete()['trashed'])
        self.assertEqual(self.moves, [self.folder])
        self.assertFalse(self.folder.exists())

    def test_historical_registration_never_masks_integrity_failure_or_changed_index_path(self):
        from src.core.job_ledger.contracts import integrity_error
        self.use_upgraded_ledger()
        with patch.object(self.service.control, 'show', side_effect=integrity_error('LEDGER_TAMPERED')):
            with self.assertRaises(LiveError): self.delete()
        sibling = self.output/uid('recording'); sibling.mkdir()
        self.service.index.write('UPDATE recordings SET directory=? WHERE id=?', (str(sibling), self.rid))
        with self.assertRaises(LiveError): self.delete()
        self.assertEqual(self.moves, [])
        self.assertTrue(self.folder.is_dir())
        self.assertTrue(sibling.is_dir())

    def test_entire_folder_moves_and_only_its_index_rows_are_removed(self):
        other, sibling, _ = register_recording(self.service, self.output)
        outside = self.root / 'elsewhere.mp4'
        outside.write_bytes(b'independent export')
        before = {p.relative_to(self.folder): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(self.delete(), {'recording_id': self.rid, 'trashed': True})
        self.assertFalse(self.folder.exists())
        self.assertEqual(before, {p.relative_to(self.trash / self.rid): p.read_bytes()
            for p in (self.trash / self.rid).rglob('*') if p.is_file()})
        self.assertIsNone(self.service.index.one('SELECT id FROM recordings WHERE id=?', (self.rid,)))
        self.assertFalse(self.service.index.all('SELECT id FROM segments WHERE recording_id=?', (self.rid,)))
        self.assertIsNotNone(self.service.index.one('SELECT id FROM recordings WHERE id=?', (other,)))
        self.assertTrue(sibling.exists())
        self.assertEqual(outside.read_bytes(), b'independent export')
        self.assertEqual(len(self.service.control.list()), 2)
        self.assertEqual(self.service.index.one('SELECT COUNT(*) AS n FROM hosts')['n'], 2)
        with self.assertRaises(LiveError):
            self.delete()
        self.assertEqual(len(self.moves), 1)

    def test_native_trash_failure_retains_files_rows_and_allows_retry(self):
        with patch('src.core.live_recording.service.move_to_trash', side_effect=LiveError('移动失败')):
            with self.assertRaisesRegex(LiveError, '移动失败'):
                self.delete()
        self.assertTrue(self.folder.exists())
        self.assertIsNotNone(self.service.index.one('SELECT id FROM segments WHERE id=?', (self.sid,)))
        self.assertFalse(self.service.deleting)
        self.assertTrue(self.delete()['trashed'])

    def test_active_and_concurrent_file_work_reject_before_any_move(self):
        for mode in ('recording', 'finalizing', 'sampling', 'export', 'recovery', 'export_target'):
            with self.subTest(mode=mode):
                if mode == 'recording':
                    self.service.index.write('UPDATE recordings SET ended=NULL WHERE id=?', (self.rid,))
                elif mode == 'finalizing':
                    self.service.monitors['fixture'] = {'recording_id': self.rid, 'thread': object()}
                elif mode == 'sampling':
                    self.service.data_checks.add(self.rid)
                else:
                    self.service.exports['fixture'] = {'state': 'running',
                        'recording_ids': {self.rid} if mode != 'export_target' else set(),
                        'target': str(self.folder / 'new.mp4') if mode == 'export_target' else ''}
                with self.assertRaises(LiveError):
                    self.delete()
                self.service.index.write('UPDATE recordings SET ended=? WHERE id=?', (time.time(), self.rid))
                self.service.monitors.clear()
                self.service.exports.clear()
                self.service.data_checks.clear()
        self.assertEqual(self.moves, [])
        self.assertTrue(self.folder.exists())

    def test_forged_index_path_and_symlink_are_never_trashed(self):
        unrelated = self.root / self.rid
        unrelated.mkdir()
        (unrelated / 'private.txt').write_text('retain')
        self.service.index.write('UPDATE recordings SET directory=? WHERE id=?', (str(unrelated), self.rid))
        with self.assertRaisesRegex(LiveError, '登记不一致'):
            self.delete()
        self.service.index.write('UPDATE recordings SET directory=? WHERE id=?', (str(self.folder), self.rid))
        real = self.folder.rename(self.output / 'original-preserved')
        self.folder.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaisesRegex(LiveError, '符号链接'):
            self.delete()
        self.assertTrue(real.exists())
        self.assertTrue((unrelated / 'private.txt').exists())
        self.assertEqual(self.moves, [])

    def test_changed_save_location_still_deletes_only_registered_old_folder(self):
        new = self.root / 'new-output'
        new.mkdir()
        self.service.index.set_setting('directory', str(new))
        self.assertTrue(self.delete()['trashed'])
        self.assertTrue(new.exists())

    def test_invalid_channel_fields_or_stale_session_cannot_move_files(self):
        with self.assertRaises(LiveError):
            validate_request('delete_recording', {'session': self.session, 'recording_id': self.rid, 'path': str(self.root)})
        with self.assertRaises(LiveError):
            self.delete(session=uid('session'))
        self.assertTrue(self.folder.exists())
        self.assertEqual(self.moves, [])

    def test_slow_move_does_not_hold_controls_and_rejects_second_delete(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow(folder):
            entered.set()
            release.wait(4)
            return self.fake_trash(folder)
        def run():
            try:
                results.append(self.delete())
            except Exception as exc:
                results.append(exc)
        with patch('src.core.live_recording.service.move_to_trash', slow):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                before = time.monotonic()
                heartbeat = self.service.handle('heartbeat', {'session': self.session})
                self.service.handle('pause_all', {'session': self.session})
                self.assertLess(time.monotonic() - before, .5)
                self.assertTrue(heartbeat['active'])
                with self.assertRaises(LiveError):
                    self.delete()
                row = self.service.snapshot('recordings', '', 'all', 1)['rows'][0]
                self.assertTrue(row['delete_blocked'])
            finally:
                release.set()
                worker.join(4)
        self.assertEqual(results, [{'recording_id': self.rid, 'trashed': True}])

    def test_failed_index_transaction_rolls_back_rows_and_reports_actual_file_location(self):
        self.service.index.write("CREATE TRIGGER deny_recording_delete BEFORE DELETE ON recordings BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaisesRegex(LiveError, '已移入系统废纸篓，列表未能更新'):
            self.delete()
        self.assertFalse(self.folder.exists())
        self.assertTrue((self.trash / self.rid).exists())
        self.assertIsNotNone(self.service.index.one('SELECT id FROM segments WHERE id=?', (self.sid,)))
        self.assertIsNotNone(self.service.index.one('SELECT id FROM recordings WHERE id=?', (self.rid,)))
        self.assertFalse(self.service.deleting)

    def test_admitted_data_read_finishes_before_delete_and_new_reads_are_refused(self):
        entered, release = threading.Event(), threading.Event()
        replies, deletions = [], []
        def read(_):
            entered.set()
            release.wait(4)
            return {'sample_count': 1, 'samples': []}
        def run_read():
            try:
                replies.append(self.service.handle('data', {'session': self.session, 'recording_id': self.rid}))
            except Exception as exc:
                replies.append(exc)
        def run_delete():
            try:
                deletions.append(self.delete())
            except Exception as exc:
                deletions.append(exc)
        with patch('src.core.live_recording.service.read_data', read):
            reader = threading.Thread(target=run_read)
            deletion = threading.Thread(target=run_delete)
            reader.start()
            try:
                self.assertTrue(entered.wait(2))
                deletion.start()
                limit = time.monotonic() + 2
                while self.rid not in self.service.deleting and time.monotonic() < limit:
                    time.sleep(.01)
                self.assertIn(self.rid, self.service.deleting)
                with self.assertRaisesRegex(LiveError, '正在移入'):
                    self.service.handle('data', {'session': self.session, 'recording_id': self.rid})
                self.assertTrue(self.folder.exists())
            finally:
                release.set()
                reader.join(4)
                if deletion.ident is not None:
                    deletion.join(4)
        self.assertEqual(len(replies), 1)
        self.assertIsInstance(replies[0], dict)
        self.assertEqual(replies[0]['sample_count'], 1)
        self.assertEqual(deletions, [{'recording_id': self.rid, 'trashed': True}])

    def test_platform_adapter_uses_native_trash_and_never_permanent_deletion(self):
        with patch('PySide6.QtCore.QFile.moveToTrash', return_value=(True, '/fake-trash/recording')) as native:
            self.assertEqual(move_to_trash(self.folder), '/fake-trash/recording')
            native.assert_called_once_with(str(self.folder))
        with patch('PySide6.QtCore.QFile.moveToTrash', return_value=(False, '')):
            with self.assertRaisesRegex(LiveError, '录像保留'):
                move_to_trash(self.folder)
        self.assertTrue(self.folder.exists())

    def test_native_boolean_success_updates_index_after_actual_move(self):
        def native(path):
            self.fake_trash(Path(path))
            return True
        self.patcher.stop()
        with patch('PySide6.QtCore.QFile.moveToTrash', side_effect=native) as move:
            self.assertTrue(self.delete()['trashed'])
        move.assert_called_once_with(str(self.folder))
        self.assertFalse(self.service.index.all('SELECT id FROM recordings'))
        self.assertFalse(self.service.index.all('SELECT id FROM segments'))

    def test_native_boolean_failure_retains_files_and_rows(self):
        self.patcher.stop()
        with patch('PySide6.QtCore.QFile.moveToTrash', return_value=False):
            with self.assertRaisesRegex(LiveError, '录像保留'):
                self.delete()
        self.assertTrue(self.folder.exists())
        self.assertIsNotNone(self.service.index.one('SELECT id FROM recordings WHERE id=?', (self.rid,)))

    def test_retry_after_files_already_moved_only_removes_stale_record(self):
        self.fake_trash(self.folder)
        # Even an older row without a transferable job registration can be
        # removed from this owned index; this never authorizes a file operation.
        self.service.control = JobControlService(JobLedgerStore(self.root/'new-ledger', FakeIntegrityAnchorStore()))
        reply = self.delete()
        self.assertEqual(reply, {'recording_id': self.rid, 'trashed': False})
        self.assertEqual(self.moves, [self.folder])
        self.assertFalse(self.service.index.all('SELECT id FROM recordings'))
        self.assertTrue((self.trash/self.rid/'part-000000.ts').is_file())

    def test_disconnected_save_parent_does_not_remove_record(self):
        self.output.rename(self.root/'disconnected-output')
        with self.assertRaisesRegex(LiveError, '保存位置不存在'):
            self.delete()
        self.assertIsNotNone(self.service.index.one('SELECT id FROM recordings WHERE id=?', (self.rid,)))
        self.assertEqual(self.moves, [])
