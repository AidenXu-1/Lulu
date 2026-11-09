"""Confirmed cross-flow regressions; temporary ledger/media and no real services."""
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.live_recording.contracts import LiveError, Observation, Stream
from src.core.live_recording.media import fingerprint
from src.core.live_recording.service import LiveService
from tests import test_live_recording as fixtures

until = fixtures.until


class HeldProducer:
    def __init__(self, ffmpeg, stream, folder, **kwargs):
        self.started = time.time()
        self.has_data, self.last_bytes, self.file_count = True, 1, 0
        self.last_growth = time.monotonic()
        self.stopped = threading.Event()
        self.process = self

    def poll(self):
        return 0 if self.stopped.is_set() else None

    def stop(self):
        self.stopped.set()

    def growing(self):
        self.last_growth = time.monotonic()
        return True

    def completed(self):
        return []


class CrossFlowTests(unittest.TestCase):
    call, add = fixtures.ServiceTests.call, fixtures.ServiceTests.add

    def setUp(self):
        fixtures.ServiceTests.setUp(self)
        self.service.stop_event.set()
        self.service.scheduler.join(2)

    def hosts(self):
        return self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows']

    def tick(self):
        class OneIteration:
            calls = 0

            def wait(self, seconds):
                self.calls += 1
                return self.calls > 1

        original = self.service.stop_event
        try:
            self.service.stop_event = OneIteration()
            self.service.last_disk = 0
            self.service._schedule()
            until(lambda: not getattr(self.service, 'disk_checks', {}))
        finally:
            self.service.stop_event = original

    def start_held(self, number, folder):
        self.provider.check = lambda host, key: Observation('live', web_rid=host['source'].rsplit('/', 1)[-1],
            streams=(Stream('https://pull.douyincdn.com/fixture.flv'),))
        self.service.producer_factory = HeldProducer
        hid = self.add(number)
        self.call('directory', path=str(folder))
        self.call('start', host_id=hid)
        self.service._check(hid, self.session, False)
        until(lambda: self.service.monitors[hid].get('producer'))
        until(lambda: not self.service.data_checks)
        record = next(row for row in self.call('snapshot', tab='recordings', query='', filter='all', page=1)['rows']
                      if row['host_id'] == hid)
        return hid, record

    def test_slow_disk_control_and_late_results_are_fenced(self):
        for operation in ('periodic', 'start', 'begin'):
            with self.subTest(operation=operation):
                hid = self.add(700 + ('periodic', 'start', 'begin').index(operation))
                if operation != 'start':
                    self.call('start', host_id=hid)
                entered, release, done = threading.Event(), threading.Event(), threading.Event()
                errors = []
                def slow(path):
                    entered.set()
                    release.wait(10)
                    return 100 * 1024**3
                def run():
                    try:
                        if operation == 'periodic': self.tick()
                        elif operation == 'start': self.call('start', host_id=hid)
                        else:
                            self.service._begin(self.service._host(hid), Observation('live'),
                                                self.service.monitors[hid], self.session)
                    except LiveError:
                        pass  # A pending start is explicitly cancelled.
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        done.set()
                self.service.disk_free = slow
                work = threading.Thread(target=run, daemon=True)
                work.start()
                self.assertTrue(entered.wait(2))
                paused = threading.Event()
                def pause():
                    self.call('pause_all')
                    self.call('heartbeat')
                    paused.set()
                control = threading.Thread(target=pause, daemon=True)
                control.start()
                try:
                    self.assertTrue(paused.wait(.25), 'disk I/O held the control lock')
                finally:
                    release.set()
                    work.join(3)
                    control.join(3)
                    until(lambda: not getattr(self.service, 'disk_checks', {}))
                    self.service.disk_free = lambda p: 100 * 1024**3
                self.assertTrue(done.is_set())
                self.assertFalse(errors)
                self.assertFalse(self.service._host(hid)['enabled'])
                self.assertEqual(self.control.list(), ())

    def test_slow_producer_handshake_does_not_block_pause(self):
        entered, release = threading.Event(), threading.Event()
        producers = []
        def slow(*args, **kwargs):
            entered.set()
            release.wait(10)
            producer = HeldProducer(*args, **kwargs)
            producers.append(producer)
            return producer
        hid = self.add(801)
        self.call('start', host_id=hid)
        self.service.producer_factory = slow
        self.provider.check = lambda *args: Observation('live', streams=(Stream('https://pull.douyincdn.com/f.flv'),))
        worker = threading.Thread(target=self.service._check, args=(hid, self.session, False), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))
        done = threading.Event()
        def pause():
            self.call('pause_all')
            self.call('heartbeat')
            done.set()
        control = threading.Thread(target=pause, daemon=True)
        control.start()
        try:
            self.assertTrue(done.wait(.25), 'producer handshake held the control lock')
        finally:
            release.set()
            worker.join(3)
            control.join(3)
        until(lambda: not self.service.busy())
        self.assertTrue(producers)
        self.assertTrue(all(p.stopped.is_set() for p in producers))
        self.assertFalse(self.service._host(hid)['enabled'])

    def test_late_directory_check_cannot_overwrite_newer_selection(self):
        from src.core.live_recording.index import directory
        old, new = self.root/'old-output', self.root/'new-output'
        old.mkdir()
        new.mkdir()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def delayed(path, **kwargs):
            if str(path) == str(old):
                entered.set()
                release.wait(5)
            return directory(path, **kwargs)
        def select_old():
            try:
                self.call('directory', path=str(old))
            except LiveError as exc:
                errors.append(exc)
        with patch('src.core.live_recording.service.directory', delayed):
            worker = threading.Thread(target=select_old, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.call('directory', path=str(new))
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.service.output_dir(), str(new))
        self.assertEqual(len(errors), 1)
        # Returning to the same path still invalidates a start prepared earlier.
        hid = self.add(802)
        entered.clear()
        release.clear()
        errors.clear()
        def disk(path):
            entered.set()
            release.wait(5)
            return 100*1024**3
        def start():
            try:
                self.call('start', host_id=hid)
            except LiveError as exc:
                errors.append(exc)
        self.service.disk_free = disk
        worker = threading.Thread(target=start, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.call('directory', path=str(old))
            self.call('directory', path=str(new))
        finally:
            release.set()
            worker.join(3)
            self.service.disk_free = lambda p: 100*1024**3
        self.assertFalse(self.service._host(hid)['enabled'])
        self.assertEqual(len(errors), 1)






    def interrupted_recording(self, writer=None):
        self.provider.check = lambda *args: Observation('offline', web_rid='public_alias', name='同一主播')
        hid = self.call('add', source='https://live.douyin.com/public_alias')['host_id']
        until(lambda: not self.service.checks)

        def interrupted(ffmpeg, stream, folder, **kwargs):
            path = Path(folder)/'part-000000.ts'
            if writer:
                writer(ffmpeg, path)
            else:
                path.write_bytes(b'isolated interrupted media')
            raise LiveError('isolated interruption')

        self.service.producer_factory = interrupted
        self.provider.check = lambda *args: Observation('live', web_rid='public_alias', name='同一主播',
            streams=(Stream('https://pull.douyincdn.com/fixture.flv'),))
        self.call('start', host_id=hid)
        self.service._check(hid, self.session, False)
        record = until(lambda: next((row for row in self.call('snapshot', tab='recordings',
            query='', filter='all', page=1)['rows'] if row['ended'] is not None), None))
        self.call('pause', host_id=hid)
        return hid, record



