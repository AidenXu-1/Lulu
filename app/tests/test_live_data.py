"""Whitelisted metadata, durable local CSV and sampling fences. No network/secrets."""
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from src.core.live_recording.contracts import LiveError, LiveMetrics, Observation
from src.core.live_recording.data import COLUMNS, append_sample, data_path, read_data
from src.core.live_recording.douyin import parse_room
from src.core.live_recording.service import uid
from tests import test_live_recording as live_fixtures

payload, until = live_fixtures.payload, live_fixtures.until

class DataFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.recording = {'id': uid('recording'), 'name': '测试主播', 'directory': str(self.folder)}
        self.host = {'source': 'https://live.douyin.com/123'}

    def sample(self, viewers=100, *, raw='100', likes=0, cumulative=300, title='直播标题', at='2026-10-09T10:00:00+08:00'):
        observation = Observation('live', web_rid='123', name='测试主播',
            metrics=LiveMetrics(title, viewers, raw, likes, cumulative))
        append_sample(self.recording, self.host, observation, sampled_at=at)

    def test_room_fields_do_not_mix_online_and_total_or_approximate_counts(self):
        reply = payload()
        room = reply['data']['data'][0]
        room.update(title='公开标题', user_count_str='800+', like_count=5683,
                    stats={'user_count_str': '805', 'total_user_str': '4316', 'like_count': 0})
        metrics = parse_room(reply).metrics
        self.assertEqual(metrics, LiveMetrics('公开标题', 805, '805', 5683, 4316))
        room['stats'].pop('user_count_str')
        self.assertEqual(parse_room(reply).metrics.viewer_count_raw, '800+')
        self.assertIsNone(parse_room(reply).metrics.viewer_count)
        room.update(user_count_str='', like_count=False, stats={'total_user_str': '1.2万'}, room_view_stats={'display_long': '805热度', 'display_value': '805'})
        metrics = parse_room(reply).metrics
        self.assertIsNone(metrics.viewer_count)
        self.assertIsNone(metrics.like_count)
        self.assertIsNone(metrics.cumulative_view_count)

    def test_csv_survives_reopen_preserves_zero_and_has_no_private_fields(self):
        self.sample(0, raw='0', likes=0, cumulative=0, title='中文,带引号"')
        self.sample(None, raw='800+', at='2026-10-09T10:05:00+08:00')
        path = data_path(self.folder)
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        with path.open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(tuple(rows[0]), COLUMNS)
        self.assertEqual(rows[0]['like_count'], '0')
        self.assertEqual(rows[1]['viewer_count'], '')
        self.assertEqual(rows[1]['viewer_count_raw'], '800+')
        self.assertNotIn('token', path.read_text())
        result = read_data(dict(self.recording))
        self.assertEqual(result['peak_viewers'], 0)
        self.assertIsNone(result['last_viewers'])
        self.assertEqual(result['last_viewers_raw'], '800+')
        self.assertEqual(len(result['samples']), 2)

    def test_public_room_without_total_keeps_other_metrics_and_missing_total(self):
        reply = payload()
        room = reply['data']['data'][0]
        room.update(user_count_str='606', like_count=6738)
        value = parse_room(reply, '123')
        self.assertEqual(value.metrics.viewer_count, 606)
        self.assertIsNone(value.metrics.cumulative_view_count)
        append_sample(self.recording, self.host, value)
        sample = read_data(self.recording)['samples'][0]
        self.assertEqual((sample['viewer_count'], sample['like_count']), (606, 6738))
        self.assertIsNone(sample['cumulative_view_count'])
        with data_path(self.folder).open(encoding='utf-8-sig') as file:
            self.assertEqual(next(csv.DictReader(file))['cumulative_view_count'], '')

    def test_platform_total_text_survives_csv_without_becoming_an_exact_number(self):
        for raw in ('6万+', '165.9万', '800+'):
            reply = payload()
            reply['data']['data'][0].update(stats={'total_user_str': raw})
            value = parse_room(reply, '123')
            self.assertIsNone(value.metrics.cumulative_view_count)
            self.assertEqual(value.metrics.cumulative_view_count_raw, raw)
            append_sample(self.recording, self.host, value)
            result = read_data(self.recording)
            self.assertEqual(result['samples'][-1]['cumulative_view_count_raw'], raw)
            self.assertIsNone(result['samples'][-1]['cumulative_view_count'])
            self.assertEqual(result['invalid_count'], 0)
        with data_path(self.folder).open(encoding='utf-8-sig') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(tuple(rows[0]), COLUMNS)  # Keep the established ten-column format.
        self.assertEqual(rows[0]['cumulative_view_count'], '6万+')

    def test_explicit_total_display_can_supply_exact_count_but_online_cannot(self):
        reply = payload()
        room = reply['data']['data'][0]
        room.update(stats={'total_user_str': '165.9万', 'user_count_str': '491'},
            room_view_stats={'display_long': '165.9万人看过', 'display_value': 1658939,
                             'display_type': 3, 'incremental': True})
        value = parse_room(reply, '123')
        self.assertEqual(value.metrics.cumulative_view_count, 1658939)
        self.assertEqual(value.metrics.viewer_count, 491)
        for display in ({'display_long': '491在线观众', 'display_value': 491, 'display_type': 1, 'incremental': False},
                        {'display_long': '165.9万人看过', 'display_value': 1658939},
                        {'display_long': '热度', 'display_value': 1658939, 'display_type': 3, 'incremental': True}):
            room['room_view_stats'] = display
            self.assertIsNone(parse_room(reply).metrics.cumulative_view_count)

    def test_failed_samples_are_blank_not_zero(self):
        self.sample()
        append_sample(self.recording, self.host, Observation('network_error'), sampled_at='2026-10-09T10:05:00+08:00')
        self.sample(90, at='2026-10-09T10:10:00+08:00')
        result = read_data(self.recording)
        self.assertEqual((result['sample_count'], result['valid_count']), (3, 2))
        self.assertIsNone(result['samples'][1]['viewer_count'])
        self.assertEqual(result['samples'][1]['status'], 'network_error')

    def test_public_metrics_survive_unavailable_video_urls(self):
        for streams in ({}, {'live_core_sdk_data': []}):
            reply = payload()
            room = reply['data']['data'][0]
            room.update(stream_url=streams, title='标题', user_count_str='42', like_count=5,
                        stats={'total_user_str': '70'})
            observation = parse_room(reply)
            self.assertEqual(observation.state, 'parse_error')  # Must not start a broken video URL.
            append_sample(self.recording, self.host, observation)
            result = read_data(self.recording)
            self.assertEqual(result['samples'][-1]['viewer_count'], 42)
            self.assertEqual(result['samples'][-1]['like_count'], 5)
            self.assertEqual(result['samples'][-1]['cumulative_view_count'], 70)

    def test_old_recordings_are_not_backfilled_and_text_is_formula_safe(self):
        self.assertEqual(read_data(self.recording)['samples'], [])
        self.assertFalse((self.folder/'直播数据').exists())
        self.sample(title='=1+1')
        self.assertIn("'=1+1", data_path(self.folder).read_text())
        self.assertEqual(read_data(self.recording)['title'], '=1+1')

    def test_formula_protection_round_trips_literal_quotes_and_maximum_title(self):
        for title in ("'=公开标题", "''标题", '=' + '文' * 499):
            self.sample(title=title)
            self.assertEqual(read_data(self.recording)['title'], title)

    def test_symlink_hardlink_and_incomplete_file_are_preserved(self):
        target = self.folder/'keep.csv'
        target.write_text('untouched')
        sub = self.folder/'直播数据'
        sub.mkdir()
        path = sub/'直播数据.csv'
        path.symlink_to(target)
        with self.assertRaises(LiveError): self.sample()
        path.unlink()
        os.link(target, path)
        with self.assertRaises(LiveError): self.sample()
        path.unlink()
        self.sample()
        saved = path.read_bytes()[:-2]
        path.write_bytes(saved)
        with self.assertRaises(LiveError): self.sample(at='2026-10-09T10:05:00+08:00')
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(target.read_text(), 'untouched')

    def test_bounded_chart_keeps_complete_csv_and_complete_summary(self):
        for number in range(5):
            self.sample(100 - number, at=f'2026-10-09T10:{number * 5:02d}:00+08:00')
        with patch('src.core.live_recording.data.MAX_POINTS', 3):
            result = read_data(self.recording)
        self.assertEqual(len(result['samples']), 3)
        self.assertTrue(result['truncated'])
        self.assertEqual((result['sample_count'], result['peak_viewers']), (5, 100))
        with data_path(self.folder).open(encoding='utf-8-sig') as file:
            self.assertEqual(len(list(csv.DictReader(file))), 5)

