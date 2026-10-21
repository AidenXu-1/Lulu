"""Native local-data entry and chart interactions with isolated temporary files."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from src.core.live_recording.contracts import LiveMetrics, Observation
from src.core.live_recording.data import append_sample
from tests import test_live_recording_ui as live_ui_fixtures

class NativeDataTests(unittest.TestCase):
    setUpClass = classmethod(live_ui_fixtures.NativeLiveTests.setUpClass.__func__)
    setUp = live_ui_fixtures.NativeLiveTests.setUp
    tearDown = live_ui_fixtures.NativeLiveTests.tearDown
    until = live_ui_fixtures.NativeLiveTests.until
    completed_segment = live_ui_fixtures.NativeLiveTests.completed_segment

    def opened(self, *, gap=False, empty=False):
        rid, _ = self.completed_segment()
        row = self.service._recording(rid)
        if not empty:
            for i in range(5):
                observation = Observation('network_error') if gap and i == 2 else Observation('live', web_rid='123',
                    metrics=LiveMetrics('测试直播标题', i * 10, str(i * 10), i * 100, i * 1000))
                append_sample(row, {'source': 'https://live.douyin.com/123'}, observation,
                              sampled_at=f'2026-10-09T10:{i * 5:02d}:00+08:00')
        self.page._tab('recordings')
        self.until(lambda: self.page.data['total'] == 1)
        self.page.table.cellWidget(0, 5).findChild(QPushButton).click()
        self.until(lambda: self.page.data_dialog and self.page.data_dialog.isVisible())
        return self.page.data_dialog


    def test_enter_on_chart_does_not_close_or_open_folder_and_metric_description_updates(self):
        dialog = self.opened()
        dialog.chart.setFocus()
        QTest.keyClick(dialog.chart, Qt.Key.Key_Right)
        self.assertIn('在线人数', dialog.chart.accessibleDescription())
        with patch('src.ui.live_data_dialog.QDesktopServices.openUrl') as opened:
            dialog.chart.setFocus()
            QTest.keyClick(dialog.chart, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertTrue(dialog.isVisible())
            opened.assert_not_called()
        dialog.select_metric('like_count')
        self.assertIn('点赞', dialog.chart.accessibleDescription())
        dialog.reject()


    def test_slow_local_data_request_does_not_queue_pause_or_stop_behind_it(self):
        rid, _ = self.completed_segment()
        original = self.controller.dispatcher
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        callbacks = []
        def delayed(operation, arguments):
            if operation == 'data':
                entered.set()
                release.wait(4)
            if operation == 'pause_all': paused.set()
            return original(operation, arguments)
        self.controller.dispatcher = delayed
        try:
            self.controller.call('data', recording_id=rid, callback=callbacks.append)
            self.until(entered.is_set)
            self.controller.call('pause_all')
            self.until(paused.is_set, timeout=1)
            self.controller.request_stop()
            self.until(lambda: self.service.closing)
            self.assertFalse(self.controller.settled)  # Wait for the outstanding read thread before deleting UI.
        finally:
            release.set()
            self.until(lambda: self.controller.worker is None)
        self.until(lambda: self.controller.settled)
        self.assertEqual(callbacks, [])


    def test_failed_refresh_stops_clock_and_successful_retry_restores_it(self):
        from src.core.live_recording.contracts import LiveError
        dialog = self.opened()
        self.until(lambda: self.controller.data_worker is None)
        data = dict(dialog.data, duration=60, clock_running=True, ended=None)
        dialog.update_data(data)
        original = self.controller.dispatcher
        def unavailable(operation, arguments):
            if operation == 'data':
                raise LiveError('隔离的连接中断')
            return original(operation, arguments)
        self.controller.dispatcher = unavailable
        self.page._refresh_data()
        self.until(lambda: bool(self.errors) and self.controller.data_worker is None)
        with patch('src.ui.live_data_dialog.time.monotonic', return_value=dialog.anchor + 300):
            dialog.tick_clock()
        self.assertEqual(dialog.stat_values[0].text(), '00:01:00')
        self.assertFalse(dialog.clock_timer.isActive())
        self.assertIn('最后确认', dialog.notice.text())
        self.controller.dispatcher = original
        dialog.update_data(data)  # Even an identical successful reply restores freshness.
        self.assertTrue(dialog.clock_timer.isActive())
        self.assertFalse(dialog.notice.isVisible())
        with patch('src.ui.live_data_dialog.time.monotonic', return_value=dialog.anchor + 2):
            dialog.tick_clock()
        self.assertEqual(dialog.stat_values[0].text(), '00:01:02')
        dialog.reject()

    def test_repeated_refresh_waits_quietly_for_pending_data_and_keeps_pause_free(self):
        dialog=self.opened()
        self.until(lambda:self.controller.data_worker is None)
        original=self.controller.dispatcher
        entered,release=threading.Event(),threading.Event()
        def delayed(operation,arguments):
            if operation=='data':
                entered.set()
                release.wait(4)
            return original(operation,arguments)
        self.controller.dispatcher=delayed
        try:
            self.page._refresh_data()
            self.until(entered.is_set)
            self.page._refresh_data()
            self.assertEqual(self.errors,[])
            self.controller.call('pause_all')
            self.until(lambda:self.controller.worker is None,timeout=1)
        finally:
            release.set()
            self.until(lambda:self.controller.data_worker is None)
            self.controller.dispatcher=original
            dialog.reject()

    def test_missing_refresh_reply_expires_clock_without_fabricating_duration(self):
        dialog = self.opened()
        dialog.update_data(dict(dialog.data, duration=60, clock_running=True, ended=None))
        with patch('src.ui.live_data_dialog.time.monotonic', return_value=dialog.anchor + 300):
            dialog.tick_clock()
        self.assertEqual(dialog.stat_values[0].text(), '00:01:00')
        self.assertFalse(dialog.clock_timer.isActive())
        self.assertIn('最后确认', dialog.notice.text())
        dialog.reject()
