"""Native page and GUI lease checks with a temporary, credential-free Core."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import time
import threading
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFontMetrics, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.live_recording.contracts import LiveError, Observation
from src.core.live_recording.service import LiveService, uid
from src.core.live_recording.media import bundled_ffmpeg, probe
from src.ui.live_controller import LiveController
from src.ui.live_recording_page import LiveRecordingPage, stamp
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from tests.test_live_recording_delete import register_recording

class NativeLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        control = JobControlService(JobLedgerStore(self.root/'ledger', FakeIntegrityAnchorStore()))
        self.service = LiveService(self.root, control, 'fixture',
            provider=SimpleNamespace(check=lambda host, key: Observation('offline', name='本地主播')),
            secret_read=lambda: self.fail('passive credential access'), secret_write=lambda _: None,
            disk_free=lambda p: 100*1024**3)
        self.output = self.root/'recordings'
        self.output.mkdir()
        self.service.index.set_setting('directory', str(self.output))
        self.controller = LiveController(dispatcher=self.service.handle)
        self.errors = []
        self.controller.error.connect(self.errors.append)
        self.controller.start()
        self.until(lambda: self.controller.connected)
        self.page = LiveRecordingPage(self.controller, transcribe=lambda _: self.fail('unexpected transcription'))
        self.page.resize(1240, 760)
        self.page.show()
        self.until(lambda: self.page.data)

    def until(self, predicate, timeout=7):
        limit = time.monotonic()+timeout
        while time.monotonic()<limit:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.01)
        self.fail('native live operation did not settle: '+repr(self.errors))

    def tearDown(self):
        self.page.hide()
        self.page.export_timer.stop()
        self.controller.request_stop()
        self.until(lambda: self.controller.settled and self.controller.worker is None)
        self.service.shutdown()
        self.page.deleteLater()
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def seed(self, hosts=30, recordings=120):
        now = time.time()
        for number in range(hosts):
            self.service.index.write('INSERT INTO hosts(id,source,name,created,status) VALUES(?,?,?,?,?)',
                (uid('host'), f'https://live.douyin.com/{1000+number}', f'测试主播 {number:02d}', now-number, 'paused'))
        for number in range(recordings):
            self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended,reason) VALUES(?,?,?,?,?,?,?,?)',
                (uid('recording'), 'fixture', f'测试主播 {number:03d}', uid('live'), str(self.root/f'file{number}'),
                 now-number*100, now-number*100+50, 'offline'))
        self.page.refresh()
        self.until(lambda: self.page.data['hosts']==hosts)

    def test_idle_core_restart_refreshes_session_and_normal_stop_settles(self):
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None)
        previous = self.controller.session
        self.service.shutdown()
        control = JobControlService(JobLedgerStore(self.root/'restart-ledger', FakeIntegrityAnchorStore()))
        self.service = LiveService(self.root/'restart', control, 'fixture',
            provider=SimpleNamespace(check=lambda *_: Observation('offline')),
            secret_read=lambda: self.fail('restart credential access'))
        self.controller.dispatcher = self.service.handle
        self.controller.reconnect()
        self.until(lambda: self.controller.connected and self.controller.session != previous)
        self.until(lambda: not self.controller.active)
        self.assertEqual(self.service.control.list(), ())
        self.controller.request_stop()
        self.until(lambda: self.controller.settled and self.controller.worker is None)
        self.assertEqual(self.errors, [])

    def test_reconnect_discards_inflight_old_reply_and_never_replays_queued_start(self):
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None)
        entered, release = threading.Event(), threading.Event()
        calls = []
        old = self.controller.session
        def delayed(operation, arguments):
            calls.append((operation, arguments.copy()))
            if operation == 'heartbeat' and arguments['session'] == old:
                entered.set()
                if not release.wait(5): raise AssertionError('old reply was not released')
                return {'active': True, 'stopped': False}
            return self.service.handle(operation, arguments)
        self.controller.dispatcher = delayed
        self.controller._enqueue('heartbeat', {'session': old})
        self.until(entered.is_set)
        self.controller._enqueue('start', {'session': old, 'host_id': uid('host')})
        self.controller.reconnect()
        self.assertFalse(self.controller.connected)
        release.set()
        self.until(lambda: self.controller.connected)
        self.until(lambda: self.controller.worker is None and not self.controller.active)
        self.assertFalse(any(operation == 'start' for operation, _ in calls))
        self.assertEqual(sum(operation == 'open' for operation, _ in calls), 1)
        self.assertEqual(self.errors, [])

    def test_recording_clock_ticks_without_requesting_a_new_snapshot(self):
        self.seed(hosts=1, recordings=0)
        self.controller.timer.stop()
        self.until(lambda:self.controller.worker is None)
        value = self.page.data
        value['rows'][0].update(status='live', live_state='live', message='直播中', progress_message='正在录制', duration=11,
                                clock_running=True, bytes=1000)
        self.page.render(value)
        calls = Mock()
        with patch.object(self.controller, 'view', calls):
            self.until(lambda:any(self.page.table.item(0,c) and self.page.table.item(0,c).text().split('\n')[0] in
                                  {'00:00:12','00:00:13','00:00:14','00:00:15','00:00:16'}
                                  for c in range(self.page.table.columnCount())), timeout=4)
        calls.assert_not_called()
        with patch('src.ui.live_recording_page.time.monotonic', return_value=self.page.clock_anchor + 30):
            self.page._tick_durations()
        self.assertEqual(self.page.table.item(0, 4).text(), '00:00:16\n正在录制')

    def test_host_broadcast_column_has_only_three_labels_and_keeps_errors_with_progress(self):
        self.seed(hosts=3, recordings=0)
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None)
        for row, state in zip(self.page.data['rows'], ('live', 'offline', 'access_limited')):
            self.service._remember_observation(row['id'], state)
            self.service._status(row['id'], 'stopping')
        self.page.render(self.service.snapshot('hosts', '', 'all', 1))
        self.assertEqual(self.page.table.horizontalHeaderItem(2).text(), '直播状态')
        self.assertEqual(self.page.table.horizontalHeaderItem(4).text(), '录制进度')
        self.assertEqual([self.page.table.item(row, 2).text() for row in range(3)], ['直播中', '未直播', '无法确认'])
        self.assertNotIn('访问受限', self.page.table.item(2, 2).toolTip())
        self.assertIn('访问受限', self.page.table.item(2, 4).toolTip())
        self.assertEqual(self.page.host_filter.choices,
                         [('全部状态', 'all'), ('直播中', 'live'), ('未直播', 'offline'), ('无法确认', 'unknown')])

    def test_fixed_native_layout_half_toolbar_and_archive_pagination(self):
        original = self.page.size()
        self.seed()
        self.assertEqual(len(self.page.stats), 3)
        self.assertEqual(self.page.size(), original)
        self.assertEqual(self.page.table.rowCount(),20)
        self.assertTrue(self.page.table.verticalScrollBar().maximum()>0)
        self.assertEqual(self.page.table.columnWidth(0), 44)
        QTest.mouseClick(self.page.table.viewport(), Qt.MouseButton.LeftButton,
                        pos=self.page.table.visualItemRect(self.page.table.item(0, 1)).center())
        self.assertFalse(self.page.table.selectedItems())
        self.assertEqual(self.page.table.horizontalHeader().defaultAlignment(), Qt.AlignmentFlag.AlignCenter)
        self.assertLess(self.page.table.columnWidth(2), self.page.table.columnWidth(1))
        controls = self.page.table.cellWidget(0, 6).findChildren(QPushButton)
        self.assertEqual(len(controls), 1)
        self.assertGreaterEqual(controls[0].width(), controls[0].fontMetrics().horizontalAdvance(controls[0].text()) + 24)
        for column in (1, 2, 3, 4):
            self.assertEqual(self.page.table.item(0, column).textAlignment(), Qt.AlignmentFlag.AlignCenter)
        box = self.page.table.cellWidget(0, 6)
        self.assertLessEqual(abs(controls[0].geometry().center().x()-box.rect().center().x()), 1)
        toolbar = self.page.toolbars.widget(0).layout()
        self.assertLessEqual(abs(toolbar.itemAt(0).widget().width()-toolbar.itemAt(1).widget().width()),1)
        self.page._tab('recordings')
        self.until(lambda: self.page.data['total']==120)
        first = self.page.data['rows'][0]['id']
        self.service.index.write('UPDATE recordings SET gaps=? WHERE id=?', (json.dumps([[time.time()-5,time.time()]]), first))
        self.page.refresh()
        self.until(lambda:self.page.table.item(0,4).text()=='录制有缺口')
        self.assertFalse(any(control.text().startswith('录制有缺口') for control in self.page.findChildren(QPushButton)))
        self.assertEqual((self.page.table.rowCount(),self.page.data['pages']),(20,6))
        self.page._page(1)
        self.until(lambda: self.page.data['page']==2)
        self.page._tab('hosts')
        self.until(lambda: self.page.data['total']==30)
        self.page._tab('recordings')
        self.until(lambda: self.page.data['total']==120 and self.page.data['page']==2)
        self.page.record_search.setText('119')
        self.until(lambda: self.page.data['total']==1)
        self.assertEqual(self.page.table.rowCount(),1)
        self.page.resize(940, 640)
        self.app.processEvents()
        self.assertLessEqual(self.page.table.geometry().bottom(),self.page.height())
        self.assertEqual(self.page.table.horizontalScrollBar().maximum(), 0)

    def test_archive_clock_and_detail_action_survive_separate_time_column(self):
        rid, _ = self.completed_segment()
        self.page._tab('recordings')
        self.until(lambda:self.page.data['total']==1)
        self.controller.timer.stop()
        self.until(lambda:self.controller.worker is None)
        value = self.page.data
        record = value['rows'][0]
        self.assertEqual(record['id'], rid)
        record.update(duration=11, clock_running=True, ended=None)
        self.page.render(value)
        self.assertEqual([self.page.table.horizontalHeaderItem(c).text() for c in range(7)],
                         ['主播', '录制时间', '已录时长', '文件', '状态', '直播数据', '操作'])
        self.assertEqual(self.page.table.item(0, 0).text(), record['name'])
        self.assertEqual(self.page.table.item(0, 1).text(), stamp(record['started']))
        for column in range(5):
            self.assertEqual(self.page.table.item(0, column).textAlignment(), Qt.AlignmentFlag.AlignCenter)
        self.until(lambda:self.page.table.item(0, 2).text()=='00:00:13', timeout=4)
        QTest.mouseClick(self.page.table.viewport(), Qt.MouseButton.LeftButton,
                        pos=self.page.table.visualItemRect(self.page.table.item(0, 1)).center())
        self.assertFalse(self.page.table.selectedItems())
        self.assertEqual(self.page.table.item(0, 4).text(), '正在录制')
        self.page.resize(940, 640)
        self.app.processEvents()
        self.assertEqual(self.page.table.horizontalScrollBar().maximum(), 0)
        for column in (1, 2, 4):
            cell = self.page.table.item(0, column)
            self.assertGreaterEqual(self.page.table.columnWidth(column),
                                    QFontMetrics(cell.font()).horizontalAdvance(cell.text()) + 24)
        self.page.table.cellWidget(0, 6).findChild(QPushButton).click()
        self.until(lambda:self.page.detail_dialog and self.page.detail_dialog.export_button.isEnabled())
        self.assertEqual(self.page.detail_dialog.recording_id, rid)

    def test_recovery_scan_keeps_interrupted_recording_status_visible(self):
        self.seed(hosts=0, recordings=1)
        self.page._tab('recordings')
        self.until(lambda: self.page.data['total'] == 1)
        rid = self.page.data['rows'][0]['id']
        for reason, label in (('recovery_required', '上次录制中断'), ('recovery_checked', '中断录像已检查')):
            self.service.index.write('UPDATE recordings SET reason=? WHERE id=?', (reason, rid))
            self.page.refresh()
            self.until(lambda: self.page.table.item(0, 4).text() == label)

    def test_reconnect_and_final_save_share_actual_state_in_every_view(self):
        rid, media = self.completed_segment()
        self.service.index.write('UPDATE recordings SET ended=NULL,reason=? WHERE id=?', ('', rid))
        self.service.index.write('INSERT INTO hosts(id,source,name,created,status,enabled) VALUES(?,?,?,?,?,?)',
            ('fixture', 'https://live.douyin.com/123', '示例主播', time.time(), 'access_limited', 1))
        self.service._remember_observation('fixture', 'access_limited')
        producer = SimpleNamespace(has_data=True, last_bytes=media.stat().st_size, file_count=1,
            last_growth=time.monotonic(), started=time.time(), process=SimpleNamespace(process=SimpleNamespace(poll=lambda:0)))
        monitor = {'recording_id':rid, 'thread':object(), 'producer':producer, 'cancel':threading.Event(),
            'next':float('inf'), 'data_next':float('inf'), 'recording_state':'reconnecting', 'session':self.controller.session}
        self.service.monitors['fixture'] = monitor
        try:
            self.page._tab('recordings')
            self.until(lambda:self.page.data['total']==1)
            self.controller.timer.stop()
            self.until(lambda:self.controller.worker is None)
            for phase, label in (('reconnecting','等待重连'), ('stopping','正在保存最后一段')):
                monitor['recording_state'] = phase
                state = self.service.snapshot('recordings', '', 'all', 1)
                self.page.render(state)
                self.assertEqual(self.page.stats[1].text(), '0 位')
                self.assertEqual(self.page.table.item(0,4).text(),label)
                self.assertFalse(self.page.clock_cells)
                arguments = {'session':self.controller.session, 'recording_id':rid}
                self.page._data_detail(self.service.handle('data',arguments))
                self.assertIn(label,self.page.data_dialog.session_label.text())
                self.page.data_dialog.reject()
                self.page._detail(self.service.handle('detail',arguments))
                self.assertIn(label,self.page.detail_dialog.summary.text())
                self.page.detail_dialog.reject()
                host = self.service.snapshot('hosts','','all',1)['rows'][0]
                self.assertEqual(host['message'], '无法确认')
                self.assertEqual(host['progress_message'], label)
                self.assertIn('访问受限', host['error_message'])
        finally:
            self.service.monitors.clear()

    def test_both_clock_columns_inherit_body_font_and_size(self):
        self.seed(hosts=1, recordings=1)
        self.assertEqual(self.page.table.item(0, 4).font(), self.page.table.item(0, 3).font())
        self.page._tab('recordings')
        self.until(lambda: self.page.data['total'] == 1)
        self.assertEqual(self.page.table.item(0, 2).font(), self.page.table.item(0, 1).font())

    def test_segment_close_button_backdrop_and_escape_stop_refresh_without_clicking_through(self):
        rid, _ = self.completed_segment()
        background = QPushButton('background', self.page)
        background.setGeometry(0, 0, 22, 22)
        background.show()
        clicked = Mock()
        background.clicked.connect(clicked)
        for mode in ('close', 'backdrop', 'escape'):
            with self.subTest(mode=mode):
                self.controller.call('detail', recording_id=rid, callback=self.page._detail)
                self.until(lambda: self.page.detail_dialog and self.page.detail_dialog.isVisible())
                dialog = self.page.detail_dialog
                self.assertTrue(dialog.close_button.isVisible())
                QTest.mouseClick(dialog, Qt.MouseButton.LeftButton, pos=dialog.card.geometry().topLeft() + QPoint(2, 2))
                self.assertTrue(dialog.isVisible())
                if mode == 'close':
                    QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
                elif mode == 'backdrop':
                    QTest.mouseClick(dialog, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
                else:
                    QTest.keyClick(dialog.listing, Qt.Key.Key_Escape)
                self.assertFalse(dialog.isVisible())
                self.assertFalse(self.page.detail_timer.isActive())
        clicked.assert_not_called()

    def test_segment_overlay_fits_resize_and_keyboard_stays_inside_card(self):
        rid, _ = self.completed_segment()
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda: self.page.detail_dialog and self.page.detail_dialog.isVisible())
        dialog = self.page.detail_dialog
        self.assertFalse(dialog.isWindow())
        self.assertIs(dialog.window(), self.page.window())
        self.page.resize(940, 640)
        self.app.processEvents()
        self.assertEqual(dialog.geometry(), self.page.rect())
        self.assertTrue(dialog.rect().contains(dialog.card.geometry()))
        dialog.close_button.setFocus()
        for _ in range(15):
            QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
            self.assertTrue(dialog.isAncestorOf(QApplication.focusWidget()))
        QTest.keyClick(dialog.close_button, Qt.Key.Key_Return)
        self.assertTrue(dialog.isVisible())
        self.page.hide()
        self.assertFalse(dialog.isVisible())

    def test_segment_close_survives_destruction_of_original_focus_button(self):
        rid, _ = self.completed_segment()
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda: self.page.detail_dialog and self.page.detail_dialog.isVisible())
        dialog = self.page.detail_dialog
        old_button = QPushButton('old focus', self.page)
        dialog._previous_focus = old_button
        old_button.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.page.detail_timer.isActive())

    def test_archive_last_column_deletes_registered_folder_with_one_click(self):
        rid, folder, _ = register_recording(self.service, self.output)
        self.page._tab('recordings')
        self.until(lambda: self.page.data['recordings'] == 1)
        controls = self.page.table.cellWidget(0, 6).findChildren(QPushButton)
        self.assertEqual([b.text() for b in controls], ['查看分段', '删除'])
        trash = self.root / 'fake-trash'
        trash.mkdir()
        def native_move(path):
            Path(path).rename(trash / Path(path).name)
            return True  # Actual macOS binding return, not the declared tuple.
        with patch('PySide6.QtCore.QFile.moveToTrash', side_effect=native_move):
            QTest.mouseClick(controls[1], Qt.MouseButton.LeftButton)
            self.until(lambda: self.page.data['recordings'] == 0)
        self.assertTrue((trash / rid / 'part-000000.ts').exists())
        self.assertFalse(folder.exists())
        self.assertIn('系统废纸篓', self.page.message.text())
        self.assertEqual(self.page.data['hosts'], 1)

    def test_archive_missing_folder_removes_row_without_moving_any_file(self):
        rid, folder, _ = register_recording(self.service, self.output)
        preserved = folder.rename(self.root/'already-moved')
        self.page._tab('recordings')
        self.until(lambda: self.page.data['recordings'] == 1)
        with patch('PySide6.QtCore.QFile.moveToTrash', side_effect=AssertionError('no file operation')):
            self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1].click()
            self.until(lambda: self.page.data['recordings'] == 0)
        self.assertTrue((preserved/'part-000000.ts').is_file())
        self.assertIn('未改动文件', self.page.message.text())

    def test_late_detail_and_data_replies_cannot_reopen_deleted_recording(self):
        rid, _, _ = register_recording(self.service, self.output)
        reply = self.service.handle('detail', {'session': self.controller.session, 'recording_id': rid})
        self.page._tab('recordings')
        self.until(lambda: self.page.data['recordings'] == 1)
        trash = self.root / 'fake-trash'
        trash.mkdir()
        with patch('src.core.live_recording.service.move_to_trash',
                   side_effect=lambda path: str(path.rename(trash / path.name))):
            self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1].click()
            self.until(lambda: self.page.data['recordings'] == 0)
        self.page._detail(reply)
        self.page._data_detail(reply)
        self.assertIsNone(self.page.detail_dialog)
        self.assertIsNone(self.page.data_dialog)
        self.assertFalse(self.page.detail_timer.isActive())
        self.assertFalse(self.page.data_timer.isActive())

    def test_recording_delete_disabled_and_failure_reenables_for_retry(self):
        rid, folder, _ = register_recording(self.service, self.output)
        self.service.index.write('UPDATE recordings SET ended=NULL WHERE id=?', (rid,))
        self.page._tab('recordings')
        self.until(lambda: self.page.data['recordings'] == 1)
        remove = self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1]
        self.assertFalse(remove.isEnabled())
        self.assertIn('录制', remove.toolTip())
        self.service.index.write('UPDATE recordings SET ended=? WHERE id=?', (time.time(), rid))
        self.page.refresh()
        self.until(lambda: self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1].isEnabled())
        with patch('src.core.live_recording.service.move_to_trash', side_effect=LiveError('暂时无法移动，文件保留')):
            self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1].click()
            self.until(lambda: bool(self.errors) and not self.page.deleting)
            self.until(lambda: self.page.table.cellWidget(0, 6).findChildren(QPushButton)[1].isEnabled())
        self.assertTrue(folder.exists())
        self.assertEqual(self.page.data['recordings'], 1)

    def test_slow_trash_request_keeps_pause_and_shutdown_available(self):
        rid, _, _ = register_recording(self.service, self.output)
        entered, release = threading.Event(), threading.Event()
        def slow(_):
            entered.set()
            release.wait(4)
            raise LiveError('隔离移动取消，文件保留')
        with patch('src.core.live_recording.service.move_to_trash', slow):
            self.controller.call('delete_recording', recording_id=rid)
            self.until(entered.is_set)
            try:
                paused = Mock()
                self.controller.call('pause_all', callback=paused)
                self.until(lambda: paused.called, timeout=1)
                self.controller.request_stop()
                self.until(lambda: self.service.closing, timeout=1)
                self.assertFalse(self.controller.settled)
            finally:
                release.set()
            self.until(lambda: self.controller.settled)

    def test_page_input_waits_for_submit_and_survives_live_refresh(self):
        self.seed()
        self.page._tab('recordings')
        self.until(lambda:self.page.data['total']==120)
        self.assertTrue(self.page.pagination.isVisible())
        self.page.jump.setFocus()
        self.page.jump.selectAll()
        QCoreApplication.sendEvent(self.page.jump,QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_6,Qt.KeyboardModifier.NoModifier,'6'))
        self.page.render(self.page.data)
        self.assertEqual((self.page.jump.text(),self.page.data['page']),('6',1))
        QCoreApplication.sendEvent(self.page.jump,QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_Return,Qt.KeyboardModifier.NoModifier))
        self.until(lambda:self.page.data['page']==6)
        self.page.jump.selectAll()
        QCoreApplication.sendEvent(self.page.jump,QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_7,Qt.KeyboardModifier.NoModifier,'7'))
        self.page.jump_button.click()
        self.assertEqual(self.page.data['page'],6)
        self.assertEqual(self.page.message.text(),'请输入 1 至 6 之间的页码')
        self.page.record_search.setText('119')
        self.until(lambda:self.page.data['total']==1)
        self.assertEqual(self.page.data['page'],1)
        self.assertFalse(self.page.pagination.isVisible())
        self.assertNotIn('页码',self.page.message.text())

    def test_default_browser_room_link_and_key_operations_share_page(self):
        hid = uid('host')
        self.service.index.write('INSERT INTO hosts(id,source,name,web_rid,created,status) VALUES(?,?,?,?,?,?)',
            (hid,'https://live.douyin.com/123','测试主播','123',time.time(),'paused'))
        self.page.refresh()
        self.until(lambda:self.page.table.rowCount()==1)
        with patch('src.ui.live_recording_page.QDesktopServices.openUrl',return_value=True) as opener:
            self.page.table.cellWidget(0,5).findChild(QPushButton).click()
            self.assertEqual(opener.call_args[0][0].toString(),'https://live.douyin.com/123')
        self.page.credentials.show()
        self.assertEqual(self.page.credential.echoMode(),self.page.credential.EchoMode.Password)
        self.page.credential.setText('fixture-cookie')
        QCoreApplication.sendEvent(self.page.credential,QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_Return,Qt.KeyboardModifier.NoModifier))
        self.until(lambda:self.service.index.setting('credential')=='configured')
        self.until(lambda:not self.page.credential.text())
        self.page._credential(delete=True)
        self.until(lambda:not self.service.index.setting('credential'))

    def completed_segment(self):
        rid, sid = uid('recording'), uid('segment')
        folder = self.output / rid
        folder.mkdir()
        media = folder / 'part-000000.ts'
        self.service.ffmpeg = bundled_ffmpeg()
        subprocess.run([self.service.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-c:a', 'aac', '-f', 'mpegts', str(media)],
            check=True, timeout=15)
        info = probe(media, self.service.ffmpeg)
        now = time.time() - 100
        self.service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended,reason) VALUES(?,?,?,?,?,?,?,?)',
            (rid, 'fixture', '示例主播', uid('live'), str(folder), now, now+2, 'offline'))
        self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
            (sid, rid, str(media), now, info['duration'], info['bytes'], 1, 1, info['sha256']))
        return rid, media

    def test_segment_extract_hands_off_original_without_exporting(self):
        rid, media = self.completed_segment()
        self.page.transcribe = Mock(return_value=True)
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda:self.page.detail_dialog and self.page.detail_dialog.extract_button.isEnabled())
        dialog = self.page.detail_dialog
        dialog.extract_button.click()
        self.until(lambda:self.page.transcribe.called)
        self.page.transcribe.assert_called_once_with(str(media))
        self.assertEqual(self.service.exports, {})
        self.assertFalse(list(media.parent.glob('*.mp4')))
        self.assertTrue(media.exists())

    def test_recovered_segment_without_wall_time_keeps_order_duration_and_actions(self):
        rid, media = self.completed_segment()
        known = self.service.index.one('SELECT * FROM segments WHERE recording_id=?', (rid,))
        recovered = media.parent/'part-000001.ts'
        recovered.write_bytes(media.read_bytes())
        sid = uid('segment')
        self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
            (sid, rid, str(recovered), 0, known['duration'], known['bytes'], 1, 1, known['sha256']))
        self.page.transcribe = Mock(return_value=True)
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda: self.page.detail_dialog and self.page.detail_dialog.listing.count() == 2)
        dialog = self.page.detail_dialog
        listing = dialog.listing
        self.assertEqual(listing.item(0).data(Qt.ItemDataRole.UserRole)['path'], str(media))
        self.assertEqual(listing.item(1).data(Qt.ItemDataRole.UserRole)['path'], str(recovered))
        first = [label.text() for label in listing.itemWidget(listing.item(0)).findChildren(QLabel)]
        second = [label.text() for label in listing.itemWidget(listing.item(1)).findChildren(QLabel)]
        self.assertTrue(any(stamp(known['started']) + ' 至 ' in text for text in first))
        self.assertTrue(any('起止时间无法确认' in text and '00:02' in text for text in second))
        self.assertFalse(any('1970' in text for text in second))
        listing.setCurrentRow(1)
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertTrue(dialog.extract_button.isEnabled())
        dialog.extract_button.click()
        self.until(lambda: self.page.transcribe.called)
        self.page.transcribe.assert_called_once_with(str(recovered))

    def test_slow_transcribe_keeps_gui_controls_free_and_discards_reply_after_stop(self):
        rid, _ = self.completed_segment()
        sid = self.service.index.one('SELECT id FROM segments WHERE recording_id=?', (rid,))['id']
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        replies = []
        original = self.controller.dispatcher
        def slow(operation, arguments):
            if operation == 'transcribe':
                entered.set()
                release.wait(5)
            if operation == 'pause_all':
                paused.set()
            return original(operation, arguments)
        self.controller.dispatcher = slow
        try:
            self.controller.call('transcribe', segment_id=sid, callback=replies.append)
            self.until(entered.is_set)
            self.controller.call('pause_all')
            self.until(paused.is_set, timeout=1)
            self.controller.request_stop()
            self.until(lambda: self.service.closing)
            self.assertFalse(self.controller.settled)
        finally:
            release.set()
            self.until(lambda: self.controller.worker is None)
        self.until(lambda: self.controller.settled)
        self.assertEqual(replies, [])

    def test_segments_update_automatically_and_keep_selection_and_scroll(self):
        rid, media = self.completed_segment()
        first = self.service.index.one('SELECT * FROM segments WHERE recording_id=?', (rid,))
        self.service.index.write('UPDATE recordings SET ended=NULL WHERE id=?', (rid,))
        for number in range(1, 12):
            self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                (uid('segment'), rid, str(media.parent/f'saved-{number:02}.ts'), first['started']+number*2,
                 first['duration'], first['bytes'], 1, 1, first['sha256']))
        pending = media.parent/'part-000001.ts'
        pending.write_bytes(media.read_bytes())
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda:self.page.detail_dialog and self.page.detail_dialog.listing.count()==13)
        dialog = self.page.detail_dialog
        listing = dialog.listing
        listing.setCurrentRow(5)
        selected = listing.currentItem().data(Qt.ItemDataRole.UserRole)['path']
        listing.verticalScrollBar().setValue(250)
        scroll = listing.verticalScrollBar().value()
        self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
            (uid('segment'), rid, str(pending), first['started']+24,
             first['duration'], first['bytes'], 1, 1, first['sha256']))
        (media.parent/'part-000002.ts').write_bytes(media.read_bytes())
        self.until(lambda:listing.count()==14, timeout=5)
        self.assertIs(self.page.detail_dialog, dialog)
        self.assertEqual(listing.currentItem().data(Qt.ItemDataRole.UserRole)['path'], selected)
        self.assertEqual(listing.verticalScrollBar().value(), scroll)
        self.assertTrue(listing.item(12).flags() & Qt.ItemFlag.ItemIsSelectable)
        self.assertFalse(listing.item(13).flags() & Qt.ItemFlag.ItemIsSelectable)
        self.assertFalse(any(b.text()=='刷新分段' for b in dialog.findChildren(QPushButton)))

    def test_segment_updates_pause_for_actions_and_discard_late_close_reply(self):
        rid, _ = self.completed_segment()
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda:self.page.detail_dialog and self.page.detail_dialog.isVisible())
        dialog = self.page.detail_dialog
        self.until(lambda:self.controller.worker is None)
        entered, release = threading.Event(), threading.Event()
        original = self.controller.dispatcher
        calls = []
        def delayed(operation, args):
            if operation=='detail':
                calls.append(operation)
                entered.set()
                release.wait(5)
            return original(operation, args)
        self.controller.dispatcher = delayed
        dialog.set_busy(True)
        self.page._refresh_detail()
        self.assertEqual(calls, [])
        dialog.set_busy(False)
        self.page._refresh_detail()
        self.until(entered.is_set)
        self.page._refresh_detail()
        self.page._refresh_detail()
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.page.detail_timer.isActive())
        dialog.close()
        self.assertFalse(self.page.detail_timer.isActive())
        with patch.object(dialog, 'update_data') as updated:
            release.set()
            self.until(lambda:self.controller.worker is None)
            updated.assert_not_called()
        self.assertFalse(dialog.isVisible())

    def test_extract_rejects_unfinished_and_changed_segments_without_creating_tasks(self):
        rid, media = self.completed_segment()
        sid = self.service.index.one('SELECT id FROM segments WHERE recording_id=?', (rid,))['id']
        args = {'session': self.controller.session, 'segment_id': sid}
        self.service.index.write('UPDATE segments SET closed=0 WHERE id=?', (sid,))
        with self.assertRaisesRegex(LiveError, '已完成'):
            self.service.handle('transcribe', args)
        self.service.index.write('UPDATE segments SET closed=1 WHERE id=?', (sid,))
        with media.open('ab') as handle:
            handle.write(b'changed')
        with self.assertRaisesRegex(LiveError, '已变化'):
            self.service.handle('transcribe', args)
        self.assertEqual(self.service.exports, {})
        self.assertTrue(media.exists())

    def test_export_picker_selects_audio_location_and_cancel_has_no_effect(self):
        rid, media = self.completed_segment()
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda:self.page.detail_dialog and self.page.detail_dialog.export_button.isEnabled())
        dialog = self.page.detail_dialog
        with patch('src.ui.live_recording_page.QFileDialog.getSaveFileName', return_value=('', '')):
            dialog.export_button.click()
        self.assertEqual(self.service.exports, {})
        self.assertTrue(dialog.isVisible())
        target = self.root/'chosen-audio.mp3'
        with patch('src.ui.live_recording_page.QFileDialog.getSaveFileName', return_value=(str(target), 'MP3 音频 (*.mp3)')):
            dialog.export_button.click()
        self.until(lambda:target.exists() and 'MP3 已导出' in self.page.message.text())
        self.assertTrue(probe(target, self.service.ffmpeg)['audio'])
        self.assertTrue(media.exists())

    def test_left_cross_removes_host_in_one_click_and_keeps_recordings(self):
        self.seed(hosts=1, recordings=1)
        cross = self.page.table.cellWidget(0,0).findChild(QPushButton)
        self.assertEqual(cross.text(), '×')
        self.assertGreaterEqual(cross.parentWidget().width(), cross.width() + 12)
        cross.click()
        self.until(lambda:self.page.data['hosts']==0)
        self.assertEqual(self.page.data['recordings'], 1)
        self.assertIsNotNone(self.service.index.one('SELECT id FROM recordings'))

    def test_access_dialog_draft_errors_and_segmented_layout(self):
        self.assertFalse(self.page.credentials.isVisible())
        self.assertFalse(self.page.message.isVisible())
        self.assertFalse(self.page.pause_all.isEnabled())
        self.page.settings_button.click()
        self.app.processEvents()
        self.assertTrue(self.page.credentials.isVisible())
        self.assertFalse(self.page.delete_credential.isEnabled())
        self.page.credential.setText('unsaved-fixture')
        self.controller.error.emit('保存失败，请重试')
        self.page.render(self.page.data)
        self.assertEqual(self.page.credential.text(),'unsaved-fixture')
        self.assertEqual(self.page.credential_status.text(),'保存失败，请重试')
        self.page.credentials.reject()
        self.assertFalse(self.page.credential.text())
        self.page.record_tab.click()
        self.until(lambda:self.page.tab=='recordings' and self.page.data['total']==0)
        self.assertTrue(self.page.record_tab.isChecked())
        self.assertFalse(self.page.host_tab.isChecked())
        self.page.record_tab.click()
        self.assertTrue(self.page.record_tab.isChecked())
        self.page.resize(940,640)
        self.app.processEvents()
        self.assertLessEqual(self.page.table.geometry().bottom(),self.page.height())

    def test_waiting_monitor_guards_install_and_close_settles(self):
        from src.ui.main_window import MainWindow
        entered, release = threading.Event(), threading.Event()
        def delayed_open(operation, arguments):
            if operation == 'open':
                entered.set()
                release.wait(5)
                return {'session': uid('session')}
            return {'active': False, 'stopped': True}
        opening = LiveController(dispatcher=delayed_open)
        opening.start()
        window = SimpleNamespace(live_controller=opening, _toast=Mock(),
                                 update_controller=Mock(), _open_update_settings=Mock())
        window.update_controller.install_gate.return_value = 'ready'
        window.update_controller.launch_install.return_value = 'failed'
        try:
            self.assertTrue(entered.wait(2))
            MainWindow._request_update_install(window)
            window.update_controller.launch_install.assert_not_called()
        finally:
            release.set()
            opening.request_stop()
            self.until(lambda: opening.settled and opening.worker is None)
            opening.deleteLater()
        self.page.source.setText('https://live.douyin.com/123')
        self.page._add()
        self.until(lambda:self.page.data['hosts']==1)
        hid = self.page.data['rows'][0]['id']
        self.controller.call('start',host_id=hid)
        self.until(lambda:self.controller.active)
        self.assertTrue(MainWindow.has_active_work(SimpleNamespace(live_controller=self.controller)))
        self.assertFalse(self.controller.request_stop())
        self.until(lambda:self.controller.settled and self.controller.worker is None)
        self.assertFalse(self.service.busy())
