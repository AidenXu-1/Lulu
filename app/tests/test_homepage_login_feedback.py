"""Cross-page login regressions: fake Core/browser, isolated data, no accounts."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core.collect.application import CollectionCommand
from src.core.collect.contracts import CollectResult
from src.core.collect.history import CollectionHistoryRepository
from src.core.collect.router import detect_link
from src.ui.approved_collection import ApprovedCollection
from src.ui.live_controller import LiveController
from src.ui.live_recording_page import LiveRecordingPage


class HiddenLoginStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.core_active = False
        self.core_login = None
        def dispatch(op, args):
            reply = dict(active=self.core_active, stopped=False)
            if self.core_login is not None:
                reply['login'] = self.core_login
            return reply
        self.controller = LiveController(dispatcher=dispatch)
        self.controller.connected = True
        self.controller.session = 'isolated-session'
        self.page = LiveRecordingPage(self.controller, transcribe=Mock())
        self.page.export_timer.stop()
        self.page.data = {'active': False}
        start_patch = patch.object(self.page, '_start_browser_login')
        self.start = start_patch.start()
        self.addCleanup(start_patch.stop)

    def tearDown(self):
        self.controller.timer.stop()
        self.page.deleteLater()
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def heartbeat(self, active):
        self.core_active = active
        self.controller.call('heartbeat')
        deadline = time.monotonic() + 3
        while self.controller.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.controller.worker)
        self.assertIsNone(self.controller.params)

    def test_hidden_busy_snapshot_cannot_block_login_after_idle_heartbeat(self):
        self.page.data['active'] = True
        self.heartbeat(False)
        self.assertTrue(self.page.data['active'])  # The hidden view remains stale.
        self.assertFalse(self.page._access_busy())
        self.assertTrue(self.page.collection_browser_login('/fixture/Chrome', Mock()))
        self.start.assert_called_once()

    def test_hidden_idle_snapshot_cannot_allow_login_after_busy_heartbeat(self):
        self.heartbeat(True)
        self.assertFalse(self.page.data['active'])
        feedback = Mock()
        self.assertFalse(self.page.collection_browser_login('/fixture/Chrome', feedback))
        self.start.assert_not_called()
        self.assertIn('直播', feedback.call_args.args[0])

    def test_heartbeat_updates_hidden_login_controls_in_both_directions(self):
        self.heartbeat(True)
        self.assertFalse(self.page.browser_login.isEnabled())
        self.heartbeat(False)
        self.assertTrue(self.page.browser_login.isEnabled())

    def test_unavailable_connection_has_its_own_feedback(self):
        self.controller.connected = False
        feedback = Mock()
        self.assertFalse(self.page.collection_browser_login('/fixture/Chrome', feedback))
        self.assertIn('连接', feedback.call_args.args[0])
        self.assertNotIn('采集、监控', feedback.call_args.args[0])
        self.start.assert_not_called()

    def test_hidden_login_presence_tracks_heartbeat_and_does_not_restore_old_cookie(self):
        self.page.data['login'] = {'cookie': True, 'source': 'manual'}
        self.page.data['credential'] = True
        self.core_login = {'browser': True, 'cookie': False, 'profile': True}
        self.heartbeat(False)
        self.assertEqual(self.page.login_state(), ('已保存浏览器登录', True))
        self.core_login = {'browser': False, 'cookie': False, 'profile': False}
        self.heartbeat(False)
        self.assertEqual(self.page.login_state(), ('未登录', False))
        self.assertTrue(self.page.data['credential'])  # Deliberately stale UI cache.

    def test_collection_pending_save_and_closing_still_block_browser(self):
        for kind in ('collection', 'credential', 'closing', 'login'):
            with self.subTest(kind=kind):
                self.page.collection_busy = lambda: kind == 'collection'
                self.page._credential_pending = kind == 'credential'
                self.controller.closing = kind == 'closing'
                self.controller.login_worker = Mock() if kind == 'login' else None
                feedback = Mock()
                self.assertFalse(self.page.collection_browser_login('/fixture/Chrome', feedback))
                self.assertTrue(feedback.call_args.args[0])
                self.start.assert_not_called()
        self.controller.login_worker = None
        self.controller.closing = False

    def test_collection_bridge_preserves_specific_rejection_reason(self):
        self.controller.connected = False
        owner = SimpleNamespace(
            _browser_login=self.page.collection_browser_login,
            _browser_path='/fixture/Chrome', status=SimpleNamespace(setText=Mock()),
            _payload=Mock(),
        )
        replies = []
        ApprovedCollection._login(owner, lambda value=None, **kw: replies.append(kw or value))
        self.assertIn('连接', replies[-1]['message'])
        owner._payload.assert_not_called()
        self.start.assert_not_called()


class CollectionLoginViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.fake_worker = None
        self.blocked = ''
        self.login = Mock(return_value=True)
        cfg = AppConfig(auto_update_enabled=False)
        with patch('src.ui.collection_page.is_logged_in', side_effect=AssertionError('real login lookup')):
            self.page = ApprovedCollection(
                cfg, history_repository=CollectionHistoryRepository(Path(self.temp.name) / 'records.json'),
                douyin_login_state=lambda: ('未登录', False), browser_login=self.login,
                browser_login_blocked=lambda: self.blocked,
                browser_retry_blocked=lambda: self.blocked,
            )
        self.page.resize(1200, 760)
        self.page.show()
        self.until(lambda: self.page.approved.bridge.is_ready)

    def tearDown(self):
        if self.fake_worker is not None and self.page._worker is self.fake_worker:
            self.page._worker = None
        self.page.shutdown_workers()
        self.page.hide()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('isolated collection view did not settle')

    def js(self, code):
        answers = []
        self.page.approved.page().runJavaScript(code, answers.append)
        self.until(lambda: bool(answers))
        return answers[0]

    def test_real_view_disables_login_with_reason_then_recovers_without_navigation(self):
        self.blocked = '直播任务正在运行，请先暂停监控或等待保存结束，再登录。'
        self.page._publish(force=True)
        self.until(lambda: self.js('document.getElementById("loginButton").disabled'))
        self.assertEqual(self.js('document.getElementById("loginButton").title'), self.blocked)
        self.assertIn(self.blocked, self.js('document.getElementById("settingsPopover").textContent'))
        self.blocked = ''
        self.page._publish(force=True)
        self.until(lambda: not self.js('document.getElementById("loginButton").disabled'))
        self.js('document.getElementById("loginButton").click()')
        self.until(lambda: self.login.called)
        self.assertEqual(self.login.call_count, 1)

    def test_partial_login_retry_cannot_start_while_login_changes_are_blocked(self):
        self.blocked = '请先结束当前主页采集，再登录。'
        replies = []
        with patch.object(self.page, '_on_partial_login_retry') as retry:
            self.page._retry_partial(lambda value=None, **kw: replies.append(kw or value), link=self.page.input.text())
        retry.assert_not_called()
        self.assertEqual(replies[-1]['message'], self.blocked)

    def test_active_login_remains_cancellable_even_when_other_controls_are_blocked(self):
        self.page._browser_login_active = lambda: True
        self.page._douyin_login_busy = lambda: True
        self.blocked = '正在登录，请先完成或取消登录。'
        self.page._publish(force=True)
        self.until(lambda: self.js('document.getElementById("loginButton").textContent') == '取消登录')
        self.assertFalse(self.js('document.getElementById("loginButton").disabled'))
        self.js('document.getElementById("loginButton").click()')
        self.until(lambda: self.login.called)
        self.assertTrue(self.login.call_args.kwargs['cancel'])

    def test_403_ui_retry_keeps_original_author_small_scope_and_single_explicit_start(self):
        link = 'https://www.douyin.com/user/MS4w_feedback-author'
        command = CollectionCommand(link, 'anonymous_latest', limit=3)
        self.page.input.setText(link)
        self.page._workspace_link_version += 1
        self.page._run_token += 1
        token = self.page._run_token
        self.page._history_context[token] = (command, detect_link(link))
        self.page._active_collection_mode = 'partial'
        running = [True]
        worker = SimpleNamespace(isRunning=lambda: running[0])
        self.fake_worker = worker
        self.page._worker = worker
        self.page._on_collect_done(CollectResult.failed('permission_denied'), token)
        self.page._publish(force=True)
        self.until(lambda: self.js('document.getElementById("collectStart").disabled'))
        self.login.assert_not_called()
        running[0] = False
        self.page._on_collect_worker_finished(worker, False)
        self.page._publish(force=True)
        self.until(lambda: self.js('document.getElementById("collectStart").textContent.includes("登录后部分采集")'))
        self.until(lambda: not self.js('document.getElementById("collectStart").disabled'))
        with patch.object(self.page, '_start_collect') as start:
            self.js('document.getElementById("collectStart").click(); document.getElementById("collectStart").click()')
            self.until(lambda: start.called)
            self.assertEqual(start.call_count, 1)
        retry = start.call_args.args[0]
        self.assertEqual((retry.raw_input, retry.mode, retry.limit), (link, 'profile_latest', 3))
        self.assertFalse(start.call_args.kwargs['full'])
        self.assertIsNone(self.page._partial_login_retry)
        self.login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
