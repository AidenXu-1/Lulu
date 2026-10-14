"""Isolated contracts, real ledger and real bundled FFmpeg; no Douyin/keychain."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import multiprocessing
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from src.core.job_ledger.contracts import JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.live_recording.contracts import LiveError, Observation, Stream, public_link, safe_stream_url, validate_request
from src.core.live_recording.douyin import parse_room
from src.core.live_recording.media import bundled_ffmpeg, export_media, fingerprint, probe, SegmentProducer
from src.core.live_recording.service import LiveService
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore

def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError('isolated live operation did not settle')

def payload(status=2, stream=True):
    return {'status_code': 0, 'data': {'user': {'sec_uid': 'MS4w-test', 'nickname': '本地主播'}, 'data': [{
        'status': status, 'stream_url': {'live_core_sdk_data': {'pull_data': {'stream_data': json.dumps({'data': {
            'origin': {'main': {'flv': 'https://pull.douyincdn.com/live.flv?token=secret',
                               'hls': 'https://pull.douyincdn.com/live.m3u8?token=secret',
                               'sdk_params': json.dumps({'vbitrate': 4000000, 'VCodec': 'h264'})}}
        }})}}} if stream else {}}]}}

def crash_owner(ffmpeg, url, folder, connection):
    producer = SegmentProducer(ffmpeg, Stream(url), folder, segment_seconds=2)
    connection.send((producer.process.process.pid, producer.process.child_pid))
    connection.close()
    while True:
        time.sleep(1)

class ContractTests(unittest.TestCase):
    def test_public_only_identity_and_no_signed_query(self):
        self.assertEqual(public_link('分享 https://live.douyin.com/123?token=private'), 'https://live.douyin.com/123')
        for url in ['http://live.douyin.com/123', 'https://127.0.0.1/123', 'https://evil.com/user/a',
                    'https://live.douyin.com.evil.com/123', 'https://live.douyin.com:9443/123',
                    'https://user:secret@live.douyin.com/123', 'https://www.douyin.com/video/123']:
            with self.subTest(url=url), self.assertRaises(LiveError):
                public_link(url)
        self.assertFalse(safe_stream_url('https://127.0.0.1/media'))
        self.assertFalse(safe_stream_url('file:///private/tmp/x.ts'))

    def test_closed_operations_reject_process_and_test_overrides(self):
        for args in [{'nonce': 'a'*32, 'ffmpeg': '/tmp/fake'}, {'nonce': 'a'*32, 'allow_localhost': True}]:
            with self.assertRaises(LiveError):
                validate_request('open', args)
        with self.assertRaises(LiveError):
            validate_request('start', {'session': 'session-'+'a'*32, 'host_id': 'host-'+'b'*32, 'command': 'echo hi'})

    def test_empty_and_restricted_payload_never_mean_offline(self):
        self.assertEqual(parse_room({}).state, 'parse_error')
        self.assertEqual(parse_room({'status_code': 403}).state, 'access_limited')
        self.assertEqual(parse_room(payload(4), '123').state, 'offline')
        self.assertEqual(parse_room(payload(2, False), '123').state, 'parse_error')
        observed = parse_room(payload(), '123')
        self.assertEqual(observed.state, 'live')
        self.assertEqual(observed.streams[0].protocol, 'flv')
        self.assertNotIn('secret', repr(observed))

class ProviderTests(unittest.TestCase):
    def test_shared_profile_uses_anonymous_session_and_public_live_alias(self):
        from src.core.live_recording.douyin import DouyinProvider
        class Response:
            status_code = 200
            headers = {}
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield self.body
        class Session:
            def __init__(self):
                self.cookies = requests.cookies.RequestsCookieJar()
                self.calls = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, **kwargs):
                self.calls.append(('GET', url, kwargs))
                path = urlsplit(url).path
                if path == '/':
                    self.cookies.set('__ac_nonce', 'anonymous-nonce', domain='.douyin.com')
                    return Response(b'<html>public visitor</html>')
                if urlsplit(url).hostname == 'v.douyin.com':
                    reply = Response(b'')
                    reply.status_code = 302
                    reply.headers = {'Location': 'https://www.iesdouyin.com/share/user/MS4w-test'}
                    return reply
                if '/share/user/' in path:
                    self.cookies.set('__ac_nonce', 'share-only-fixture', domain='www.iesdouyin.com')
                    return Response(b'<html>public share page</html>')
                if path.endswith('/user/profile/other/'):
                    if 'ttwid=anonymous-fixture' not in kwargs['headers'].get('Cookie', ''):
                        return Response(b'')
                    return Response(json.dumps({'status_code': 0, 'user': {
                        'sec_uid': 'MS4w-test', 'nickname': '本地主播', 'room_id': 0,
                        'unique_id': 'public_anchor'}}).encode())
                self.assert_room = kwargs['params']['web_rid']
                return Response(json.dumps(payload()).encode())
            def post(self, url, **kwargs):
                self.calls.append(('POST', url, kwargs))
                self.cookies.set('ttwid', 'anonymous-fixture', domain='.bytedance.com')
                return Response(b'{}')
        session = Session()
        value = DouyinProvider(lambda: session).check({'source': 'https://v.douyin.com/fixture'})
        self.assertEqual((value.state, value.name, value.web_rid), ('live', '本地主播', 'public_anchor'))
        self.assertEqual(session.assert_room, 'public_anchor')
        self.assertTrue(value.streams)
        self.assertGreater(value.received_bytes, 0)
        self.assertEqual(urlsplit(session.calls[0][1]).hostname, 'v.douyin.com')
        profile_call = next(call for call in session.calls if '/user/profile/other/' in call[1])
        self.assertNotIn('share-only-fixture', profile_call[2]['headers']['Cookie'])
        self.assertTrue(all(call[2]['verify'] and not call[2]['allow_redirects'] for call in session.calls))
        self.assertEqual(sum(method == 'POST' for method, _, _ in session.calls), 1)

    def test_empty_api_response_is_restriction_and_keeps_known_identity(self):
        from src.core.live_recording.douyin import DouyinProvider
        class Response:
            status_code, headers = 200, {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield b''
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, *args, **kwargs): return Response()
        value = DouyinProvider(Session).check({'source': 'https://live.douyin.com/123',
                                               'name': '已知主播', 'sec_uid': 'MS4w-test',
                                               'web_rid': '123'}, 'fixture-cookie')
        self.assertEqual((value.state, value.name, value.web_rid), ('access_limited', '已知主播', '123'))

    def test_http_restriction_and_tls_redirect_boundaries(self):
        from src.core.live_recording.douyin import DouyinProvider
        class Response:
            headers = {}
            def __init__(self, code, body=b'{}'): self.status_code, self.body = code, body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield self.body
        class Session:
            def __init__(self, response): self.response, self.calls = response, []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, **kwargs): self.calls.append((url, kwargs)); return self.response
        for code, state in [(403, 'access_limited'), (429, 'access_limited'), (401, 'login_required'), (502, 'network_error')]:
            session = Session(Response(code))
            result = DouyinProvider(lambda: session).check({'source': 'https://live.douyin.com/123'})
            self.assertEqual(result.state, state)
            self.assertTrue(session.calls[0][1]['verify'])
            self.assertEqual(session.calls[0][1]['timeout'], (5, 15))
            self.assertNotIn('Cookie', session.calls[0][1]['headers'])
        response = Response(302)
        response.headers = {'Location': 'http://127.0.0.1/internal'}
        session = Session(response)
        result = DouyinProvider(lambda: session).check({'source': 'https://v.douyin.com/abc'})
        self.assertEqual(result.state, 'parse_error')
        self.assertEqual(len(session.calls), 1)
        session = Session(Response(200, b'<html>captcha</html>'))
        self.assertEqual(DouyinProvider(lambda: session).check({'source': 'https://live.douyin.com/123'}).state, 'access_limited')

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.control = JobControlService(JobLedgerStore(self.root/'ledger', FakeIntegrityAnchorStore()), enforce_fencing=True)
        self.provider = SimpleNamespace(check=lambda host, key: Observation('offline', 'sec-' + host['id'], '', '本地' + host['id'][-4:]))
        self.reads, self.writes = [], []
        self.service = LiveService(self.root, self.control, 'isolated', provider=self.provider,
            ffmpeg=bundled_ffmpeg(), secret_read=lambda: self.reads.append(True) or 'fixture-cookie',
            secret_write=self.writes.append, disk_free=lambda p: 100*1024**3)
        self.output = self.root/'recordings'
        self.output.mkdir()
        self.service.index.set_setting('directory', str(self.output))
        self.session = self.service.handle('open', {'nonce': 'a'*32})['session']
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.shutdown)

    def call(self, operation, **arguments):
        return self.service.handle(operation, {'session': self.session, **arguments})

    def add(self, number=123):
        result = self.call('add', source=f'https://live.douyin.com/{number}')
        until(lambda: not self.service.checks)
        return result['host_id']

    def test_passive_reads_do_not_access_secrets_add_does_not_monitor(self):
        self.call('credential', value='fixture-cookie')
        hid = self.add()
        result = self.call('snapshot', tab='hosts', query='', filter='all', page=1)
        self.assertEqual(self.reads, [])
        self.assertFalse(result['active'])
        self.assertEqual(self.control.list(), ())
        self.call('start', host_id=hid)
        self.assertEqual(self.reads, [True])
        until(lambda: not self.service.checks)
        self.call('pause_all')
        until(lambda: not self.service.busy())
        self.call('credential', value='')
        self.assertEqual(self.writes, ['fixture-cookie', ''])

    def test_identity_coalesces_homepage_room_without_two_producers(self):
        self.provider.check = lambda *args: Observation('offline', 'same-sec', '123', '同一主播')
        self.add(123)
        self.add(456)
        self.assertEqual(self.service.index.one('SELECT COUNT(*) AS n FROM hosts')['n'], 1)
        self.assertEqual(self.control.list(), ())

    def test_many_hosts_search_page_and_intent_restore_only(self):
        for number in range(25): self.add(1000 + number)
        result = self.call('snapshot', tab='hosts', query='', filter='all', page=1)
        self.assertEqual((len(result['rows']), result['total'], result['pages']), (20, 25, 2))
        hid = result['rows'][0]['id']
        self.call('start', host_id=hid)
        until(lambda: not self.service.checks)
        self.call('stop')
        until(lambda: not self.service.busy())
        self.assertTrue(self.service._host(hid)['enabled'])
        self.assertEqual(self.control.list(), ())
        other = self.service.handle('open', {'nonce': 'b'*32})['session']
        with self.assertRaises(LiveError): self.call('start', host_id=hid)
        self.session = other
        self.assertIn(hid, self.service.monitors)

    def test_expired_session_fences_late_network_completion(self):
        entered, release = threading.Event(), threading.Event()
        self.provider.check = lambda *args: (entered.set(), release.wait(5), Observation('live', streams=(Stream('https://pull.douyincdn.com/live.flv'),)))[-1]
        hid = self.call('add', source='https://live.douyin.com/123')['host_id']
        self.assertTrue(entered.wait(2))
        self.service.deadline = time.monotonic() - 1
        until(lambda: self.service.closing)
        release.set()
        until(lambda: not self.service.checks)
        self.assertEqual(self.control.list(), ())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_low_disk_pauses_intent_and_read_errors_do_not_start(self):
        hid = self.add()
        self.service.disk_free = lambda p: 1*1024**3
        with self.assertRaises(LiveError): self.call('start', host_id=hid)
        self.assertFalse(self.service._host(hid)['enabled'])
        self.service.disk_free = lambda p: 100*1024**3
        self.call('credential', value='fixture-cookie')
        self.service.secret_read = lambda: (_ for _ in ()).throw(RuntimeError('private credential'))
        with self.assertRaises(LiveError) as raised: self.call('start', host_id=hid)
        self.assertNotIn('private', str(raised.exception))
        self.assertEqual(self.control.list(), ())

    def test_date_search_keeps_normal_recordings_and_gap_metadata(self):
        from src.core.live_recording.service import uid
        now = time.time()
        for i, (name, start, gaps) in enumerate([('甲', now, [[now-10, now]]), ('甲', now-40*86400, [[1, 2]]), ('乙', now, []), ('甲', now, [])]):
            self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended,gaps) VALUES(?,?,?,?,?,?,?,?)',
                (uid('recording'), 'fixture', name, uid('live'), str(self.root/str(i)), start, start+1, json.dumps(gaps)))
        result = self.call('snapshot',tab='recordings',query='甲',filter='today',page=1)
        self.assertEqual(result['total'],2)
        self.assertEqual(sorted(bool(json.loads(row['gaps'])) for row in result['rows']), [False, True])

    def test_failed_claim_never_leaves_an_orphan_queued_job(self):
        hid = self.add()
        self.provider.check = lambda *a: Observation('live', streams=(Stream('https://pull.douyincdn.com/live.flv'),))
        with patch.object(self.control,'claim_worker',side_effect=RuntimeError('fixture failure')):
            self.call('start',host_id=hid)
            until(lambda: self.control.list() and not self.service.checks)
        self.assertTrue(all(j.state is JobState.FAILED for j in self.control.list()))
        self.assertFalse(self.service.monitors[hid].get('thread'))

class RealMediaTests(ServiceTests):
    # Own only actual media tests, rather than inheriting another suite's cases.
    test_passive_reads_do_not_access_secrets_add_does_not_monitor = None
    test_identity_coalesces_homepage_room_without_two_producers = None
    test_many_hosts_search_page_and_intent_restore_only = None
    test_expired_session_fences_late_network_completion = None
    test_low_disk_pauses_intent_and_read_errors_do_not_start = None
    test_date_search_keeps_normal_recordings_and_gap_metadata = None
    test_failed_claim_never_leaves_an_orphan_queued_job = None

    def setUp(self):
        super().setUp()
        self.media = self.root/'fixture.ts'
        subprocess.run([self.service.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-t', '8', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '10', '-c:a', 'aac',
            '-f', 'mpegts', str(self.media)], check=True, timeout=15)

    def server(self, *, stall=False):
        fixture = self.media.read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/MP2T')
                self.end_headers()
                try:
                    for _ in range(30):
                        self.wfile.write(fixture)
                        self.wfile.flush()
                        time.sleep(.2)
                        if stall: break
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/fixture.ts'

    def live(self, number=123, url=None):
        url = url or self.server()
        self.provider.check = lambda host, key: Observation('live', 'sec-' + host['id'], str(number), '本地主播', (Stream(url),))
        self.service.producer_factory = lambda ffmpeg, stream, folder, **kwargs: SegmentProducer(ffmpeg, stream, folder, segment_seconds=2, **kwargs)
        hid = self.add(number)
        self.call('start', host_id=hid)
        until(lambda: self.service.monitors[hid].get('thread'))
        until(lambda: list(self.output.glob('*/part-*.ts')))
        return hid

    def test_metadata_network_and_file_failure_do_not_stop_actual_ffmpeg(self):
        hid = self.live()
        monitor = self.service.monitors[hid]
        until(lambda: monitor.get('producer') and monitor['producer'].last_bytes > 0)
        producer = monitor['producer']
        before = producer.last_bytes
        self.provider.check = lambda *a: (_ for _ in ()).throw(RuntimeError('isolated metadata failure'))
        with self.service.lock:
            monitor['data_next'] = time.monotonic() - 1
        from src.core.live_recording.data import read_data
        until(lambda: read_data(self.service._recording(monitor['recording_id']))['sample_count'] == 2)
        with patch('src.core.live_recording.service.append_sample', side_effect=OSError('isolated disk failure')):
            self.service._sample_data(hid, monitor['recording_id'], monitor, self.session)
        until(lambda: producer.last_bytes > before)
        row = self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows'][0]
        self.assertEqual(row['message'], '无法确认')
        self.assertEqual(row['progress_message'], '正在录制')
        self.assertTrue(row['clock_running'])
        self.assertIsNone(producer.process.process.poll())
        self.assertFalse(monitor['cancel'].is_set())
        self.call('pause', host_id=hid)
        until(lambda: not self.service.busy())
        self.assertIsNotNone(producer.process.process.poll())

    def test_stalled_initial_csv_still_starts_video_and_pause_stops_process(self):
        from src.core.live_recording.data import append_sample
        entered, release = threading.Event(), threading.Event()
        def stalled(*args, **kwargs):
            entered.set()
            if not release.wait(10): raise AssertionError('CSV fixture was not released')
            return append_sample(*args, **kwargs)
        with patch('src.core.live_recording.service.append_sample', side_effect=stalled):
            try:
                hid = self.live()
                self.assertTrue(entered.wait(2))
                producer = self.service.monitors[hid]['producer']
                until(lambda: producer.last_bytes > 0)
                self.assertIsNone(producer.process.process.poll())
                self.call('pause', host_id=hid)
                until(lambda: producer.process.process.poll() is not None)
                self.assertFalse(release.is_set())  # Actual process stopped before file access resumed.
            finally:
                release.set()
        until(lambda: not self.service.busy())

    def test_four_owned_producers_pause_closes_processes_and_ledger(self):
        url = self.server()
        # Injectable provider is the sole local-network seam; production request cannot set it.
        self.provider.check = lambda host, key: Observation('live', 'sec-' + host['id'], '', '本地并发', (Stream(url),))
        self.service.producer_factory = lambda ffmpeg, stream, folder, **kwargs: SegmentProducer(ffmpeg, stream, folder, segment_seconds=2, **kwargs)
        hosts = [self.add(100+i) for i in range(4)]
        for hid in hosts: self.call('start', host_id=hid)
        until(lambda: sum(bool(m.get('thread')) for m in self.service.monitors.values()) == 4)
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'] >= 4, timeout=15)
        processes = [m['owned'] for m in self.service.monitors.values()]
        self.call('pause_all')
        until(lambda: not self.service.busy(), timeout=15)
        self.assertEqual(len(self.control.list()), 4)
        self.assertTrue(all(j.state is JobState.CANCELLED for j in self.control.list()))
        self.assertTrue(all(p.process.poll() is not None for p in processes))
        self.assertGreaterEqual(len(list(self.output.glob('*/part-*.ts'))), 4)

    def test_remove_running_host_stops_once_and_preserves_archive(self):
        hid = self.live()
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
        process = self.service.monitors[hid]['producer'].process
        self.call('remove', host_id=hid)
        until(lambda: not self.service.busy(), timeout=15)
        self.assertIsNone(self.service.index.one('SELECT id FROM hosts WHERE id=?', (hid,)))
        self.assertIsNotNone(process.process.poll())
        self.assertTrue(all(job.state is JobState.CANCELLED for job in self.control.list()))
        recording = self.service.index.one('SELECT * FROM recordings')
        self.assertIsNotNone(recording)
        self.assertTrue(self.call('detail', recording_id=recording['id'])['segments'])
        self.assertTrue(list(self.output.glob('*/part-*.ts')))

    def test_pause_stops_producer_while_completed_media_is_validating(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow_probe(*args, **kwargs):
            result = probe(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                release.wait(12)
            return result
        with patch('src.core.live_recording.service.probe', side_effect=slow_probe):
            hid = self.live()
            self.assertTrue(entered.wait(8))
            process = self.service.monitors[hid]['producer'].process
            self.call('pause_all')
            try:
                until(lambda: process.process.poll() is not None, timeout=5)
            finally:
                release.set()
                until(lambda: not self.service.busy(), timeout=15)
        self.assertTrue(list(self.output.glob('*/part-*.ts')))

    def test_segment_probe_export_preserves_original_and_refuses_mutation(self):
        info = probe(self.media, self.service.ffmpeg)
        self.assertTrue(info['audio'])
        segment = {'path': str(self.media), 'closed': 1, 'valid': 1, **info}
        recording = {'directory': str(self.root)}
        first = Path(export_media(segment, recording, self.service.ffmpeg))
        second = Path(export_media(segment, recording, self.service.ffmpeg))
        self.assertNotEqual(first, second)
        self.assertEqual(fingerprint(self.media), info['sha256'])
        self.assertTrue(probe(first, self.service.ffmpeg)['audio'])
        with self.media.open('ab') as handle: handle.write(b'changed')
        with self.assertRaises(LiveError): export_media(segment, recording, self.service.ffmpeg)
        broken = self.root/'broken.ts'
        broken.write_bytes(b'nonzero is not valid media')
        with self.assertRaises(LiveError): probe(broken, self.service.ffmpeg)

    def test_export_video_and_audio_to_chosen_paths_never_overwrites(self):
        from src.core.live_recording.media import export_media
        info = probe(self.media, self.service.ffmpeg)
        segment = {'path': str(self.media), 'closed': 1, 'valid': 1, **info}
        folder = self.root / 'chosen'
        folder.mkdir()
        for format in ('mp4', 'mp3'):
            target = folder / ('my-recording.' + format)
            result = Path(export_media(segment, {'directory': str(self.root)}, self.service.ffmpeg,
                                       format=format, target=target))
            self.assertEqual(result, target)
            self.assertTrue(probe(result, self.service.ffmpeg)['audio'])
            original = target.read_bytes()
            with self.assertRaises(LiveError):
                export_media(segment, {'directory': str(self.root)}, self.service.ffmpeg,
                             format=format, target=target)
            self.assertEqual(target.read_bytes(), original)
        self.assertEqual(fingerprint(self.media), info['sha256'])
        self.assertFalse(list(folder.glob('*.partial.*')))

    def test_two_views_share_live_clock_then_use_final_media_duration(self):
        hid = self.live()
        until(lambda:self.service.monitors[hid]['producer'].last_bytes > 0)
        host = self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows'][0]
        record = self.call('snapshot', tab='recordings', query='', filter='all', page=1)['rows'][0]
        self.assertTrue(host['clock_running'])
        self.assertTrue(record['clock_running'])
        self.assertAlmostEqual(host['duration'], record['duration'], delta=.2)
        self.call('pause_all')
        until(lambda:not self.service.busy(), timeout=15)
        final = self.call('snapshot', tab='recordings', query='', filter='all', page=1)['rows'][0]
        self.assertFalse(final['clock_running'])
        total = self.service.index.one('SELECT SUM(duration) AS duration FROM segments')['duration']
        self.assertEqual(final['duration'], total)

    def test_generic_job_cancel_really_stops_live_producer(self):
        hid = self.live()
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
        jid = self.service.monitors[hid]['job_id']
        owned = self.service.monitors[hid]['owned']
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.live_recording.service import utc
        runtime = LocalTranscriptionCliRuntime.__new__(LocalTranscriptionCliRuntime)
        runtime._job_control, runtime._history, runtime._now = self.control, {}, utc
        runtime._application = SimpleNamespace(assert_job_control_allowed=lambda _: None)
        runtime._workspace_application_lock = threading.RLock()
        runtime._live_recording = self.service
        reply = runtime.jobs_cancel(jid)
        self.assertEqual(reply['cancellation'], 'requested')
        until(lambda: not self.service.busy(), timeout=12)
        self.assertIs(self.control.show(jid).state, JobState.CANCELLED)
        self.assertEqual(self.service._host(hid)['enabled'], 0)
        self.assertIsNotNone(owned.process.poll())

    def test_heartbeat_expiry_stops_recording_preserves_intent_and_media(self):
        hid = self.live()
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
        jid = self.service.monitors[hid]['job_id']
        self.service.deadline = time.monotonic() - 1
        until(lambda: self.service.closing and not self.service.busy(), timeout=12)
        self.assertIs(self.control.show(jid).state, JobState.CANCELLED)
        self.assertEqual(self.service._host(hid)['enabled'], 1)
        self.assertTrue(list(self.output.glob('*/part-*.ts')))

    def test_core_crash_guardian_stops_actual_ffmpeg(self):
        folder = self.output/'crash'
        folder.mkdir()
        context = multiprocessing.get_context('spawn')
        connection, child = context.Pipe()
        owner = context.Process(target=crash_owner,args=(self.service.ffmpeg,self.server(),str(folder),child))
        owner.start()
        child.close()
        self.addCleanup(lambda: owner.kill() if owner.is_alive() else None)
        self.addCleanup(connection.close)
        self.assertTrue(connection.poll(10))
        guardian_pid, ffmpeg_pid = connection.recv()
        until(lambda:list(folder.glob('part-*.ts')))
        owner.terminate()
        owner.join(4)
        def gone(pid):
            try: os.kill(pid,0); return False
            except ProcessLookupError: return True
        until(lambda:gone(ffmpeg_pid),timeout=10)
        # Guardian may briefly be an OS-reaped zombie; FFmpeg cannot keep writing.
        self.assertTrue(list(folder.glob('part-*.ts')))

    def test_disk_drop_pauses_actual_producer_and_keeps_files(self):
        hid = self.live()
        until(lambda:self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'],timeout=12)
        self.service.disk_free = lambda p: 1024**3
        self.service.last_disk = 0
        until(lambda:not self.service.busy(),timeout=12)
        self.assertEqual(self.service._host(hid)['enabled'],0)
        self.assertTrue(list(self.output.glob('*/part-*.ts')))

    def test_drop_reconnect_adds_new_segments_and_visible_gap(self):
        self.service.producer_factory = lambda ffmpeg, stream, folder, **kwargs: SegmentProducer(ffmpeg, stream, folder, segment_seconds=2, **kwargs)
        calls = []
        def check(host, key):
            calls.append(time.monotonic())
            return Observation('live', 'local-sec', '123', '断线演练', (Stream(self.server()),))
        self.provider.check = check
        hid = self.add()
        self.call('start', host_id=hid)
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
        monitor = self.service.monitors[hid]
        first_job = monitor['job_id']
        # Terminate only this module-owned producer, then return a fresh endpoint.
        monitor['producer'].process.stop()
        until(lambda: len(calls)>=3, timeout=15)
        until(lambda: self.service.index.one("SELECT COUNT(*) AS n FROM recordings WHERE gaps!='[]'")['n'], timeout=15)
        self.assertEqual(monitor['job_id'],first_job)
        self.call('pause_all')
        until(lambda:not self.service.busy(),timeout=15)
        recording = self.service.index.one('SELECT * FROM recordings')
        self.assertTrue(json.loads(recording['gaps']))
        paths = [r['path'] for r in self.service.index.all('SELECT path FROM segments')]
        self.assertEqual(len(paths),len(set(paths)))
        self.assertGreaterEqual(len(paths),2)

    def test_confirmed_offline_closes_actual_media_and_all_views(self):
        hid = self.live()
        until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
        monitor = self.service.monitors[hid]
        rid = monitor['recording_id']
        producer = monitor['producer']
        self.provider.check = lambda *a: Observation('offline')
        producer.stop()
        until(lambda: not monitor.get('thread') and self.service._recording(rid)['ended'] is not None, timeout=22)
        self.assertIsNotNone(producer.process.process.poll())
        self.assertEqual(self.service._recording(rid)['reason'], 'offline')
        self.assertEqual(self.service._host(hid)['status'], 'offline')
        self.assertEqual(self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows'][0]['message'], '未直播')
        for tab in ('hosts', 'recordings'):
            state = self.call('snapshot', tab=tab, query='', filter='all', page=1)
            self.assertEqual(state['recording'], 0)
            self.assertFalse(state['rows'][0]['clock_running'])
        for operation in ('data', 'detail'):
            result = self.call(operation, recording_id=rid)
            self.assertEqual(result['recording_state'], 'ended')
            self.assertIsNotNone(result['ended'])
        self.assertTrue(all(segment['valid'] for segment in self.call('detail', recording_id=rid)['segments']))

    def test_access_failure_finishes_actual_media_and_reopen_preserves_files(self):
        with patch('src.core.live_recording.service.RECONNECT_TIMEOUT', 8):
            hid = self.live()
            until(lambda: self.service.index.one('SELECT COUNT(*) AS n FROM segments WHERE valid=1')['n'], timeout=12)
            monitor = self.service.monitors[hid]
            rid = monitor['recording_id']
            producer = monitor['producer']
            self.provider.check = lambda *a: Observation('access_limited')
            producer.stop()
            until(lambda: self.service._host(hid)['status'] == 'access_limited', timeout=6)
            self.assertEqual(self.call('snapshot', tab='hosts', query='', filter='all', page=1)['rows'][0]['message'], '无法确认')
            waiting = self.call('snapshot', tab='recordings', query='', filter='all', page=1)
            self.assertEqual(waiting['recording'], 0)
            self.assertEqual(waiting['rows'][0]['recording_state'], 'reconnecting')
            until(lambda: not monitor.get('thread') and self.service._recording(rid)['ended'] is not None, timeout=10)
        row = self.service._recording(rid)
        self.assertEqual(row['reason'], 'reconnect_timeout')
        self.assertEqual(self.service._host(hid)['status'], 'access_limited')
        self.assertIsNotNone(producer.process.process.poll())
        paths = {p: fingerprint(p) for p in Path(row['directory']).glob('part-*.ts')}
        from src.core.live_recording.data import data_path
        csv_path = data_path(row['directory'])
        paths[csv_path] = fingerprint(csv_path)
        self.service.shutdown()
        self.service = LiveService(self.root, self.control, 'isolated', provider=self.provider,
            ffmpeg=bundled_ffmpeg(), secret_read=lambda: 'fixture-cookie', secret_write=lambda _: None,
            disk_free=lambda _: 100*1024**3)
        self.addCleanup(self.service.shutdown)
        self.session = self.service.handle('open', {'nonce': 'b'*32})['session']
        reopened = self.call('snapshot', tab='recordings', query='', filter='all', page=1)
        self.assertEqual(reopened['recording'], 0)
        self.assertEqual(reopened['rows'][0]['reason'], 'reconnect_timeout')
        self.assertIsNotNone(reopened['rows'][0]['ended'])
        self.assertEqual(paths, {p: fingerprint(p) for p in paths})

class MeasuredLiveTests(ServiceTests):
    test_passive_reads_do_not_access_secrets_add_does_not_monitor = None
    test_identity_coalesces_homepage_room_without_two_producers = None
    test_many_hosts_search_page_and_intent_restore_only = None
    test_expired_session_fences_late_network_completion = None
    test_low_disk_pauses_intent_and_read_errors_do_not_start = None
    test_date_search_keeps_normal_recordings_and_gap_metadata = None
    test_failed_claim_never_leaves_an_orphan_queued_job = None

    def test_authenticated_gui_request_and_cli_role_rejection(self):
        from tests.test_generation9_connection_protocol import Generation9ConnectionProtocolTests
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient
        from src.core.agent_entry.handshake import HandshakeError
        fixture = Generation9ConnectionProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        state = self.root/'installation'
        state.mkdir()
        installation = SimpleNamespace(paths=SimpleNamespace(state_root=state))
        runtime = SimpleNamespace(live_recording=lambda:self.service)
        dispatcher = CoreCommandDispatcher(product_identity={},resource_root=self.root/'Resources',runtime_factory=lambda:runtime)
        protocol = AgentCoreProtocol(client_requirement=(fixture.gui_requirement,fixture.cli_requirement),dispatcher=dispatcher)
        class Exchange:
            def __init__(inner, facts):inner.facts=facts
            def exchange(inner, payload, **kwargs):return protocol.handle(payload,inner.facts),fixture.core_facts
        with patch('src.core.agent_entry.product_install.build_default_product_installer',return_value=installation):
            gui = MeasuredCoreClient(exchange=Exchange(fixture.gui_facts),client_requirement=fixture.gui_requirement,core_requirement=fixture.core_requirement)
            reply = gui.dispatch('gui.live',{'operation':'heartbeat','arguments':{'session':self.session}})
            self.assertEqual(reply['result']['active'],False)
            from src.core.live_recording.service import uid
            rid = uid('recording')
            folder = self.output/rid
            folder.mkdir()
            self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended) VALUES(?,?,?,?,?,?,?)',
                                     (rid, 'fixture', '隔离数据', uid('live'), str(folder), time.time()-10, time.time()))
            reply = gui.dispatch('gui.live', {'operation':'data','arguments':{'session':self.session,'recording_id':rid}})
            self.assertEqual(reply['result']['sample_count'], 0)
            cli = MeasuredCoreClient(exchange=Exchange(fixture.client_facts),client_requirement=fixture.cli_requirement,core_requirement=fixture.core_requirement)
            with self.assertRaises(HandshakeError):
                cli.dispatch('gui.live',{'operation':'add','arguments':{'session':self.session,'source':'https://live.douyin.com/123'}})
            with self.assertRaises(HandshakeError):
                cli.dispatch('gui.live', {'operation':'data','arguments':{'session':self.session,'recording_id':rid}})
        self.assertEqual(self.service.index.one('SELECT COUNT(*) AS n FROM hosts')['n'],0)

    def test_live_jobs_do_not_block_existing_synthesis_or_leak_into_extraction(self):
        from tests.test_cli_local_synthesis import LocalSynthesisTests
        from tests.test_phase5_job_ledger import _create_arguments
        fixture = LocalSynthesisTests('test_async_job_verified_audio_and_gui_separation')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        args = {**_create_arguments(),'job_kind':'live_recording'}
        job = fixture.store.create_job(**args)
        queued = fixture.store.transition(job.job_id,expected_revision=job.job_revision,new_state=JobState.QUEUED,changed_at='2026-10-07T10:00:00Z')
        fixture.control.claim_worker(job.job_id,expected_revision=queued.job_revision,worker_instance_id='live-fixture',operation_receipt=None,changed_at='2026-10-07T10:00:01Z')
        code, result = fixture.start()
        self.assertEqual(code,0,result)
        self.assertEqual(fixture.runtime.gui_jobs_list()['jobs'],[])
        self.assertEqual(fixture.finish(result['result']['job_id'])['state'],'succeeded')