class DataServiceTests(unittest.TestCase):
    setUp = live_fixtures.ServiceTests.setUp
    call = live_fixtures.ServiceTests.call
    add = live_fixtures.ServiceTests.add

    def recording(self):
        hid = self.add()
        folder = self.output/uid('recording')
        folder.mkdir()
        rid = folder.name
        self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started) VALUES(?,?,?,?,?,?)',
                                (rid, hid, '测试主播', uid('live'), str(folder), time.time()))
        self.service._enable(self.service._host(hid))
        monitor = self.service.monitors[hid]
        monitor.update(thread=object(), recording_id=rid, directory=str(folder), data_next=float('inf'), next=float('inf'))
        self.addCleanup(self.service.monitors.clear)
        return hid, rid, monitor

    def test_local_data_read_is_authenticated_and_never_calls_provider_or_secrets(self):
        hid, rid, monitor = self.recording()
        self.service._save_data(self.service._host(hid), Observation('live', web_rid='123', metrics=LiveMetrics('标题', 0, '0', 0, 0)), monitor)
        with patch.object(self.provider, 'check', side_effect=AssertionError('network read')):
            result = self.call('data', recording_id=rid)
        self.assertEqual(result['peak_viewers'], 0)
        self.assertEqual(self.reads, [])
        with self.assertRaises(LiveError):
            self.service.handle('data', {'session': uid('session'), 'recording_id': rid})
        with self.assertRaises(LiveError):
            self.service.handle('data', {'session': self.session, 'recording_id': rid, 'path': '/tmp/other'})

    def test_removing_host_and_changing_default_directory_keeps_existing_csv_accessible(self):
        hid, rid, monitor = self.recording()
        self.service._save_data(self.service._host(hid), Observation('live', web_rid='123', metrics=LiveMetrics('标题', 20, '20')), monitor)
        saved_folder = self.call('data', recording_id=rid)['folder']
        monitor['cancel'].set()
        self.service.monitors.pop(hid)
        self.call('remove', host_id=hid)
        new_folder = self.root/'new-output'
        new_folder.mkdir()
        self.call('directory', path=str(new_folder))
        result = self.call('data', recording_id=rid)
        self.assertEqual((result['folder'], result['peak_viewers']), (saved_folder, 20))
        self.assertTrue(data_path(monitor['directory']).exists())

    def test_periodic_sampling_does_not_run_when_merely_viewing_and_uses_five_minutes(self):
        hid, rid, monitor = self.recording()
        self.provider.check = lambda *a: Observation('live', web_rid='123', metrics=LiveMetrics('标题', 10, '10'))
        monitor['data_next'] = time.monotonic() - 1
        until(lambda: data_path(monitor['directory']) is not None and data_path(monitor['directory']).exists())
        until(lambda: not self.service.data_checks)
        self.assertGreater(monitor['data_next'] - time.monotonic(), 295)
        self.assertEqual(self.call('data', recording_id=rid)['sample_count'], 1)

    def test_failure_is_a_gap_and_csv_failure_never_cancels_the_recorder(self):
        hid, rid, monitor = self.recording()
        self.provider.check = lambda *a: (_ for _ in ()).throw(RuntimeError('private-network-detail'))
        self.service._sample_data(hid, rid, monitor, self.session)
        result = self.call('data', recording_id=rid)
        self.assertEqual(result['samples'][0]['status'], 'network_error')
        self.assertIsNone(result['samples'][0]['viewer_count'])
        with patch('src.core.live_recording.service.append_sample', side_effect=OSError('disk')):
            self.service._sample_data(hid, rid, monitor, self.session)
        self.assertFalse(monitor['cancel'].is_set())
        self.assertIsNotNone(monitor['thread'])
        self.assertIn('录像继续', self.call('data', recording_id=rid)['data_error'])
        self.assertNotIn('private-network-detail', json.dumps(result))

    def test_slow_csv_read_and_write_do_not_block_pause_or_heartbeat(self):
        import src.core.live_recording.service as service_module
        for operation in ('read_data', 'append_sample'):
            with self.subTest(operation=operation):
                hid, rid, monitor = self.recording()
                entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
                original = getattr(service_module, operation)
                errors = []
                def slow(*args, **kwargs):
                    entered.set()
                    if not release.wait(4): raise AssertionError('CSV test was not released')
                    return original(*args, **kwargs)
                def control():
                    try:
                        self.call('pause', host_id=hid)
                        self.call('heartbeat')
                        controlled.set()
                    except Exception as exc: errors.append(exc)
                work = threading.Thread(target=(lambda: self.call('data', recording_id=rid)) if operation == 'read_data'
                                        else lambda: self.service._sample_data(hid, rid, monitor, self.session))
                with patch.object(service_module, operation, side_effect=slow):
                    work.start()
                    controller = threading.Thread(target=control)
                    try:
                        self.assertTrue(entered.wait(2))
                        controller.start()
                        self.assertTrue(controlled.wait(1), 'CSV I/O blocked recording controls')
                    finally:
                        release.set()
                        work.join(5)
                        if controller.ident is not None: controller.join(5)
                        self.service.monitors.clear()
                self.assertFalse(errors)
                self.assertFalse(work.is_alive())

    def test_late_sample_after_pause_end_or_recording_replacement_never_writes(self):
        for change in ('pause', 'end', 'replace', 'expire'):
            with self.subTest(change=change):
                hid, rid, monitor = self.recording()
                entered, release = threading.Event(), threading.Event()
                self.provider.check = lambda *a: (entered.set(), release.wait(3), Observation('live', metrics=LiveMetrics('标题', 30, '30')))[-1]
                with self.service.lock:
                    self.service._submit_data(hid, monitor, self.session)
                self.assertTrue(entered.wait(2))
                with self.service.lock:
                    if change == 'pause': monitor['cancel'].set()
                    elif change == 'end': monitor['thread'] = None
                    elif change == 'replace': monitor['recording_id'] = uid('recording')
                    else: self.service.deadline = time.monotonic() - 1
                release.set()
                until(lambda: not self.service.data_checks)
                self.assertIsNone(data_path(monitor['directory']))
                self.service.monitors.clear()

    def test_sample_waiting_for_csv_access_is_rejected_after_pause(self):
        hid, rid, monitor = self.recording()
        observed = threading.Event()
        self.provider.check = lambda *a: (observed.set(), Observation('live', metrics=LiveMetrics('标题', 30, '30')))[-1]
        with self.service.data_lock:
            with self.service.lock: self.service._submit_data(hid, monitor, self.session)
            self.assertTrue(observed.wait(2))
            self.call('pause', host_id=hid)
        until(lambda: not self.service.data_checks)
        self.assertIsNone(data_path(monitor['directory']))

    def test_inflight_local_read_after_core_shutdown_is_rejected_without_index_access(self):
        from src.core.live_recording.data import read_data as original
        _, rid, _ = self.recording()
        entered, release = threading.Event(), threading.Event()
        replies, errors = [], []
        def slow(row):
            entered.set()
            release.wait(4)
            return original(row)
        def read():
            try: replies.append(self.call('data', recording_id=rid))
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=read)
        with patch('src.core.live_recording.service.read_data', side_effect=slow):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.service.monitors.clear()  # Fixture owns no actual producer.
                self.service.shutdown()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(replies)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LiveError)
        self.assertFalse(worker.is_alive())
