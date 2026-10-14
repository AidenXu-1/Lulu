"""Broadcast evidence stays separate from local recording activity; no network."""
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.live_recording.contracts import Observation, Stream
from src.core.live_recording.douyin import parse_room
from src.core.live_recording.index import LiveIndex
from src.core.live_recording.service import uid
from tests import test_live_recording as fixtures


class LiveStatusTests(unittest.TestCase):
    setUp, call, add = fixtures.ServiceTests.setUp, fixtures.ServiceTests.call, fixtures.ServiceTests.add

    def host(self, hid):
        return next(row for row in self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows']
                    if row['id'] == hid)

    def test_passive_platform_check_confirms_live_without_starting_a_recorder(self):
        self.service.provider.check = lambda *args: parse_room(fixtures.payload(), '123')
        hid = self.add()
        row = self.host(hid)
        self.assertEqual((row['live_state'], row['message']), ('live', '直播中'))
        self.assertEqual(row['progress_message'], '已暂停')
        self.assertFalse(row['clock_running'])
        self.assertFalse(self.service.monitors)
        self.assertEqual(self.control.list(), ())

    def test_unknown_replies_replace_previous_offline_and_do_not_claim_a_broadcast_ended(self):
        hid = self.add()
        self.assertEqual(self.host(hid)['message'], '未直播')
        for reply in ({}, fixtures.payload(2, False), {'status_code': 403}, {'data': {'data': []}}):
            with self.subTest(reply=reply):
                self.service.provider.check = lambda *args: parse_room(reply, '123')
                self.service._check(hid, self.session, True)
                row = self.host(hid)
                self.assertEqual((row['live_state'], row['message']), ('unknown', '无法确认'))

    def test_local_phases_and_failures_cannot_change_confirmed_broadcast_evidence(self):
        hid = self.add()
        self.service._remember_observation(hid, 'live')
        checked = self.host(hid)['checked']
        for phase in ('checking', 'reconnecting', 'stopping', 'paused', 'disk_full', 'write_error'):
            with self.subTest(phase=phase):
                self.service._status(hid, phase)
                row = self.host(hid)
                self.assertEqual((row['live_state'], row['message'], row['checked']), ('live', '直播中', checked))
        self.assertIn('保存失败', row['error_message'])

    def active_recording(self, hid):
        rid = uid('recording')
        self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started) VALUES(?,?,?,?,?,?)',
            (rid, hid, '本地主播', uid('live'), str(self.output/rid), time.time()))
        self.service._status(hid, 'live', checked=True)
        producer = SimpleNamespace(has_data=True, last_bytes=123, file_count=1,
            last_growth=time.monotonic(), started=time.time(),
            process=SimpleNamespace(process=SimpleNamespace(poll=lambda: None)))
        monitor = {'recording_id': rid, 'thread': object(), 'producer': producer, 'cancel': threading.Event(),
                   'recording_state': 'live', 'session': self.session, 'next': float('inf'),
                   'data_next': float('inf')}
        self.service.monitors[hid] = monitor
        self.addCleanup(self.service.monitors.clear)
        return rid, monitor

    def test_failed_data_check_shows_unknown_while_actual_recording_keeps_running(self):
        hid = self.add()
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        rid, monitor = self.active_recording(hid)
        for error in ('access_limited', 'network_error', 'login_required', 'parse_error'):
            with self.subTest(error=error), patch.object(self.service, '_save_data'):
                self.service.provider.check = lambda *args: Observation(error)
                self.service._sample_data(hid, rid, monitor, self.session)
                row = self.host(hid)
                self.assertEqual(row['message'], '无法确认')
                self.assertTrue(row['clock_running'])
                self.assertEqual(row['progress_message'], '正在录制')
                self.assertTrue(row['error_message'])
                self.assertEqual(self.call('snapshot', tab='hosts', query='', filter='live', page=1)['total'], 0)

    def test_reused_initial_sample_and_cancelled_check_cannot_overwrite_newer_evidence(self):
        hid = self.add()
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        rid, monitor = self.active_recording(hid)
        self.service._remember_observation(hid, 'network_error')
        checked = self.host(hid)['checked']
        with patch.object(self.service, '_save_data'):
            self.service._sample_data(hid, rid, monitor, self.session,
                                      Observation('live', streams=(Stream('https://pull.douyincdn.com/live.flv'),)))
        self.assertEqual((self.host(hid)['message'], self.host(hid)['checked']), ('无法确认', checked))
        def cancelled(*args):
            monitor['cancel'].set()
            return Observation('offline')
        self.service.provider.check = cancelled
        self.service._sample_data(hid, rid, monitor, self.session)
        self.assertEqual((self.host(hid)['message'], self.host(hid)['checked']), ('无法确认', checked))

    def test_filters_use_platform_evidence_instead_of_recording_phase_and_keep_pagination(self):
        for number in range(25):
            self.service.index.write('INSERT INTO hosts(id,source,name,created,status,live_state) VALUES(?,?,?,?,?,?)',
                (uid('host'), f'https://live.douyin.com/{1000+number}', '检索主播', number, 'reconnecting', 'live'))
        self.add(123)
        for page, count in ((1, 20), (2, 5)):
            result = self.call('snapshot', tab='hosts', query='检索', filter='live', page=page)
            self.assertEqual((result['total'], result['pages'], len(result['rows'])), (25, 2, count))
        self.assertEqual(self.call('snapshot', tab='hosts', query='', filter='offline', page=1)['total'], 1)
        self.assertEqual(self.call('snapshot', tab='hosts', query='', filter='unknown', page=1)['total'], 0)

    def test_reopening_requires_new_evidence_without_removing_host(self):
        hid = self.add()
        self.service._remember_observation(hid, 'live')
        self.call('stop')
        self.session = self.service.handle('open', {'nonce': 'b'*32})['session']
        self.assertEqual(self.host(hid)['message'], '无法确认')
        self.assertEqual(self.service.index.one('SELECT COUNT(*) AS n FROM hosts')['n'], 1)

    def test_legacy_index_keeps_original_fields_without_treating_old_status_as_evidence(self):
        folder = self.root/'legacy-index'
        folder.mkdir()
        with sqlite3.connect(folder/'index.sqlite3') as db:
            db.execute("""CREATE TABLE hosts(id TEXT PRIMARY KEY,source TEXT UNIQUE NOT NULL,
                sec_uid TEXT NOT NULL DEFAULT '',web_rid TEXT NOT NULL DEFAULT '',name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,checked REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'resolving')""")
            db.execute('INSERT INTO hosts(id,source,name,created,checked,status) VALUES(?,?,?,?,?,?)',
                       ('legacy', 'https://live.douyin.com/123', '原主播', 1., 2., 'offline'))
            before = dict(zip([c[0] for c in db.execute('SELECT * FROM hosts').description],
                              db.execute('SELECT * FROM hosts').fetchone()))
        for _ in range(2):
            index = LiveIndex(folder)
            try:
                row = index.one('SELECT * FROM hosts')
                self.assertEqual({key: row[key] for key in before}, before)
                self.assertEqual((row['live_state'], row['live_error']), ('unknown', ''))
            finally:
                index.close()
