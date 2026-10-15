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
