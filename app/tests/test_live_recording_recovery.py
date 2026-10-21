"""Expired GUI sessions and observed CDN variants; isolated files and no network."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from src.core.live_recording.contracts import LiveError, Observation, safe_stream_url
from src.core.live_recording.douyin import parse_room
from src.core.live_recording.service import LiveService, uid
from src.ui.live_controller import LiveController


def room_payload(main_domain='douyinliving.com', backup_domain='douyincdn.com'):
    # Structure observed in the public room reply; URLs and identity are fixtures.
    streams = {}
    for line, domain in (('main', main_domain), ('backup', backup_domain)):
        streams[line] = {
            'flv': f'https://pull-flv.{domain}/fixture.flv',
            'hls': f'https://pull-hls.{domain}/fixture.m3u8',
            'sdk_params': json.dumps({'vbitrate': 8000000, 'VCodec': 'h264'}),
        }
    return {'status_code': 0, 'data': {'data': [{
        'status': 2, 'owner': {'nickname': '隔离主播', 'web_rid': '123'},
        'stream_url': {'live_core_sdk_data': {'pull_data': {
            'stream_data': json.dumps({'data': {'origin': streams}})}}},
    }]}}


class StreamRouteTests(unittest.TestCase):
    def test_observed_cdn_keeps_host_protocol_port_and_credential_boundaries(self):
        self.assertTrue(safe_stream_url('https://pull-flv-f1.douyinliving.com/fixture.flv'))
        for url in ('https://douyinliving.com.evil.example/fixture.flv',
                    'https://evildouyinliving.com/fixture.flv',
                    'https://douyinliving.com@evil.example/fixture.flv',
                    'https://user:secret@pull.douyinliving.com/fixture.flv',
                    'https://pull.douyinliving.com:8443/fixture.flv',
                    'file:///fixture.flv', 'https://127.0.0.1/fixture.flv'):
            with self.subTest(url=url):
                self.assertFalse(safe_stream_url(url))

    def test_live_room_keeps_main_and_backup_at_the_same_quality(self):
        value = parse_room(room_payload(), '123')
        self.assertEqual(value.state, 'live')
        self.assertEqual(len(value.streams), 4)
        self.assertEqual([s.protocol for s in value.streams], ['flv', 'flv', 'hls', 'hls'])
        self.assertIn('douyinliving.com', value.streams[0].url)
        self.assertIn('douyincdn.com', value.streams[1].url)

    def test_supported_backup_survives_an_unsupported_main_host(self):
        value = parse_room(room_payload(main_domain='untrusted.example'), '123')
        self.assertEqual(value.state, 'live')
        self.assertEqual(len(value.streams), 2)
        self.assertTrue(all('douyincdn.com' in s.url for s in value.streams))

    def test_untrusted_main_and_backup_cannot_start_a_recording(self):
        value = parse_room(room_payload('untrusted.example', 'other.example'), '123')
        self.assertEqual((value.state, value.streams), ('parse_error', ()))

    def test_malformed_route_does_not_discard_the_other_valid_route(self):
        for broken in ('main', 'backup'):
            with self.subTest(broken=broken):
                payload = room_payload()
                pull = payload['data']['data'][0]['stream_url']['live_core_sdk_data']['pull_data']
                raw = json.loads(pull['stream_data'])
                raw['data']['origin'][broken]['sdk_params'] = 'invalid metadata'
                pull['stream_data'] = json.dumps(raw)
                value = parse_room(payload, '123')
                self.assertEqual(value.state, 'live')
                self.assertEqual(len(value.streams), 2)

    def test_failed_connections_try_backup_before_reducing_quality(self):
        value = parse_room(room_payload(), '123')
        choices = [LiveService._choice(value, failures) for failures in range(4)]
        self.assertTrue(all(choices), 'live reply must expose usable stream choices')
        self.assertEqual([s.protocol for s in choices], ['flv', 'flv', 'hls', 'hls'])
        self.assertIn('douyinliving.com', choices[0].url)
        self.assertIn('douyincdn.com', choices[1].url)
        self.assertIn('douyinliving.com', choices[2].url)
        self.assertIn('douyincdn.com', choices[3].url)


class SessionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'recordings'
        self.output.mkdir()
        self.service = LiveService(self.root, SimpleNamespace(list=lambda: ()), 'isolated',
            provider=SimpleNamespace(check=lambda *_: Observation('offline')),
            secret_read=lambda: self.fail('real credential read'),
            secret_write=lambda _: self.fail('real credential write'),
            disk_free=lambda _: 100 * 1024**3)
        self.service.index.set_setting('directory', str(self.output))
        self.calls, self.errors, self.releases = [], [], []
        self.hook = None
        def dispatch(operation, arguments):
            self.calls.append((operation, dict(arguments)))
            if self.hook:
                result = self.hook(operation, arguments)
                if result is not None:
                    return result
            return self.service.handle(operation, arguments)
        self.controller = LiveController(dispatcher=dispatch)
        self.controller.error.connect(self.errors.append)
        self.controller.start()
        self.until(lambda: self.controller.connected and self.controller.worker is None)
        self.controller.timer.stop()

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('live session did not settle: ' + repr(self.errors))

    def heartbeat(self):
        self.controller.heartbeat_at = 0
        self.controller._tick()

    def wait_recovered(self, old_session):
        self.until(lambda: self.controller.connected and self.controller.session != old_session
                   and self.controller.worker is None)
        self.controller.timer.stop()

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.hook = None
        self.service.transcribing.clear()
        with self.service.lock:
            self.service._stop_session()
        self.controller.request_stop()
        self.until(lambda: self.controller.settled and self.controller.worker is None
                   and self.controller.data_worker is None)
        self.service.shutdown()
        self.controller.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_expired_empty_session_recovers_and_accepts_a_new_operation(self):
        old = self.controller.session
        self.service.deadline = time.monotonic() - 1
        self.until(lambda: self.service.closing)
        self.heartbeat()
        self.wait_recovered(old)
        replies = []
        self.controller.call('pause_all', replies.append)
        self.until(lambda: bool(replies) and self.controller.worker is None)
        self.assertEqual(replies, [{'changed': True}])
        self.assertEqual(self.errors, [])
        self.assertFalse(self.service.busy())

    def test_expired_heartbeat_before_scheduler_closes_instead_of_renewing_old_session(self):
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        old = self.controller.session
        self.service.deadline = time.monotonic() - 1
        deadline = self.service.deadline
        self.assertEqual(self.service.handle('heartbeat', {'session': old}),
                         {'active': False, 'stopped': True})
        self.assertEqual(self.service.deadline, deadline)
        self.heartbeat()
        self.wait_recovered(old)
        self.assertEqual(self.errors, [])

    def test_foreign_heartbeat_cannot_close_an_expired_current_session(self):
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        self.service.deadline = time.monotonic() - 1
        with self.assertRaises(LiveError):
            self.service.handle('heartbeat', {'session': uid('session')})
        self.assertFalse(self.service.closing)

    def test_recovery_waits_for_core_work_and_explicit_exit_never_reopens(self):
        old = self.controller.session
        self.service.transcribing.add('isolated-finishing-file-check')
        with self.service.lock:
            self.service._stop_session()
        self.heartbeat()
        self.until(lambda: self.controller.worker is None)
        self.assertEqual(self.controller.session, old)
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 1)
        self.service.transcribing.clear()
        self.heartbeat()
        self.wait_recovered(old)
        opened = sum(op == 'open' for op, _ in self.calls)
        self.controller.request_stop()
        self.until(lambda: self.controller.settled and self.controller.worker is None)
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), opened)

    def test_exit_while_reopening_still_stops_the_new_session(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold_open(operation, arguments):
            if operation == 'open':
                entered.set()
                release.wait(5)
        self.hook = hold_open
        with self.service.lock:
            self.service._stop_session()
        self.heartbeat()
        self.until(entered.is_set)
        self.controller.request_stop()
        release.set()
        self.until(lambda: self.controller.settled and self.controller.worker is None)
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 2)
        self.assertTrue(self.service.closing)
        self.assertFalse(self.service.busy())

    def test_recovery_discards_queued_operations_from_the_expired_session(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold_heartbeat(operation, arguments):
            if operation == 'heartbeat':
                entered.set()
                release.wait(5)
        self.hook = hold_heartbeat
        old = self.controller.session
        with self.service.lock:
            self.service._stop_session()
        self.heartbeat()
        self.assertTrue(entered.wait(2))
        self.controller.call('directory', path=str(self.root))
        self.hook = None
        release.set()
        self.wait_recovered(old)
        self.assertFalse(any(op == 'directory' for op, _ in self.calls))
        self.assertEqual(self.service.output_dir(), str(self.output))
        self.assertEqual(self.errors, ['直播连接已中断，未执行刚才的操作；连接恢复后请重试'])

    def test_recovered_session_ignores_a_late_file_error_from_the_old_session(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold_file(operation, arguments):
            if operation == 'data':
                entered.set()
                release.wait(5)
                raise LiveError('isolated stale file error')
        self.hook = hold_file
        self.controller.call('data', recording_id=uid('recording'))
        self.assertTrue(entered.wait(2))
        old = self.controller.session
        with self.service.lock:
            self.service._stop_session()
        self.heartbeat()
        self.wait_recovered(old)
        release.set()
        self.until(lambda: self.controller.data_worker is None)
        self.assertEqual(self.errors, [])
