"""Regression checks for real native click, pending-state and login boundaries."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from src.core.live_recording.contracts import LiveError
from src.ui.live_login import LiveLoginRequest
from tests.test_live_recording_ui import NativeLiveTests


class WorkspaceInteractionTests(NativeLiveTests):
    # Reuse only fixture helpers, not the parent's complete test collection.
    def steady(self):
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None and not self.controller.queue)

    def test_held_click_survives_refresh_and_follows_new_monitor_state(self):
        self.seed(hosts=2, recordings=0)
        self.steady()
        original = self.controller.dispatcher
        entered, release = threading.Event(), threading.Event()
        calls = []
        def delayed(operation, args):
            if operation == 'start':
                calls.append(operation)
                entered.set()
                release.wait(4)
            return original(operation, args)
        self.controller.dispatcher = delayed
        button = self.page.table.cellWidget(0, 6).findChild(QPushButton)
        try:
            QTest.mousePress(button, Qt.MouseButton.LeftButton)
            self.page.render(copy.deepcopy(self.page.data))
            self.assertIs(self.page.table.cellWidget(0, 6).findChild(QPushButton), button)
            QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
            self.until(entered.is_set)
            self.assertEqual(button.text(), '启动中…')
            self.assertFalse(button.isEnabled())
            button.click()
            self.page.render(copy.deepcopy(self.page.data))
            self.assertEqual(button.text(), '启动中…')
            self.assertEqual(calls, ['start'])
        finally:
            release.set()
        self.until(lambda: button.text() == '暂停监控' and not self.page.pending_hosts)
        self.assertIs(self.page.table.cellWidget(0, 6).findChild(QPushButton), button)
        button.click()
        self.until(lambda: button.text() == '开始监控' and not self.page.pending_hosts)
        self.assertFalse(self.service.busy())

    def test_row_membership_refresh_waits_for_mouse_release(self):
        self.seed(hosts=2, recordings=0)
        self.steady()
        button = self.page.table.cellWidget(0, 6).findChild(QPushButton)
        second = self.page.data['rows'][1]['id']
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        self.service.index.write('DELETE FROM hosts WHERE id=?', (second,))
        fresh = self.service.snapshot(**self.controller.params)
        self.page.render(fresh)
        self.assertEqual(self.page.table.rowCount(), 2)
        self.assertIs(self.page.table.cellWidget(0, 6).findChild(QPushButton), button)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
        self.until(lambda: self.page.table.rowCount() == 1 and not self.page.pending_hosts)
        self.assertTrue(self.page.data['rows'][0]['enabled'])

    def test_comments_stale_snapshot_failure_and_retry(self):
        self.service.index.set_setting('comments_enabled', '0')
        self.page.refresh()
        self.until(lambda: not self.page.comments_toggle.isChecked())
        self.steady()
        old = copy.deepcopy(self.page.data)
        original = self.controller.dispatcher
        entered, release = threading.Event(), threading.Event()
        def fail(operation, args):
            if operation == 'comments_setting':
                entered.set()
                release.wait(4)
                raise LiveError('隔离设置保存失败')
            return original(operation, args)
        self.controller.dispatcher = fail
        try:
            self.page.comments_toggle.click()
            self.until(entered.is_set)
            self.page.render(old)
            self.assertTrue(self.page.comments_toggle.isChecked())
            self.assertFalse(self.page.comments_toggle.isEnabled())
            self.assertEqual(self.service.index.setting('comments_enabled'), '0')
        finally:
            release.set()
        self.until(lambda: self.page.pending_comments is None and self.errors)
        self.assertFalse(self.page.comments_toggle.isChecked())
        self.assertTrue(self.page.comments_toggle.isEnabled())
        self.controller.dispatcher = original
        self.page.comments_toggle.click()
        self.until(lambda: self.page.pending_comments is None and self.page.comments_toggle.isChecked())
        self.assertEqual(self.service.index.setting('comments_enabled'), '1')

    def test_poll_coalescing_action_priority_and_current_view(self):
        self.steady()
        original = self.controller.dispatcher
        entered, release = threading.Event(), threading.Event()
        def slow(operation, args):
            if operation == 'snapshot':
                result = original(operation, args)
                entered.set()
                release.wait(4)
                return result
            return original(operation, args)
        self.controller.dispatcher = slow
        self.controller._enqueue('snapshot', {'session': self.controller.session, **self.controller.params})
        self.until(entered.is_set)
        try:
            self.controller.poll_at = 0
            self.controller.heartbeat_at = time.monotonic()
            for _ in range(5):
                self.controller._tick()
            self.assertFalse(self.controller.queue)
            for query in ['first', 'second', 'latest']:
                self.controller.view(tab='hosts', query=query, filter='all', page=1)
            self.controller.call('pause_all')
            self.assertEqual([item[0] for item in self.controller.queue], ['pause_all', 'snapshot'])
            self.assertEqual(self.controller.queue[-1][1]['query'], 'latest')
        finally:
            release.set()
        self.until(lambda: self.controller.worker is None and not self.controller.queue)

    def test_reconnect_releases_pending_choice_without_replaying_mutation(self):
        self.steady()
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        before = self.page.comments_toggle.isChecked()
        calls = []
        def slow(operation, args):
            if operation == 'comments_setting':
                calls.append(operation)
                entered.set()
                release.wait(4)
                raise LiveError('隔离旧连接已结束')
            return original(operation, args)
        self.controller.dispatcher = slow
        try:
            self.page.comments_toggle.click()
            self.until(entered.is_set)
            self.controller.reconnect()
            self.assertIsNone(self.page.pending_comments)
            self.assertEqual(self.page.comments_toggle.isChecked(), before)
        finally:
            release.set()
        self.until(lambda: self.controller.connected and self.controller.worker is None)
        self.assertEqual(calls, ['comments_setting'])

    def test_cancelled_login_cannot_save_late_cookie_and_stop_waits(self):
        self.steady()
        entered, release = threading.Event(), threading.Event()
        writes = []
        self.service.secret_write = writes.append
        class FakeLogin(LiveLoginRequest):
            def run(worker):
                entered.set()
                release.wait(4)
                worker.received.emit('sessionid=isolated-fixture')
        with patch('src.ui.live_login.LiveLoginRequest', FakeLogin):
            self.page._settings()
            self.page.browser_login.click()
            self.until(entered.is_set)
            try:
                from src.ui.main_window import MainWindow
                self.assertTrue(self.controller.has_pending_work)
                self.assertTrue(MainWindow.has_active_work(SimpleNamespace(live_controller=self.controller)))
                self.page.credentials.reject()
                self.controller.request_stop()
                self.until(lambda: self.controller.core_stopped)
                self.assertFalse(self.controller.settled)
            finally:
                release.set()
            self.until(lambda: self.controller.settled)
        self.assertEqual(writes, [])
        self.assertFalse(self.service.index.setting('credential'))

    def test_browser_login_saves_only_after_valid_result_and_keeps_cookie_hidden(self):
        self.steady()
        writes = []
        self.service.secret_write = writes.append
        class FakeLogin(LiveLoginRequest):
            def run(worker):
                worker.received.emit('sessionid=isolated-fixture')
        with patch('src.ui.live_login.LiveLoginRequest', FakeLogin):
            self.page._settings()
            self.page.browser_login.click()
            self.until(lambda: self.page._configured and not self.controller.has_pending_work)
        self.assertEqual(writes, ['sessionid=isolated-fixture'])
        self.assertEqual(self.service.index.setting('credential'), 'configured')
        self.assertEqual(self.page.credential.text(), '')
        self.assertNotIn('isolated-fixture', self.page.credential_status.text())
        self.assertEqual('已登录', self.page.connection_title.text())
        self.page.credentials.reject()

    def test_login_store_failure_does_not_claim_connection_and_allows_retry(self):
        self.steady()
        self.service.secret_write = Mock(side_effect=RuntimeError('isolated-secret-fixture'))
        class FakeLogin(LiveLoginRequest):
            def run(worker):
                worker.received.emit('sessionid=isolated-fixture')
        with patch('src.ui.live_login.LiveLoginRequest', FakeLogin):
            self.page._settings()
            self.page.browser_login.click()
            self.until(lambda: self.errors and not self.controller.has_pending_work)
        self.assertFalse(self.page._configured)
        self.assertFalse(self.service.index.setting('credential'))
        self.assertTrue(self.page.browser_login.isEnabled())
        self.until(lambda: self.page.connection_title.text() == '登录状态待检查')
        self.assertNotIn('isolated-secret', self.page.credential_status.text())
        self.page.credentials.reject()

    def test_small_access_card_scrolls_and_escape_restores_focus(self):
        self.page.resize(940, 640)
        self.page.settings_button.setFocus()
        self.page._settings()
        self.page.advanced_toggle.click()
        self.page._toggle_cookie_help()
        self.app.processEvents()
        card = self.page.credentials.card
        self.assertGreaterEqual(card.geometry().top(), 0)
        self.assertLessEqual(card.geometry().bottom(), self.page.height())
        self.assertLessEqual(self.page.save_credential.mapTo(self.page, self.page.save_credential.rect().bottomRight()).y(), self.page.height())
        self.assertTrue(self.page.save_credential.isVisible())
        self.page.credential.setText('unsaved-isolated-fixture')
        QTest.keyClick(self.page.credentials.close_button, Qt.Key.Key_Escape)
        self.assertFalse(self.page.credentials.isVisible())
        self.assertEqual(self.page.credential.text(), '')
        self.assertTrue(self.page.settings_button.hasFocus())

    def test_acknowledged_start_keeps_button_progress_and_total_consistent(self):
        self.seed(hosts=1, recordings=0)
        self.steady()
        started, entered, release = threading.Event(), threading.Event(), threading.Event()
        original = self.controller.dispatcher
        def delayed_view(operation, args):
            result = original(operation, args)
            if operation == 'start':
                started.set()
            if operation == 'snapshot' and started.is_set():
                entered.set()
                release.wait(4)
            return result
        self.controller.dispatcher = delayed_view
        control = self.page.table.cellWidget(0, 6).findChild(QPushButton)
        try:
            control.click()
            self.until(entered.is_set)
            self.assertEqual(control.text(), '暂停监控')
            self.assertTrue(control.isEnabled())
            self.assertIn('监控中', self.page.table.item(0, 4).text())
            self.assertEqual(self.page.stats[0].text(), '1 位')
        finally:
            release.set()
        self.until(lambda: self.controller.worker is None)

    def test_pause_all_is_guarded_when_filter_hides_every_active_host(self):
        self.seed(hosts=1, recordings=0)
        self.steady()
        hid = self.page.data['rows'][0]['id']
        self.controller.call('start', host_id=hid, callback=lambda _: self.page.refresh())
        self.until(lambda: self.page.data['monitoring'] == 1)
        self.page.host_filter.select('live', '直播中')
        self.until(lambda: not self.page.data['rows'])
        self.steady()
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        calls = []
        def slow(operation, args):
            if operation == 'pause_all':
                calls.append(operation)
                entered.set()
                release.wait(4)
            return original(operation, args)
        self.controller.dispatcher = slow
        try:
            self.page.pause_all.click()
            self.until(entered.is_set)
            self.assertEqual(self.page.pause_all.text(), '暂停中…')
            self.assertFalse(self.page.pause_all.isEnabled())
            self.page.pause_all.click()
            self.page.pause_all.click()
            self.assertEqual(calls, ['pause_all'])
            self.assertFalse(any(item[0] == 'pause_all' for item in self.controller.queue))
        finally:
            release.set()
        self.until(lambda: self.page.data['monitoring'] == 0 and not self.page._pausing_all)


    def test_manual_cookie_status_and_clear_confirmation_are_shared(self):
        self.steady()
        self.page._settings()
        self.page._toggle_advanced()
        self.page.credential.setText('sessionid=isolated')
        self.page.save_credential.click()
        self.until(lambda: self.page.login_state()[0] == '已保存 Cookie' and not self.controller.has_pending_work)
        self.assertFalse(self.page.data['login']['browser'])
        self.page._credential(delete=True)
        self.assertTrue(self.service.index.setting('credential'))
        self.assertIn('主页采集和直播录制', self.page.access_description.text())
        self.page._set_access_view('main')
        self.assertTrue(self.service.index.setting('credential'))
        self.page.collection_busy = lambda: True
        self.page._access_controls()
        self.assertFalse(self.page.delete_credential.isEnabled())
        self.assertFalse(self.page.browser_login.isEnabled())
        self.page.credentials.reject()


# Unittest otherwise inherits every test from NativeLiveTests a second time.
for _name in tuple(vars(NativeLiveTests)):
    if _name.startswith('test_') and _name not in WorkspaceInteractionTests.__dict__:
        setattr(WorkspaceInteractionTests, _name, None)
del NativeLiveTests
