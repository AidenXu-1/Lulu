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

    def test_only_archive_adds_entry_and_folder_opens_actual_csv_location(self):
        dialog = self.opened()
        self.assertEqual(self.page.table.horizontalHeaderItem(5).text(), '直播数据')
        self.assertEqual(dialog.data['sample_count'], 5)
        with patch('src.ui.live_data_dialog.QDesktopServices.openUrl', return_value=True) as opened:
            dialog.folder_button.click()
        self.assertEqual(opened.call_args.args[0].toLocalFile(), dialog.data['folder'])
        self.assertTrue((Path(dialog.data['folder'])/'直播数据.csv').is_file())
        dialog.reject()
        self.assertFalse(self.page.data_timer.isActive())
        self.page._tab('hosts')
        self.until(lambda: self.page.tab == 'hosts' and self.page.table.horizontalHeaderItem(5).text() == '直播间')

    def test_gap_zero_metric_switch_and_keyboard_inspection(self):
        dialog = self.opened(gap=True)
        dialog.chart.grab()
        self.assertEqual(dialog.chart.path_count, 2)
        self.assertEqual(dialog.stat_values[1].text(), '40')
        self.assertIsNotNone(dialog.chart.coordinates[0])  # Zero is a real point.
        dialog.buttons['like_count'].click()
        dialog.chart.grab()
        self.assertEqual(dialog.metric_title.text(), '点赞')
        self.assertIn('重复点赞', dialog.definition.text())
        dialog.chart.setFocus()
        QTest.keyClick(dialog.chart, Qt.Key.Key_Right)
        self.assertEqual(dialog.chart.selected, 1)
        QTest.keyClick(dialog.chart, Qt.Key.Key_Right)
        self.assertIsNone(dialog.chart.samples[dialog.chart.selected]['like_count'])
        QTest.mouseMove(dialog.chart, dialog.chart.plot.center().toPoint())
        self.assertEqual(dialog.chart.selected, 2)
        dialog.buttons['cumulative_view_count'].click()
        self.assertIn('非去重人数', dialog.definition.text())
        dialog.reject()

    def test_total_platform_text_is_visible_and_distinct_from_zero_and_missing(self):
        dialog = self.opened(empty=True)
        row = self.service._recording(dialog.recording_id)
        for minute, value in ((0, LiveMetrics(cumulative_view_count_raw='6万+')),
                              (5, LiveMetrics(cumulative_view_count=0)),
                              (10, LiveMetrics())):
            append_sample(row, {'source': 'https://live.douyin.com/123'},
                Observation('live', web_rid='123', metrics=value),
                sampled_at=f'2026-10-09T10:{minute:02d}:00+08:00')
        data = self.service.handle('data', {'session': self.controller.session, 'recording_id': row['id']})
        dialog.update_data(data)
        dialog.buttons['cumulative_view_count'].click()
        dialog.chart.grab()
        self.assertEqual(dialog.records_label.text(), '1 条精确记录 · 1 条平台原文 · 1 条缺失')
        self.assertIsNone(dialog.chart.coordinates[0])  # No invented 60,000 point.
        self.assertIsNotNone(dialog.chart.coordinates[1])  # A real zero.
        dialog.chart.selected = 0
        dialog.chart._accessible_sample()
        self.assertIn('6万+', dialog.chart.accessibleDescription())
        dialog.chart.selected = 2
        dialog.chart._accessible_sample()
        self.assertIn('未返回数据', dialog.chart.accessibleDescription())
        dialog.update_data({**data, 'samples':data['samples'][:1]})
        dialog.chart.selected = None
        dialog.chart.grab()
        self.assertEqual(dialog.chart.path_count, 0)
        self.assertEqual(dialog.records_label.text(), '0 条精确记录 · 1 条平台原文')
        dialog.reject()

    def test_short_window_keeps_chart_and_folder_visible_in_one_heading_row(self):
        dialog = self.opened()
        for width, height in ((960, 680), (800, 540), (560, 540)):
            dialog.resize(width, height)
            self.app.processEvents()
            self.assertLessEqual(dialog.height(), height)
            self.assertTrue(dialog.folder_button.isVisible())
            bottom = dialog.folder_button.mapTo(dialog, dialog.folder_button.rect().bottomRight())
            self.assertLess(bottom.y(), dialog.height())
            centers = [widget.mapTo(dialog, widget.rect().center()).y() for widget in
                       (dialog.metric_title, dialog.definition, dialog.buttons['cumulative_view_count'])]
            self.assertLessEqual(max(centers) - min(centers), 1)
            self.assertGreaterEqual(dialog.chart.height(), 100)
        dialog.reject()

    def test_empty_has_no_fake_file_and_local_refresh_never_reopens_closed_dialog(self):
        dialog = self.opened(empty=True)
        self.assertFalse(dialog.folder_button.isEnabled())
        self.assertIn('暂无数据', dialog.file_label.text())
        self.assertEqual(dialog.chart.samples, [])
        callbacks = []
        with patch.object(self.controller, 'call', side_effect=lambda operation, **kwargs: callbacks.append(kwargs['callback'])):
            self.until(lambda: self.controller.worker is None)
            self.page._refresh_data()
        self.assertEqual(len(callbacks), 1)
        dialog.reject()
        with patch.object(dialog, 'update_data') as updated:
            callbacks[0](dict(dialog.data, sample_count=123))
            updated.assert_not_called()
        self.assertFalse(dialog.isVisible())

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

    def test_axis_ticks_are_distinct_and_fit_for_small_and_large_counts(self):
        import src.ui.live_data_dialog as module
        dialog = self.opened()
        painter_type = module.QPainter
        labels = []
        class Painter:
            RenderHint = painter_type.RenderHint
            def __init__(self, widget): self.delegate = painter_type(widget)
            def __del__(self): self.delegate.end()
            def __getattr__(self, name): return getattr(self.delegate, name)
            def drawText(self, rect, flags, value):
                if value.replace(',', '').isdigit():
                    labels.append((value, self.delegate.fontMetrics().horizontalAdvance(value), rect.width()))
                return self.delegate.drawText(rect, flags, value)
        for count in (1, 10**15):
            labels.clear()
            dialog.chart.set_data([dict(point, viewer_count=count) for point in dialog.chart.samples], 'viewer_count')
            with patch.object(module, 'QPainter', Painter): dialog.chart.grab()
            self.assertEqual(len(set(value for value, _, _ in labels)), 5)
            self.assertTrue(all(width <= available for _, width, available in labels), labels)
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

    def test_unreadable_csv_is_not_presented_as_zero_collected_records(self):
        dialog = self.opened()
        path = Path(dialog.data['folder'])/'直播数据.csv'
        path.write_text('unrecognized header\n', encoding='utf-8')
        self.page._refresh_data()
        self.until(lambda: bool(dialog.data.get('data_error')))
        self.assertEqual(dialog.stat_values[3].text(), '—')
        self.assertNotIn('0 条', dialog.records_label.text())
        self.assertIn('无法读取', dialog.chart.empty_message)
        self.assertEqual(path.read_text(), 'unrecognized header\n')
        self.assertTrue(dialog.folder_button.isEnabled())
        dialog.reject()

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

    def test_missing_refresh_reply_expires_clock_without_fabricating_duration(self):
        dialog = self.opened()
        dialog.update_data(dict(dialog.data, duration=60, clock_running=True, ended=None))
        with patch('src.ui.live_data_dialog.time.monotonic', return_value=dialog.anchor + 300):
            dialog.tick_clock()
        self.assertEqual(dialog.stat_values[0].text(), '00:01:00')
        self.assertFalse(dialog.clock_timer.isActive())
        self.assertIn('最后确认', dialog.notice.text())
        dialog.reject()
