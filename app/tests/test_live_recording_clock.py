"""Measured recording time; isolated index and synthetic producer, no network."""
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_live_recording as fixtures
from src.core.live_recording.contracts import Observation, Stream
from src.core.live_recording.media import SegmentProducer


class ClockTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def active(self, *, saved=0, duration=0, has_data=True):
        rid = 'recording-clock-fixture'
        self.service.index.write(
            'INSERT INTO recordings(id,host_id,name,job_id,directory,started) VALUES(?,?,?,?,?,?)',
            (rid, 'host-clock-fixture', '隔离计时', 'live-clock-fixture', str(self.root), time.time()-100))
        if saved:
            self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                ('segment-clock-fixture', rid, str(self.root/'previous.ts'), 1, saved, 188, 1, 1, 'fixture'))
        process = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        producer = SimpleNamespace(started=time.time()-100, duration=duration, has_data=has_data,
            last_growth=time.monotonic(), last_bytes=376, file_count=2, process=process)
        monitor = dict(recording_id=rid, producer=producer, cancel=threading.Event(),
                       clock_base=saved, clock_started=producer.started, clock_duration=saved,
                       recording_state='live' if has_data else 'connecting')
        self.service.monitors['host-clock-fixture'] = monitor
        self.addCleanup(self.service.monitors.pop, 'host-clock-fixture', None)
        return self.service._recording(rid), monitor, producer

    def test_connection_wait_is_not_recorded_media_time(self):
        row, monitor, producer = self.active(duration=4.25)
        result = self.service._progress(row, 'live')
        self.assertTrue(result['clock_running'])
        self.assertEqual(result['duration'], 4.25)

    def test_wait_and_system_clock_changes_do_not_advance_media_time(self):
        row, monitor, producer = self.active(duration=4.25)
        first = self.service._progress(row, 'live')['duration']
        with patch('src.core.live_recording.service.time.time', return_value=producer.started+3600):
            self.assertEqual(self.service._progress(row, 'live')['duration'], first)
        producer.duration = 8.75
        self.assertEqual(self.service._progress(row, 'live')['duration'], 8.75)

    def test_reconnect_adds_previous_media_once_and_excludes_wait(self):
        row, monitor, producer = self.active(saved=12.5, duration=3.25)
        self.assertEqual(self.service._progress(row, 'live')['duration'], 15.75)
        # Newly validated segments from this connection are already in its progress.
        self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
            ('segment-current-fixture', row['id'], str(self.root/'current.ts'), 2, 2, 188, 1, 1, 'fixture'))
        self.assertEqual(self.service._progress(row, 'live')['duration'], 15.75)

    def test_new_connection_without_data_freezes_previous_media(self):
        row, monitor, producer = self.active(saved=12.5, has_data=False)
        result = self.service._progress(row, 'live')
        self.assertFalse(result['clock_running'])
        self.assertEqual(result['duration'], 12.5)

    def test_stop_freezes_clock_and_final_duration_uses_validated_media(self):
        row, monitor, producer = self.active(duration=4.25)
        self.assertEqual(self.service._progress(row, 'live')['duration'], 4.25)
        monitor['cancel'].set()
        producer.duration = 5
        self.assertEqual(self.service._progress(row, 'live')['duration'], 4.25)
        self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
            ('segment-final-fixture', row['id'], str(self.root/'final.ts'), 0, 5.1, 188, 1, 1, 'fixture'))
        self.service.index.write('UPDATE recordings SET ended=? WHERE id=?', (time.time(), row['id']))
        final = self.service._progress(self.service._recording(row['id']), 'live')
        self.assertFalse(final['clock_running'])
        self.assertEqual(final['duration'], 5.1)

    def test_unavailable_media_progress_does_not_guess_elapsed_time(self):
        row, monitor, producer = self.active()
        del producer.duration
        self.assertEqual(self.service._progress(row, 'live')['duration'], 0)


class RealClockTests(unittest.TestCase):
    call, add = fixtures.ServiceTests.call, fixtures.ServiceTests.add

    def setUp(self):
        fixtures.ServiceTests.setUp(self)
        media = self.root/'clock-source.ts'
        subprocess.run([self.service.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '35',
            '-c:v', 'mpeg2video', '-b:v', '600k', '-c:a', 'mp2', '-b:a', '64k',
            '-f', 'mpegts', str(media)], check=True, timeout=20)
        self.blob = media.read_bytes()

    def capture(self, *, delay, segment_seconds):
        blob = self.blob
        class Source(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                time.sleep(delay)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(blob)))
                self.end_headers()
                chunk = max(188, int(len(blob)/35/10)//188*188)
                try:
                    for offset in range(0, len(blob), chunk):
                        self.wfile.write(blob[offset:offset+chunk])
                        self.wfile.flush()
                        time.sleep(.1)
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.provider.check = lambda *a: Observation('live', streams=(
            Stream(f'http://127.0.0.1:{server.server_port}/synthetic.ts'),))
        self.service.producer_factory = lambda ffmpeg, stream, folder, **kw: SegmentProducer(
            ffmpeg, stream, folder, segment_seconds=segment_seconds, **kw)
        hid = self.add()
        self.call('start', host_id=hid)
        producer = fixtures.until(lambda: self.service.monitors[hid].get('producer'), timeout=15)
        # FFmpeg reports progress periodically, independently of the first disk write.
        fixtures.until(lambda: producer.has_data and producer.duration > 0, timeout=20)
        if segment_seconds == 2:
            fixtures.until(lambda: self.service.index.one(
                'SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'] >= 2, timeout=10)
        host = self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows'][0]
        record = self.call('snapshot', tab='recordings', query='', filter='all', page=1)['rows'][0]
        self.assertTrue(host['clock_running'])
        self.assertAlmostEqual(host['duration'], record['duration'], delta=.5)
        self.call('pause', host_id=hid)
        fixtures.until(lambda: not self.service.busy(), timeout=15)
        detail = self.call('detail', recording_id=record['id'])
        actual = sum(s['duration'] for s in detail['segments'] if s['valid'])
        self.assertGreater(actual, 0)
        self.assertGreater(host['duration'], 0)
        # Periodic reporting may lag the final packet; it must not include wait time
        # or double-count the completed segments in this same connection.
        self.assertAlmostEqual(host['duration'], actual, delta=1.5)
        self.assertEqual(detail['duration'], actual)
        self.assertFalse(detail['clock_running'])
        self.assertIsNotNone(producer.process.process.poll())
        return actual

    def test_delayed_connection_clock_matches_saved_media(self):
        self.capture(delay=4, segment_seconds=1800)

    def test_normal_recording_segments_are_not_counted_twice(self):
        self.capture(delay=0, segment_seconds=2)
