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
            def __init__(self, target, args=(), **kwargs): self.target,self.args=target,args
            def start(self):
                if self.args:
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





    def test_normal_stop_during_final_reconnect_wait_does_not_invent_a_hole(self):
        hid, monitor, clock, _ = self.prepare([Observation('network_error')])
        original = monitor['cancel'].wait
        def stop(seconds):
            if seconds == 3:
                monitor['reason'] = 'paused'
                monitor['cancel'].set()
            return original(seconds)
        monitor['cancel'].wait = stop
        row, _, _ = self.record(hid, monitor, clock)
        self.assertEqual(row['reason'], 'paused')
        self.assertEqual(json.loads(row['gaps']), [])




class TailGapTests(unittest.TestCase):
    setUp, call, add = fixtures.ServiceTests.setUp, fixtures.ServiceTests.call, fixtures.ServiceTests.add

    def saved(self, gaps, parts=((1000, 10),), reason='paused'):
        hid = self.add()
        folder = self.root/'tail-recording'; folder.mkdir()
        rid='recording-'+'a'*32
        self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended,reason,gaps) VALUES(?,?,?,?,?,?,?,?,?)',
            (rid, hid, '正常停止样本', 'unused', str(folder), 1000, 1030.1, reason, json.dumps(gaps)))
        for n,(start,length) in enumerate(parts):
            path=folder/f'part-{n:06d}.ts';path.write_bytes(b'fixture')
            self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                ('segment-'+f'{n:032x}',rid,str(path),start,length,7,1,1,'a'*64))
        return rid

    def test_legacy_normal_stop_tail_is_saved_in_list_and_detail_without_changing_evidence(self):
        from src.core.live_recording.contracts import recording_message
        rid=self.saved([[1010,1030]])
        row=self.call('snapshot',tab='recordings',query='',filter='all',page=1)['rows'][0]
        detail=self.call('detail',recording_id=rid)
        for value in (row,detail):
            self.assertEqual(json.loads(value['gaps']),[])
            self.assertEqual(recording_message(value),'已保存')
        self.assertEqual(json.loads(self.service.index.one('SELECT gaps FROM recordings')['gaps']),[[1010,1030]])

    def test_resumed_gap_remains_and_only_final_pending_tail_is_excluded(self):
        rid=self.saved([[1003,1005],[1010,1030]],parts=((1000,3),(1005,5)))
        row=self.call('detail',recording_id=rid)
        self.assertEqual(json.loads(row['gaps']),[[1003,1005]])
        from src.core.live_recording.contracts import recording_message
        self.assertEqual(recording_message(row),'录制有缺口')

    def test_failed_or_unverified_media_keeps_its_warning(self):
        rid=self.saved([[1010,1030]])
        self.service.index.write('UPDATE segments SET valid=0 WHERE recording_id=?',(rid,))
        self.assertEqual(json.loads(self.call('detail',recording_id=rid)['gaps']),[[1010,1030]])
        self.service.index.write("UPDATE recordings SET reason='reconnect_timeout' WHERE id=?",(rid,))
        from src.core.live_recording.contracts import recording_message
        self.assertEqual(recording_message(self.call('detail',recording_id=rid)),'录制中断')
