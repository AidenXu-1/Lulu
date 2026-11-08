"""Review follow-up contracts with fake transport, browser and isolated stores."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sqlite3
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from src.core.live_recording.contracts import LiveError
from src.core.collect.application import CollectionCommand
from src.core.collect.contracts import CollectResult
from src.core.collect.router import detect_link
from src.ui.live_controller import LiveController
from src.ui.live_login import LiveLoginRequest
from src.ui.live_recording_page import LiveRecordingPage
from tests import test_homepage_login_feedback as previous
from tests import test_shared_douyin_login as shared


class ConnectionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.fail_open = 0
        self.fail_heartbeat = 0
        def dispatch(operation, arguments):
            self.calls.append((operation, dict(arguments)))
            if operation == 'open':
                if self.fail_open:
                    self.fail_open -= 1
                    raise LiveError('injected unavailable connection')
                return {'session': 'isolated-session'}
            if operation == 'heartbeat' and self.fail_heartbeat:
                self.fail_heartbeat -= 1
                raise LiveError('injected lost connection')
            return {'active': False, 'stopped': operation == 'stop'}
        self.controller = LiveController(dispatcher=dispatch)
        self.errors = []
        self.controller.error.connect(self.errors.append)

    def until(self, predicate, timeout=7):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.005)
        self.fail('connection recovery did not settle: ' + repr(self.errors))

    def tearDown(self):
        self.controller.request_stop()
        self.until(lambda: self.controller.settled and self.controller.worker is None)
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_initial_open_failure_retries_same_nonce_and_recovers(self):
        self.fail_open = 1
        self.controller.start()
        self.until(lambda: self.controller.connected and self.controller.worker is None)
        opens = [args for op, args in self.calls if op == 'open']
        self.assertEqual(len(opens), 2)
        self.assertEqual(opens[0]['nonce'], opens[1]['nonce'])
        self.assertFalse(any(op == 'credential' for op, _ in self.calls))

    def test_open_failures_stop_after_three_attempts_with_clear_message(self):
        self.fail_open = 100
        self.controller.start()
        self.until(lambda: sum(op == 'open' for op, _ in self.calls) == 3 and self.controller.worker is None)
        self.assertFalse(self.controller.connected)
        self.assertFalse(self.controller.retry_timer.isActive())
        self.assertIn('重新打开 Lulu', self.errors[-1])
        self.controller._tick()
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 3)

    def test_exit_cancels_scheduled_retry(self):
        self.fail_open = 1
        self.controller.start()
        self.until(lambda: self.controller.worker is None)
        self.assertTrue(self.controller.retry_timer.isActive())
        self.controller.request_stop()
        self.assertFalse(self.controller.retry_timer.isActive())
        self.controller.retry_timer.timeout.emit()
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 1)

    def test_one_heartbeat_failure_is_tolerated_three_trigger_recovery_without_replay(self):
        self.controller.start()
        self.until(lambda: self.controller.connected and self.controller.worker is None)
        self.controller.timer.stop()
        self.fail_heartbeat = 3
        for index in range(2):
            self.controller.call('heartbeat')
            self.until(lambda: self.controller.worker is None)
            self.assertTrue(self.controller.connected)
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        def delayed(op, args):
            if op == 'heartbeat':
                entered.set()
                release.wait(4)
            return original(op, args)
        self.controller.dispatcher = delayed
        discarded = Mock()
        try:
            self.controller.call('heartbeat')
            self.until(entered.is_set)
            self.controller.call('credential', value='sessionid=synthetic', failure=discarded)
            release.set()
            self.until(lambda: self.controller.worker is None)
        finally:
            release.set()
            self.controller.dispatcher = original
        self.assertFalse(self.controller.connected)
        discarded.assert_called_once_with()
        self.until(lambda: self.controller.connected and self.controller.worker is None)
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 2)
        self.assertFalse(any(op in {'credential', 'start', 'add'} for op, _ in self.calls))

    def test_exit_during_failed_open_finishes_without_reopening(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(op, args):
            self.calls.append((op, args))
            entered.set()
            release.wait(4)
            raise LiveError('injected failed open during exit')
        self.controller.dispatcher = delayed
        try:
            self.controller.start()
            self.until(entered.is_set)
            self.controller.request_stop()
            release.set()
            self.until(lambda: self.controller.settled and self.controller.worker is None)
            self.assertFalse(self.controller.retry_timer.isActive())
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set()

    def test_reconnect_stops_if_open_succeeds_but_heartbeats_never_recover(self):
        self.fail_heartbeat = 100
        self.controller.start()
        for cycle in range(3):
            self.until(lambda: self.controller.connected and self.controller.worker is None)
            self.controller.timer.stop()
            # Opening a session already attempted the first heartbeat.
            for _ in range(2):
                self.controller.call('heartbeat')
                self.until(lambda: self.controller.worker is None)
            self.assertFalse(self.controller.connected)
        self.assertFalse(self.controller.retry_timer.isActive())
        self.assertIn('重新打开 Lulu', self.errors[-1])
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 3)


class LoginBoundaryTests(previous.HiddenLoginStateTests):
    def test_browser_retry_ignores_live_connectivity_and_activity_but_not_real_conflicts(self):
        self.controller.connected = False
        self.controller.active = True
        self.page.pending_hosts = {'fixture': True}
        self.assertEqual(self.page.collection_retry_blocked(), '')
        self.page._credential_pending = True
        self.assertIn('保存', self.page.collection_retry_blocked())
        self.page._credential_pending = False
        self.controller.closing = True
        self.assertTrue(self.page.collection_retry_blocked())

    def test_save_failure_retains_specific_core_reason_and_legacy_callback_stays_compatible(self):
        self.page.refresh = lambda: None
        reason = '请先暂停全部直播监控，再更换或清除登录'
        self.controller.dispatcher = Mock(side_effect=LiveError(reason))
        messages = []
        self.page._save_credential('sessionid=synthetic', feedback=messages.append)
        self.heartbeat(False)
        self.assertIn(reason, messages[-1])
        legacy = Mock()
        self.controller.call('credential', value='sessionid=synthetic', failure=legacy)
        self.heartbeat(False)
        legacy.assert_called_once_with()
        self.controller.dispatcher = lambda op, args: (
            {'configured': True, 'login': {'cookie': True}} if op == 'credential'
            else {'active': False, 'stopped': False})
        self.page._save_credential('sessionid=synthetic', feedback=messages.append)
        self.heartbeat(False)
        self.assertEqual(messages[-1], '登录信息已保存')

    def test_cancel_is_rejected_after_save_is_dispatched(self):
        release = threading.Event()
        self.page.refresh = lambda: None
        def dispatch(op, args):
            if op == 'credential':
                release.wait(4)
                return {'configured': True, 'login': {'browser': True, 'cookie': True, 'source': 'browser'}}
            return {'active': False, 'stopped': False}
        self.controller.dispatcher = dispatch
        worker = Mock()
        self.controller.login_worker = worker
        self.page._collection_login = True
        messages = []
        try:
            self.page._save_credential('sessionid=synthetic', feedback=messages.append)
            self.assertFalse(self.page.collection_browser_login(None, messages.append, cancel=True))
            self.assertFalse(self.page._login_cancelled)
            worker.requestInterruption.assert_not_called()
            self.assertIn('保存', messages[-1])
        finally:
            release.set()
            self.controller.login_worker = None
            self.heartbeat(False)


# Reuse only the fixtures, not all of the previous class's test methods.
for _name in vars(previous.HiddenLoginStateTests):
    if _name.startswith('test_'):
        setattr(LoginBoundaryTests, _name, None)


class PresenceFailureTests(shared.SharedLoginTests):
    def test_index_read_failure_returns_error_presence_and_keeps_heartbeat_alive(self):
        before = self.service.deadline
        with patch.object(self.service.index, 'setting', side_effect=sqlite3.OperationalError('synthetic')):
            reply = self.service.handle('heartbeat', {'session': self.session})
        self.assertTrue(reply['login']['error'])
        self.assertFalse(reply['stopped'])
        self.assertFalse(self.service.closing)
        self.assertGreaterEqual(self.service.deadline, before)


for _name in vars(shared.SharedLoginTests):
    if _name.startswith('test_'):
        setattr(PresenceFailureTests, _name, None)


class RecoveredCollectionViewTests(previous.CollectionLoginViewTests):
    def offer_retry(self, *, author=''):
        link = 'https://v.douyin.com/fixture-short/'
        self.page.input.setText(link)
        self.page._workspace_link_version += 1
        self.page._run_token += 1
        token = self.page._run_token
        self.page._history_context[token] = (CollectionCommand(link, 'anonymous_latest', limit=3), detect_link(link))
        self.page._active_collection_mode = 'partial'
        running = [True]
        self.fake_worker = SimpleNamespace(isRunning=lambda: running[0])
        self.page._worker = self.fake_worker
        self.page._on_collect_done(replace(CollectResult.failed('permission_denied'), resolved_author_id=author), token)
        running[0] = False
        self.page._on_collect_worker_finished(self.fake_worker, False)
        self.page._publish(force=True)
        self.until(lambda: not self.js('document.getElementById("collectStart").disabled'))
        return link

    def test_disconnected_live_service_does_not_disable_real_403_retry_button(self):
        self.blocked = '登录服务连接尚未就绪，请等待启动准备完成。'
        self.page._browser_retry_blocked = lambda: ''
        self.offer_retry()
        self.assertTrue(self.js('document.getElementById("loginButton").disabled'))
        with patch.object(self.page, '_start_collect') as start:
            self.js('document.getElementById("collectStart").click()')
            self.until(lambda: start.called)
        self.assertEqual(start.call_args.args[0].mode, 'profile_latest')
        self.assertEqual(start.call_args.args[0].limit, 3)

    def test_known_short_link_author_survives_the_real_ui_retry(self):
        link = self.offer_retry(author='MS4w_fixture-author')
        with patch.object(self.page, '_start_collect') as start:
            self.js('document.getElementById("collectStart").click()')
            self.until(lambda: start.called)
        command = start.call_args.args[0]
        self.assertEqual((command.raw_input, command.resolved_author_id, command.limit),
                         (link, 'MS4w_fixture-author', 3))

    def test_saved_login_retry_caption_does_not_require_a_new_login(self):
        self.page._douyin_login_state = lambda: ('已保存浏览器登录', True)
        self.offer_retry()
        self.until(lambda: '用已保存登录重试' in self.js('document.getElementById("collectStart").textContent'))
        self.assertIn('用已保存登录重试', self.page.status.text())
        self.page.hide()
        self.assertNotIn('可点击', self.page.status.text())

    def test_saving_stage_disables_cancel_in_the_real_view(self):
        self.page._browser_login_active = lambda: True
        self.page._browser_login_saving = lambda: True
        self.page._douyin_login_busy = lambda: True
        self.page._publish(force=True)
        self.until(lambda: self.js('document.getElementById("loginButton").textContent') == '正在更新…')
        self.assertTrue(self.js('document.getElementById("loginButton").disabled'))
        self.js('document.getElementById("loginButton").click()')
        self.login.assert_not_called()


for _name in vars(previous.CollectionLoginViewTests):
    if _name.startswith('test_'):
        setattr(RecoveredCollectionViewTests, _name, None)
