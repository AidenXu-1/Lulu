"""Bounded media recovery; isolated ledger/files, no network or credentials."""
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from src.core.job_ledger.contracts import JobState
from src.core.live_recording.contracts import Observation, Stream
from src.core.live_recording.media import SegmentProducer
from src.core.live_recording import service as service_module
from tests import test_live_recording as fixtures

class LifecycleTests(unittest.TestCase):
    setUp, call, add = fixtures.ServiceTests.setUp, fixtures.ServiceTests.call, fixtures.ServiceTests.add

    def prepare(self, replies):
        hid = self.add()
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        self.service._enable(self.service._host(hid))
        monitor = self.service.monitors[hid]
        clock = SimpleNamespace(now=0., frames=[])
        outer = self
        class Cancel:
            flag = False
            def is_set(self): return self.flag
            def set(self): self.flag = True
            def wait(self, seconds):
                clock.now += seconds
                if monitor.get('recording_id'):
                    clock.frames.append(outer.call('snapshot',tab='recordings',query='',filter='all',page=1))
                if clock.now > 1000: self.flag = True  # Bound the old endless loop.
                return self.flag
        monitor['cancel'] = Cancel()
        observed = []
        def check(*args):
            value = replies.pop(0) if len(replies) > 1 else replies[0]
            observed.append((clock.now, value.state))
            return value
        self.provider.check = check
        return hid, monitor, clock, observed

    def record(self, hid, monitor, clock, attempts=None):
        attempts = [True] if attempts is None else attempts
        producers = []
        outer = self
        class Process:
            stopped = False
            def __init__(self): self.process = self
            def poll(self): return 0 if self.stopped else None
            def stop(self): self.stopped = True
        class Producer:
            def __init__(self, ffmpeg, stream, folder, start_number):
                self.started = time.time()
                self.last_growth = clock.now
                self.last_bytes = sum(p.stat().st_size for p in Path(folder).glob('part-*.ts'))
                self.file_count = len(list(Path(folder).glob('part-*.ts')))
                self.has_data = False
                self.path = Path(folder)/f'part-{start_number:06d}.ts'
                self.process = Process()
                self.write = attempts.pop(0) if attempts else False
                producers.append(self)
            def growing(self):
                if self.write and not self.has_data:
                    self.path.write_bytes(b'isolated-media')
                    self.last_bytes += self.path.stat().st_size
                    self.file_count += 1
                    self.has_data, self.last_growth = True, clock.now
                    return True
                return False
            def completed(self):
                return [(self.path,self.started)] if self.path.exists() else []
            def stop(self): self.process.stop()
        class InlineThread:
            def __init__(self, target, args, **kwargs): self.target,self.args=target,args
            def start(self):
                clock.frames.append(outer.call('snapshot',tab='recordings',query='',filter='all',page=1))
                self.target(*self.args)
        clock_module = SimpleNamespace(monotonic=lambda:clock.now, time=time.time, sleep=time.sleep)
        def media_probe(path, *args, **kwargs):
            return {'duration':10.,'bytes':path.stat().st_size,'sha256':'a'*64,'audio':True}
        with patch.object(service_module, 'time', clock_module), \
             patch.object(service_module.threading, 'Thread', InlineThread), \
             patch.object(service_module, 'probe', side_effect=media_probe), \
             patch.object(service_module, 'RECONNECT_TIMEOUT', 120., create=True), \
             patch.object(self.service, 'producer_factory', Producer):
            self.service._remember_observation(hid, 'live')
            self.service._begin(self.service._host(hid), Observation('live', streams=(Stream('https://pull.douyincdn.com/live.flv'),)), monitor, self.session)
        return self.service.index.one('SELECT * FROM recordings'), producers, clock

    def test_restricted_room_finishes_without_claiming_offline(self):
        hid, monitor, clock, observed = self.prepare([Observation('access_limited')])
        row, producers, _ = self.record(hid, monitor, clock)
        self.assertEqual(row['reason'], 'reconnect_timeout')
        self.assertLessEqual(clock.now, 121)
        self.assertFalse(monitor['cancel'].is_set())
        self.assertEqual(self.service._host(hid)['status'], 'access_limited')
        self.assertEqual(self.service._host(hid)['live_state'], 'unknown')
        self.assertTrue(all(p.process.poll() is not None for p in producers))
        self.assertEqual(self.control.list()[0].state, JobState.FAILED)
        self.assertTrue(json.loads(row['gaps']))
        self.assertTrue(Path(row['directory']).joinpath('part-000000.ts').is_file())
        for tab in ('hosts','recordings'):
            state = self.call('snapshot',tab=tab,query='',filter='all',page=1)
            self.assertEqual(state['recording'],0)
            self.assertFalse(state['rows'][0]['clock_running'])
        self.assertIsNotNone(self.call('data',recording_id=row['id'])['ended'])
        self.assertIsNotNone(self.call('detail',recording_id=row['id'])['ended'])
        self.assertTrue(observed)
        waiting = [frame for frame in clock.frames if frame['rows'][0]['recording_state']=='reconnecting']
        self.assertTrue(waiting)
        self.assertTrue(all(frame['recording']==0 and not frame['rows'][0]['clock_running'] for frame in waiting))

    def test_offline_then_failed_confirmation_finishes_as_unknown(self):
        hid, monitor, clock, _ = self.prepare([Observation('offline'),Observation('network_error')])
        row, _, _ = self.record(hid, monitor, clock)
        self.assertEqual(row['reason'],'reconnect_timeout')
        self.assertEqual(self.service._host(hid)['status'],'network_error')
        self.assertEqual(self.service._host(hid)['live_state'], 'unknown')

    def test_two_offline_observations_finish_normally(self):
        hid, monitor, clock, observed = self.prepare([Observation('offline')])
        row, _, _ = self.record(hid, monitor, clock)
        self.assertEqual(row['reason'],'offline')
        self.assertEqual(self.control.list()[0].state,JobState.SUCCEEDED)
        self.assertEqual(len(observed),2)
        self.assertEqual(self.service._host(hid)['status'],'offline')
        self.assertEqual(self.service._host(hid)['live_state'], 'offline')

    def test_short_disconnect_recovers_same_recording_and_retains_gap(self):
        live=Observation('live',streams=(Stream('https://pull.douyincdn.com/live.flv'),))
        hid, monitor, clock, _ = self.prepare([Observation('network_error'),live,Observation('offline')])
        row, producers, _ = self.record(hid,monitor,clock,attempts=[True,True])
        self.assertEqual(row['reason'],'offline')
        self.assertEqual(len(producers),2)
        self.assertEqual(len(self.control.list()),1)
        self.assertEqual(len(list(Path(row['directory']).glob('part-*.ts'))),2)
        self.assertTrue(json.loads(row['gaps']))

    def test_stale_live_replies_without_new_bytes_also_finish(self):
        live=Observation('live',streams=(Stream('https://pull.douyincdn.com/live.flv'),))
        hid, monitor, clock, _ = self.prepare([live])
        row, producers, _ = self.record(hid,monitor,clock,attempts=[True])
        self.assertEqual(row['reason'],'reconnect_timeout')
        self.assertLessEqual(clock.now,122)
        self.assertGreater(len(producers),1)
        self.assertEqual(self.service.snapshot('hosts', '', 'all', 1)['rows'][0]['message'], '直播中')

    def test_new_recording_does_not_inherit_previous_clock_or_bytes(self):
        hid, monitor, clock, _ = self.prepare([Observation('offline')])
        monitor.update(clock_duration=999, producer=SimpleNamespace(last_bytes=9999, file_count=2,
            has_data=True, process=SimpleNamespace(process=SimpleNamespace(poll=lambda:0))))
        self.record(hid,monitor,clock)
        first=clock.frames[0]['rows'][0]
        self.assertEqual((first['duration'],first['bytes'],first['n']), (0,0,0))
        self.assertEqual(first['recording_state'], 'connecting')

class GrowthTests(unittest.TestCase):
    def test_old_segments_do_not_count_as_new_connection_data(self):
        from tempfile import TemporaryDirectory
        from src.core.live_recording import media
        with TemporaryDirectory() as temporary:
            folder=Path(temporary).resolve()
            (folder/'part-000000.ts').write_bytes(b'old-recording')
            with patch.object(media,'OwnedRecordingProcess',return_value=SimpleNamespace(stop=lambda:None)):
                producer=SegmentProducer('unused',Stream('https://pull.douyincdn.com/live.flv'),folder,start_number=1)
            before=producer.last_growth
            with patch.object(media.time,'monotonic',return_value=before+31):
                self.assertFalse(producer.growing())
            self.assertFalse(producer.has_data)
            (folder/'part-000001.ts').write_bytes(b'new-recording')
            self.assertTrue(producer.growing())
            self.assertTrue(producer.has_data)
