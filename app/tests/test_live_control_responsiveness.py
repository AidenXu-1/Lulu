"""Real native clicks and temporary Core state; no credentials or live network."""
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from src.core.live_recording import service as service_module
from src.core.live_recording.contracts import LiveError, validate_request
from tests import test_live_recording_ui as fixtures


class ControlResponsivenessTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.NativeLiveTests.setUpClass.__func__)
    setUp = fixtures.NativeLiveTests.setUp
    tearDown = fixtures.NativeLiveTests.tearDown
    until = fixtures.NativeLiveTests.until
    seed = fixtures.NativeLiveTests.seed

    def steady(self):
        self.seed(hosts=2, recordings=0)
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None and not self.controller.queue)
        self.service.stop_event.set()
        self.service.scheduler.join(2)
        return self.page.data['rows'][0]['id']

    def test_click_pause_all_cancels_slow_start_before_disk_returns(self):
        hid = self.steady()
        entered, release = threading.Event(), threading.Event()
        def disk(_):
            entered.set()
            release.wait(8)
            return 100 * 1024**3
        with patch.object(self.service, 'disk_free', disk):
            try:
                QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
                self.until(entered.is_set)
                self.assertTrue(self.page.pause_all.isEnabled())
                began = time.monotonic()
                QTest.mouseClick(self.page.pause_all, Qt.LeftButton)
                self.until(lambda: not self.page.pending_hosts and not self.page._pausing_all, timeout=1)
                self.assertLess(time.monotonic() - began, .25)
                self.assertFalse(self.service._host(hid)['enabled'])
                self.assertFalse(self.service.busy())
                self.assertFalse(release.is_set())
                self.assertEqual(self.page.table.cellWidget(0, 6).findChild(QPushButton).text(), '开始监控')
            finally:
                release.set()
            self.until(lambda: not getattr(self.service, 'prepare_reads', {}))
        self.assertFalse(self.service._host(hid)['enabled'])
        self.assertNotIn(hid, self.service.monitors)
        self.assertEqual(self.service.control.list(), ())

    def test_directory_picker_keeps_pause_heartbeat_and_complete_stop_free(self):
        hid = self.steady()
        self.service.handle('start', {'session': self.controller.session, 'host_id': hid})
        self.page.refresh()
        self.until(lambda: self.page.data['monitoring'] == 1)
        target = self.root / 'new-output'
        target.mkdir()
        entered, release = threading.Event(), threading.Event()
        real_directory = service_module.directory
        def directory(path, **kwargs):
            if str(path) == str(target):
                entered.set()
                release.wait(8)
            return real_directory(path, **kwargs)
        with patch.object(service_module, 'directory', directory), patch(
                'src.ui.live_recording_page.QFileDialog.getExistingDirectory', return_value=str(target)):
            try:
                QTest.mouseClick(self.page.storage_button, Qt.LeftButton)
                action = next(a for a in self.page._storage_popup.actions() if a.text() == '更改保存位置')
                action.trigger()
                self.page._storage_popup.close()
                self.until(entered.is_set)
                QTest.mouseClick(self.page.pause_all, Qt.LeftButton)
                self.until(lambda: not self.service._host(hid)['enabled'], timeout=.25)
                old_deadline = self.service.deadline
                self.controller.heartbeat_at = 0
                self.controller._tick()
                self.until(lambda: self.service.deadline > old_deadline, timeout=.25)
                self.controller.request_stop()
                self.until(lambda: self.controller.settled, timeout=1)
                self.assertTrue(self.service.closing)
                self.assertFalse(self.service.busy())
                self.assertFalse(release.is_set())
                self.assertEqual(self.service.output_dir(), str(self.output))
            finally:
                release.set()
            self.until(lambda: not getattr(self.service, 'prepare_reads', {}))
        self.assertEqual(self.service.output_dir(), str(self.output))

    def test_pause_click_before_start_reaches_core_prevents_late_enable(self):
        hid = self.steady()
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        def delayed(operation, arguments):
            if operation == 'start':
                entered.set()
                release.wait(5)
            return original(operation, arguments)
        self.controller.dispatcher = delayed
        try:
            QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
            self.until(entered.is_set)
            QTest.mouseClick(self.page.pause_all, Qt.LeftButton)
            self.until(lambda: not self.page._pausing_all and not self.page.pending_hosts)
            self.assertGreater(self.service.pause_sequence, 0)
        finally:
            release.set()
        self.until(lambda: self.controller.start_worker is None)
        self.assertFalse(self.service._host(hid)['enabled'])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.service.control.list(), ())

    def test_late_success_after_pause_cannot_restore_started_ui(self):
        hid = self.steady()
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        def delayed(operation, arguments):
            reply = original(operation, arguments)
            if operation == 'start':
                entered.set()
                release.wait(5)
            return reply
        self.controller.dispatcher = delayed
        try:
            QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
            self.until(entered.is_set)
            self.assertTrue(self.service._host(hid)['enabled'])
            QTest.mouseClick(self.page.pause_all, Qt.LeftButton)
            self.until(lambda: not self.page._pausing_all)
            self.assertFalse(self.service._host(hid)['enabled'])
        finally:
            release.set()
        self.until(lambda: self.controller.start_worker is None)
        self.assertFalse(self.page.data['rows'][0]['enabled'])
        self.assertEqual(self.page.table.cellWidget(0, 6).findChild(QPushButton).text(), '开始监控')
        self.assertEqual(self.errors, [])

    def test_slow_start_does_not_block_other_host_pause_or_path_change(self):
        hid = self.steady()
        other = self.page.data['rows'][1]['id']
        self.service.handle('start', dict(session=self.controller.session, host_id=other))
        self.page.refresh()
        self.until(lambda: self.page.data['monitoring'] == 1)
        entered, release = threading.Event(), threading.Event()
        def disk(_):
            entered.set()
            release.wait(5)
            return 100 * 1024**3
        target = self.root / 'other-volume'
        target.mkdir()
        with patch.object(self.service, 'disk_free', disk):
            try:
                QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
                self.until(entered.is_set)
                QTest.mouseClick(self.page.table.cellWidget(1, 6).findChild(QPushButton), Qt.LeftButton)
                self.until(lambda: not self.service._host(other)['enabled'], timeout=.25)
                self.controller.call('directory', path=str(target))
                self.until(lambda: self.service.output_dir() == str(target), timeout=.25)
                self.until(lambda: self.controller.start_worker is None)
                self.assertTrue(self.service.prepare_reads)
                self.assertFalse(self.service._host(hid)['enabled'])
            finally:
                release.set()
            self.until(lambda: not self.service.prepare_reads)
        self.assertFalse(self.service._host(hid)['enabled'])

    def test_volume_timeout_is_bounded_and_retry_cannot_accumulate_reads(self):
        hid = self.steady()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def directory(path, **kwargs):
            calls.append(str(path))
            entered.set()
            release.wait(5)
            return path
        args = dict(session=self.controller.session)
        original = self.service.output_dir()
        with patch.object(service_module, 'PREPARE_TIMEOUT', .08), patch.object(service_module, 'directory', directory):
            try:
                for number in range(4):
                    with self.assertRaisesRegex(LiveError, '响应过慢'):
                        self.service.handle('directory', dict(args, path=str(self.root / f'volume-{number}')))
                self.assertEqual(len(self.service.prepare_reads), 4)
                for _ in range(12):
                    with self.assertRaisesRegex(LiveError, '仍在检查'):
                        self.service.handle('directory', dict(args, path=str(self.root / 'volume-4')))
                self.assertEqual(len(calls), 4)
                self.assertEqual(self.service.output_dir(), original)
                self.controller.request_stop()
                self.until(lambda: self.controller.settled)
                # A new session must not receive changes from old disk work.
                self.service.handle('open', {'nonce': 'c' * 32})
            finally:
                release.set()
            self.until(lambda: not self.service.prepare_reads)
        self.assertEqual(self.service.output_dir(), original)
        self.assertFalse(self.service._host(hid)['enabled'])

    def test_control_sequences_reject_stale_intent_and_invalid_wire_values(self):
        hid = self.steady()
        args = dict(session=self.controller.session, host_id=hid)
        for bad in (0, -1, True, '1', 2**53, float('nan')):
            with self.subTest(bad=bad), self.assertRaises(LiveError):
                validate_request('start', dict(args, sequence=bad))
        self.service.handle('pause', dict(args, sequence=2))
        with self.assertRaisesRegex(LiveError, '后续操作取消'):
            self.service.handle('start', dict(args, sequence=1))
        self.service.handle('start', dict(args, sequence=4))
        self.service.handle('pause_all', dict(session=self.controller.session, sequence=3))
        self.assertTrue(self.service._host(hid)['enabled'])
        self.service.handle('pause_all', dict(session=self.controller.session, sequence=5))
        self.assertFalse(self.service._host(hid)['enabled'])

    def test_pending_directory_cannot_start_with_previous_path(self):
        hid = self.steady()
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        calls = []
        target = self.root / 'new-choice'
        target.mkdir()
        def delayed(operation, arguments):
            calls.append(operation)
            if operation == 'directory':
                entered.set()
                release.wait(5)
            return original(operation, arguments)
        self.controller.dispatcher = delayed
        try:
            self.controller.call('directory', path=str(target))
            self.until(entered.is_set)
            QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
            self.assertNotIn('start', calls)
            self.assertIn('保存位置正在检查', self.errors[-1])
            self.assertFalse(self.page.pending_hosts)
            self.assertFalse(self.service._host(hid)['enabled'])
        finally:
            release.set()
        self.until(lambda: self.controller.directory_worker is None)
        self.assertEqual(self.service.output_dir(), str(target))
        QTest.mouseClick(self.page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
        self.until(lambda: bool(self.service._host(hid)['enabled']))

    def test_pause_all_settles_before_new_start_and_totals_remain_consistent(self):
        hid = self.steady()
        other = self.page.data['rows'][1]['id']
        self.service.handle('start', dict(session=self.controller.session, host_id=hid))
        self.page.refresh()
        self.until(lambda: self.page.data['monitoring'] == 1)
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        def delayed(operation, arguments):
            if operation == 'pause_all':
                entered.set()
                release.wait(5)
            return original(operation, arguments)
        self.controller.dispatcher = delayed
        try:
            QTest.mouseClick(self.page.pause_all, Qt.LeftButton)
            self.until(entered.is_set)
            other_button = self.page.table.cellWidget(1, 6).findChild(QPushButton)
            self.assertFalse(other_button.isEnabled())
            QTest.mouseClick(other_button, Qt.LeftButton)
            self.assertFalse(self.service._host(other)['enabled'])
        finally:
            release.set()
        self.until(lambda: not self.page._pausing_all and self.page.data['monitoring'] == 0)
        QTest.mouseClick(self.page.table.cellWidget(1, 6).findChild(QPushButton), Qt.LeftButton)
        self.until(lambda: self.page.data['monitoring'] == 1)
        self.assertTrue(self.service._host(other)['enabled'])
        self.assertEqual(self.page.stats[0].text(), '1 位')

    def test_two_rooms_share_volume_check_and_cancel_only_the_requested_room(self):
        from src.core.live_recording.contracts import Observation
        self.steady()
        hosts = [row['id'] for row in self.page.data['rows']]
        self.service.provider.check = lambda *_: Observation('live')
        for cancel_one in (False, True):
            with self.subTest(cancel_one=cancel_one):
                for hid in hosts:
                    self.service.handle('start', dict(session=self.controller.session, host_id=hid))
                entered, release = threading.Event(), threading.Event()
                reads, commits = [], []
                def disk(path):
                    reads.append(str(path))
                    entered.set()
                    release.wait(5)
                    return 100 * 1024**3
                def commit(host, *args):
                    commits.append(host['id'])
                with patch.object(self.service, 'disk_free', disk), patch.object(self.service, '_commit_begin', commit):
                    try:
                        for hid in hosts:
                            self.service._submit_check(hid, self.controller.session)
                        self.until(entered.is_set)
                        self.until(lambda: len(self.service.checks) == 2)
                        self.assertEqual(len(self.service.prepare_reads), 1)
                        if cancel_one:
                            self.controller.call('pause', host_id=hosts[0])
                            self.until(lambda: hosts[0] not in self.service.checks)
                    finally:
                        release.set()
                    self.until(lambda: not self.service.checks and not self.service.prepare_reads)
                self.assertEqual(reads, [str(self.output)])
                self.assertCountEqual(commits, hosts[1:] if cancel_one else hosts)
                self.service.handle('pause_all', dict(session=self.controller.session))
