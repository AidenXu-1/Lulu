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

    def test_one_unavailable_recording_folder_does_not_stop_other_recordings(self):
        other = self.root/'other-save'
        other.mkdir()
        healthy, healthy_record = self.start_held(123, self.output)
        lost, lost_record = self.start_held(456, other)
        self.call('directory', path=str(self.output))
        path = Path(lost_record['directory'])
        preserved = path.rename(path.with_name('unavailable-preserved'))
        self.tick()
        until(lambda: self.call('detail', recording_id=lost_record['id'])['ended'] is not None)
        hosts = {row['id']: row for row in self.hosts()}
        self.assertTrue(hosts[healthy]['enabled'])
        self.assertFalse(hosts[lost]['enabled'])
        self.assertIsNone(self.call('detail', recording_id=healthy_record['id'])['ended'])
        self.assertTrue(Path(healthy_record['directory']).is_dir())
        self.assertTrue(preserved.is_dir())
        self.call('pause_all')
        until(lambda: not self.service.busy())

    def test_start_then_pause_discards_late_initial_identification(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return Observation('offline', 'fixture-sec', '123', '临时主播')

        self.provider.check = delayed
        hid = self.call('add', source='https://live.douyin.com/123')['host_id']
        try:
            self.assertTrue(entered.wait(2))
            self.call('start', host_id=hid)
            self.call('pause', host_id=hid)
            paused = self.hosts()[0]
        finally:
            release.set()
        until(lambda: not self.service.checks)
        after = self.hosts()[0]
        self.assertEqual((after['live_state'], after['checked']),
                         (paused['live_state'], paused['checked']))
        self.assertFalse(after['enabled'])
        self.assertEqual(self.control.list(), ())

    def test_superseded_initial_query_error_cannot_overwrite_restarted_monitor(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(*args):
            entered.set()
            release.wait(5)
            raise RuntimeError('isolated late query failure')

        self.provider.check = delayed
        hid = self.call('add', source='https://live.douyin.com/123')['host_id']
        try:
            self.assertTrue(entered.wait(2))
            self.call('start', host_id=hid)
            self.call('pause', host_id=hid)
            self.call('start', host_id=hid)
            before = self.hosts()[0]
        finally:
            release.set()
        until(lambda: not self.service.checks)
        after = self.hosts()[0]
        self.assertEqual((after['live_state'], after['checked'], after['status']),
                         (before['live_state'], before['checked'], before['status']))
        self.assertTrue(after['enabled'])
        self.assertEqual(self.control.list(), ())
        self.call('pause_all')

    def test_default_folder_error_does_not_stop_recording_at_original_location(self):
        healthy, record = self.start_held(123, self.output)
        other = self.root/'other-save'
        other.mkdir()
        self.call('directory', path=str(other))
        waiting = self.add(456)
        self.call('start', host_id=waiting)

        def free(path):
            if Path(path) == other:
                raise OSError('isolated unavailable default volume')
            return 100*1024**3

        self.service.disk_free = free
        self.tick()
        hosts = {row['id']: row for row in self.hosts()}
        self.assertTrue(hosts[healthy]['enabled'])
        self.assertFalse(hosts[waiting]['enabled'])
        self.assertIsNone(self.call('detail', recording_id=record['id'])['ended'])
        self.assertIsNone(self.call('snapshot', tab='hosts', query='', filter='all', page=1)['free_bytes'])
        self.call('pause_all')
        until(lambda: not self.service.busy())

    def test_low_space_still_stops_only_the_affected_recording(self):
        other = self.root/'other-save'
        other.mkdir()
        healthy, healthy_record = self.start_held(123, self.output)
        full, full_record = self.start_held(456, other)
        self.service.disk_free = lambda path: (1024**3 if Path(path) == Path(full_record['directory']) else 100*1024**3)
        self.tick()
        until(lambda: self.call('detail', recording_id=full_record['id'])['ended'] is not None)
        hosts = {row['id']: row for row in self.hosts()}
        self.assertTrue(hosts[healthy]['enabled'])
        self.assertFalse(hosts[full]['enabled'])
        self.assertEqual(self.call('detail', recording_id=full_record['id'])['reason'], 'disk_full')
        self.assertIsNone(self.call('detail', recording_id=healthy_record['id'])['ended'])
        self.call('pause_all')
        until(lambda: not self.service.busy())

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

    def test_identity_merge_preserves_original_recording_ownership_and_delete(self):
        original, record = self.interrupted_recording()
        policy = self.control.show(record['job_id']).frozen_policy
        self.provider.check = lambda *args: Observation('offline', 'fixture-sec', '123', '同一主播')
        other = self.call('add', source='https://www.douyin.com/user/fixture-sec')['host_id']
        until(lambda: not self.service.checks)
        self.call('start', host_id=original)
        self.service._check(original, self.session, False)
        self.assertEqual([row['id'] for row in self.hosts()], [other])
        self.assertEqual(self.call('detail', recording_id=record['id'])['host_id'], original)
        self.assertEqual(self.control.show(record['job_id']).frozen_policy, policy)
        self.call('pause_all')
        trash = self.root/'fake-trash'
        trash.mkdir()
        folder = Path(record['directory'])
        before = (folder/'part-000000.ts').read_bytes()
        with patch('src.core.live_recording.service.move_to_trash',
                   side_effect=lambda path: str(path.rename(trash/path.name))) as move:
            result = self.call('delete_recording', recording_id=record['id'])
        self.assertTrue(result['trashed'])
        move.assert_called_once_with(folder)
        self.assertEqual((trash/folder.name/'part-000000.ts').read_bytes(), before)
        self.assertEqual(self.call('snapshot', tab='recordings', query='', filter='all', page=1)['rows'], [])

    def test_recovery_preserves_media_duration_without_inventing_wall_times(self):
        def writer(ffmpeg, path):
            subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-c:a', 'aac',
                '-f', 'mpegts', str(path)], check=True, timeout=15)

        _, record = self.interrupted_recording(writer)
        media = Path(record['directory'])/'part-000000.ts'
        before = fingerprint(media)
        self.assertEqual(self.call('detail', recording_id=record['id'])['segments'][0]['started'], 0)
        # Reproduce an existing unfinished row written by the previous version.
        self.service.index.write('UPDATE segments SET started=? WHERE recording_id=?',
                                 (media.stat().st_mtime, record['id']))
        self.service.shutdown()
        self.service = LiveService(self.root, self.control, 'isolated', provider=self.provider,
            ffmpeg=self.service.ffmpeg, secret_read=lambda: self.fail('credential access'),
            secret_write=lambda _: self.fail('credential write'), disk_free=lambda _: 100*1024**3)
        self.addCleanup(self.service.shutdown)
        self.session = self.service.handle('open', {'nonce': 'b'*32})['session']
        until(lambda: not self.service.busy())
        segment = self.call('detail', recording_id=record['id'])['segments'][0]
        self.assertTrue(segment['closed'] and segment['valid'])
        self.assertGreater(segment['duration'], 1.9)
        self.assertEqual(segment['started'], 0, 'A final file-write timestamp cannot prove a media start')
        self.assertEqual(fingerprint(media), before)

    def test_recovery_preserves_registered_start_of_a_completed_segment(self):
        hid = self.add()
        starts = []

        class CompletedProducer:
            def __init__(self, ffmpeg, stream, folder, **kwargs):
                self.started = time.time()
                starts.append(self.started)
                self.path = Path(folder)/'part-000000.ts'
                subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                    '-re', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-c:a', 'aac',
                    '-f', 'mpegts', str(self.path)], check=True, timeout=15)
                self.has_data, self.last_bytes, self.file_count = True, self.path.stat().st_size, 1
                self.last_growth = time.monotonic()
                self.process = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0), stop=lambda: None)

            def stop(self):
                pass

            def completed(self):
                return [(self.path, self.started)]

        self.service.producer_factory = CompletedProducer
        self.provider.check = lambda *args: Observation('live', streams=(Stream('https://pull.douyincdn.com/fixture.flv'),))
        self.call('start', host_id=hid)

        def temporarily_unreadable(*args, **kwargs):
            self.service.monitors[hid]['cancel'].set()
            raise LiveError('isolated first media-check failure')

        with patch('src.core.live_recording.service.probe', side_effect=temporarily_unreadable):
            self.service._check(hid, self.session, False)
            record = until(lambda: next((row for row in self.call('snapshot', tab='recordings',
                query='', filter='all', page=1)['rows'] if row['ended'] is not None), None))
        pending = self.call('detail', recording_id=record['id'])['segments'][0]
        self.assertTrue(pending['closed'])
        self.assertFalse(pending['valid'])
        self.assertEqual(pending['started'], starts[0])
        self.call('pause', host_id=hid)
        self.service.shutdown()
        self.service = LiveService(self.root, self.control, 'isolated', provider=self.provider,
            ffmpeg=self.service.ffmpeg, secret_read=lambda: self.fail('credential access'),
            secret_write=lambda _: self.fail('credential write'), disk_free=lambda _: 100*1024**3)
        self.addCleanup(self.service.shutdown)
        self.session = self.service.handle('open', {'nonce': 'b'*32})['session']
        until(lambda: not self.service.busy())
        recovered = self.call('detail', recording_id=record['id'])['segments'][0]
        self.assertTrue(recovered['valid'])
        self.assertGreater(recovered['duration'], 1.9)
        self.assertEqual(recovered['started'], starts[0])
