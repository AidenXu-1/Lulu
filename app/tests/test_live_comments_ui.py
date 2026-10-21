"""Native UI with temporary Core, fake comments and mocked clipboard/file picker."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from datetime import datetime, timezone
import threading
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionSlider

from src.core.live_recording.comments import CommentLog
from tests.test_live_comments import chat
from tests import test_live_recording_ui as fixtures

class CommentUITests(TestCase):
    setUpClass=classmethod(fixtures.NativeLiveTests.setUpClass.__func__)
    setUp=fixtures.NativeLiveTests.setUp
    tearDown=fixtures.NativeLiveTests.tearDown
    until=fixtures.NativeLiveTests.until
    completed_segment=fixtures.NativeLiveTests.completed_segment

    def open_panel(self):
        rid,_=self.completed_segment()
        record=self.service._recording(rid)
        CommentLog(record).append([chat(1),chat(2,nickname='另一个人',text='原文🙂'),chat(3,text='一样')])
        self.service.index.write("UPDATE recordings SET comments_enabled=1,comments_count=3,comments_state='ended' WHERE id=?",(rid,))
        self.page._tab('recordings')
        self.until(lambda:self.page.data['total']==1)
        self.page.table.cellWidget(0,5).findChild(QPushButton).click()
        self.until(lambda:self.page.data_dialog and self.page.data_dialog.isVisible())
        dialog=self.page.data_dialog
        dialog.tabs.setCurrentIndex(1)
        panel=dialog.comments_panel
        self.until(lambda:panel.result.get('total')==3 and self.controller.comments_worker is None)
        return dialog,panel,record

    def test_toggle_entry_search_copy_and_export(self):
        self.assertTrue(self.page.comments_toggle.isChecked())
        self.page.comments_toggle.click()
        self.until(lambda:self.service.index.setting('comments_enabled')=='0')
        dialog,panel,record=self.open_panel()
        self.assertEqual(dialog.tabs.tabText(0),'数据走势')
        self.assertEqual(dialog.tabs.tabText(1),'弹幕记录')
        self.assertEqual(self.page.table.horizontalHeaderItem(5).text(),'直播记录')
        panel.search.setText('另一个人')
        self.until(lambda:panel.result.get('matching')==1 and panel.table.rowCount()==1
                   and self.controller.comments_worker is None)
        panel.table.selectRow(0)
        copied=[]
        with patch('src.ui.live_comments_panel.QGuiApplication.clipboard',
                   return_value=SimpleNamespace(setText=copied.append)):
            panel.copy_button.click()
        self.assertIn('原文🙂',copied[0])
        target=self.root/'导出弹幕.csv'
        with patch('src.ui.live_comments_panel.QFileDialog.getSaveFileName',
                   return_value=(str(target),'CSV 文件 (*.csv)')):
            panel.csv_action.trigger()
        self.until(lambda:target.exists() and not panel.exporting and self.controller.comments_worker is None)
        self.assertIn('多少钱？',target.read_text())
        self.assertIn('已导出',panel.notice.text())
        dialog.reject()

    def test_refresh_preserves_message_identity_not_old_row_selection(self):
        from src.core.live_recording.comments import read_comments
        dialog, panel, record = self.open_panel()
        panel.timer.stop()
        self.until(lambda: self.controller.comments_worker is None)
        log = CommentLog(record)
        log.append([chat(i, text=f'消息{i}') for i in range(4, 201)])
        def populate(query=''):
            panel.show_result(dict(read_comments(record, query), comments_state='ended'))
        def selected():
            return [panel.rows[i.row()]['message_id'] for i in panel.table.selectionModel().selectedRows()]
        populate()
        panel.table.selectRow(5)
        self.assertEqual(selected(), ['6'])
        log.append([chat(201)])
        populate()
        self.assertEqual(selected(), ['6'])
        copied = []
        with patch('src.ui.live_comments_panel.QGuiApplication.clipboard',
                   return_value=SimpleNamespace(setText=copied.append)):
            panel.copy_button.click()
        self.assertIn('消息6', copied[0])
        self.assertNotIn('消息7', copied[0])
        populate('消息201')
        self.assertEqual(selected(), [])
        dialog.reject()

    def test_late_read_cannot_reopen_destroyed_dialog_and_pause_remains_available(self):
        dialog,panel,record=self.open_panel()
        entered,release=threading.Event(),threading.Event()
        original=self.controller.dispatcher
        def delayed(operation,args):
            if operation=='comments':entered.set();release.wait(4)
            return original(operation,args)
        self.controller.dispatcher=delayed
        panel.refresh()
        self.until(entered.is_set)
        dialog.close();dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.controller.call('pause_all')
        self.until(lambda:self.controller.worker is None)
        release.set()
        self.until(lambda:self.controller.comments_worker is None)
        self.assertFalse(self.errors)
        self.controller.dispatcher=original

    def test_comment_tab_refreshes_video_save_state_after_recording_ends(self):
        dialog,panel,record=self.open_panel()
        self.until(lambda:self.controller.data_worker is None and self.controller.comments_worker is None)
        # The dialog has an active snapshot while Core has finished the scene.
        dialog.update_data(dict(dialog.data,ended=None,reason='',recording_state='live',clock_running=True))
        self.assertEqual(panel.video_state_label.text(),'录像 · 正在录制')
        self.service.index.write("UPDATE recordings SET reason='paused' WHERE id=?",(record['id'],))
        self.page._refresh_data()
        self.until(lambda:dialog.data['ended'] is not None)
        self.assertEqual(dialog.record_state.text(),'已保存')
        self.assertEqual(panel.video_state_label.text(),'录像 · 已保存')
        self.assertFalse(dialog.clock_timer.isActive())
        self.assertEqual(dialog.tabs.currentIndex(),1)
        self.assertEqual(self.errors,[])
        dialog.reject()

    def test_small_and_normal_windows_keep_comment_controls_inside_dialog(self):
        dialog,panel,_=self.open_panel()
        for width,height in ((560,480),(800,600),(960,680)):
            dialog.resize(width,height)
            self.app.processEvents()
            for control in (panel.search,panel.export_button,panel.table,panel.timeline,dialog.navigation):
                point=control.mapTo(dialog,QPoint(0,0))
                self.assertGreaterEqual(point.x(),0)
                self.assertGreaterEqual(point.y(),0)
                self.assertLessEqual(point.x()+control.width(),dialog.width())
                self.assertLessEqual(point.y()+control.height(),dialog.height())
            self.assertEqual(panel.timeline.visibleRegion().boundingRect().height(),panel.timeline.height())
            self.assertGreaterEqual(panel.table.height(),80)
            columns=[panel.table.columnWidth(i) for i in range(3)]
            self.assertGreaterEqual(columns[0],82)
            self.assertGreater(columns[1],columns[0])
            self.assertGreater(columns[2],columns[1])
            self.assertGreaterEqual(columns[2],panel.table.viewport().width()*.45)
        dialog.reject()

    def test_scrollbar_has_visible_thumb_below_header_and_can_be_dragged(self):
        from src.ui import theme as T
        previous=self.app.styleSheet()
        self.app.setStyleSheet(T.QSS)
        try:
            dialog,panel,record=self.open_panel()
            CommentLog(record).append([chat(i) for i in range(4,206)])
            panel.refresh()
            self.until(lambda:panel.result.get('total')==205 and self.controller.comments_worker is None)
            dialog.resize(960,720);self.app.processEvents()
            bar=panel.table.verticalScrollBar()
            option=QStyleOptionSlider();bar.initStyleOption(option)
            thumb=bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar,option,
                                            QStyle.SubControl.SC_ScrollBarSlider,bar)
            top=bar.mapTo(panel.table,QPoint(0,thumb.top())).y()
            self.assertGreaterEqual(top,panel.table.horizontalHeader().height())
            image=bar.grab().toImage()
            visible=sum(image.pixelColor(x,thumb.center().y()).alpha()>0 for x in range(image.width()))
            self.assertGreaterEqual(visible,5)
            QTest.mousePress(bar,Qt.MouseButton.LeftButton,pos=thumb.center())
            QTest.mouseMove(bar,QPoint(thumb.center().x(),bar.height()-12))
            QTest.mouseRelease(bar,Qt.MouseButton.LeftButton,pos=QPoint(thumb.center().x(),bar.height()-12))
            self.app.processEvents()
            self.assertGreater(bar.value(),0)
            dialog.reject()
        finally:
            self.app.setStyleSheet(previous)

    def test_three_columns_whole_timeline_click_search_and_keyboard_filter(self):
        dialog,panel,record=self.open_panel()
        self.assertEqual([panel.table.horizontalHeaderItem(i).text() for i in range(3)],
                         ['时间','用户昵称','弹幕内容'])
        self.assertEqual(panel.table.item(0,0).text(),'00:01')
        self.assertEqual(panel.table.item(1,1).text(),'另一个人')
        self.assertEqual(panel.table.item(1,2).text(),'原文🙂')
        CommentLog(record).append([chat(i,text='记录'+str(i)) for i in range(4,406)])
        panel.refresh()
        self.until(lambda:panel.result.get('total')==405 and self.controller.comments_worker is None)
        self.assertEqual(sum(b['count'] for b in panel.timeline.bins),405)
        self.assertEqual(len(panel.rows),200)
        self.app.processEvents()
        plot=panel.timeline.plot
        x=plot.left()+205/panel.timeline.duration*plot.width()
        QTest.mouseClick(panel.timeline,Qt.MouseButton.LeftButton,pos=QPoint(int(x),45))
        self.until(lambda:panel.result.get('matching')==10 and self.controller.comments_worker is None)
        self.assertEqual(panel.time_range,(200,210))
        self.assertTrue(all(200<=r['elapsed_seconds']<210 for r in panel.rows))
        self.assertTrue(panel.all_button.isVisible())
        self.assertIn('录像',panel.video_state_label.text())
        self.assertIn('弹幕',panel.state_label.text())
        panel.search.setText('记录400')
        self.until(lambda:panel.result.get('matching')==1 and self.controller.comments_worker is None)
        self.assertIsNone(panel.time_range)
        self.assertEqual(sum(b['matching'] for b in panel.timeline.bins),1)
        QTest.keyClick(panel.timeline,Qt.Key.Key_Home)
        self.until(lambda:panel.result.get('matching')==0 and self.controller.comments_worker is None)
        QTest.keyClick(panel.timeline,Qt.Key.Key_Escape)
        self.until(lambda:panel.result.get('matching')==1 and self.controller.comments_worker is None)
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(panel.time_range)
        dialog.reject()

    def test_zero_comment_period_and_interruption_have_different_explanations(self):
        dialog,panel,record=self.open_panel()
        status=lambda at,state:{'kind':'status','state':state,
            'received_at':datetime.fromtimestamp(record['started']+at,timezone.utc).isoformat()}
        CommentLog(record).append([status(19,'reconnecting'),status(26,'recording'),chat(45)])
        panel.select_range((30,35))
        self.until(lambda:panel.result.get('total')==4 and panel.result.get('matching')==0
                   and self.controller.comments_worker is None)
        self.assertIn('其他时段',panel.empty_detail.text())
        self.assertNotIn('缺少',panel.empty_detail.text())
        panel.select_range((20,25))
        self.until(lambda:not panel.reading and self.controller.comments_worker is None)
        self.assertIn('未收到的数量无法确定',panel.empty_detail.text())
        dialog.reject()

    def test_time_selection_discards_reply_from_previous_pending_read(self):
        dialog,panel,record=self.open_panel()
        CommentLog(record).append([chat(20)])
        entered,release=threading.Event(),threading.Event()
        original=self.controller.dispatcher
        def delayed(operation,args):
            if operation=='comments':entered.set();release.wait(4)
            return original(operation,args)
        self.controller.dispatcher=delayed
        try:
            panel.refresh();self.until(entered.is_set)
            panel.select_range((15,25))
            release.set()
            self.until(lambda:self.controller.comments_worker is None)
            self.assertEqual(panel.result['matching'],3)  # Previous reply was discarded.
            self.controller.dispatcher=original
            panel.refresh()
            self.until(lambda:panel.result.get('matching')==1 and self.controller.comments_worker is None)
            self.assertEqual([r['elapsed_seconds'] for r in panel.rows],[20])
        finally:
            release.set()
            self.controller.dispatcher=original
        dialog.reject()

    def test_reading_rows_wrap_and_keyboard_copy_keeps_exact_original(self):
        dialog,panel,record=self.open_panel()
        text='多行原文🙂\n<b>原样保留</b> '+('一段需要自动换行的完整内容' * 20)
        CommentLog(record).append([chat(4,nickname='很长的昵称'*20,text=text)])
        panel.refresh()
        self.until(lambda:panel.result.get('total')==4 and self.controller.comments_worker is None)
        dialog.resize(960,680);self.app.processEvents();panel.table.resizeRowsToContents()
        wide=panel.table.rowHeight(3)
        dialog.resize(560,480);self.app.processEvents();panel.table.resizeRowsToContents()
        self.assertGreater(panel.table.rowHeight(3),wide)
        self.assertEqual(panel.table.horizontalScrollBar().maximum(),0)
        panel.table.selectRow(3);panel.table.setFocus();self.app.processEvents()
        self.assertTrue(panel.copy_button.isVisible())
        copied=[]
        with patch('src.ui.live_comments_panel.QGuiApplication.clipboard',
                   return_value=SimpleNamespace(setText=copied.append)):
            QTest.keySequence(panel.table,QKeySequence(QKeySequence.StandardKey.Copy))
            self.app.processEvents()
        self.assertEqual(len(copied),1)
        self.assertTrue(copied[0].endswith('：'+text))
        CommentLog(record).append([chat(5)])
        panel.refresh()
        self.until(lambda:panel.result.get('total')==5 and self.controller.comments_worker is None)
        selected=panel.table.selectionModel().selectedRows()
        self.assertEqual([panel.rows[i.row()]['message_id'] for i in selected],['4'])
        dialog.reject()

    def test_single_page_is_quiet_and_paging_search_empty_state_are_usable(self):
        dialog,panel,record=self.open_panel()
        self.assertFalse(panel.pagination.isVisible())
        self.assertFalse(panel.copy_button.isVisible())
        self.assertEqual(len(panel.export_menu.actions()),2)
        CommentLog(record).append([chat(i,text='记录'+str(i)) for i in range(4,406)])
        panel.refresh()
        self.until(lambda:panel.result.get('total')==405 and self.controller.comments_worker is None)
        self.assertTrue(panel.pagination.isVisible())
        self.assertEqual(panel.rows[0]['message_id'],'206')
        panel.older.click()
        self.until(lambda:panel.result.get('page')==2 and self.controller.comments_worker is None)
        self.assertEqual(panel.rows[0]['message_id'],'6')
        panel.search.setText('不存在的关键词')
        self.until(lambda:panel.result.get('matching')==0 and self.controller.comments_worker is None)
        self.assertEqual(panel.content.currentIndex(),1)
        self.assertEqual(panel.empty_title.text(),'没有找到相关弹幕')
        self.assertTrue(panel.export_button.isEnabled())  # Whole-scene export survives an empty search.
        panel.search.clear()
        self.until(lambda:panel.result.get('matching')==405 and self.controller.comments_worker is None)
        self.assertEqual(panel.content.currentIndex(),0)
        dialog.reject()

    def test_empty_legacy_and_corrupt_file_are_distinct(self):
        dialog,panel,record=self.open_panel()
        path=Path(record['directory'])/'弹幕记录'/'弹幕记录.jsonl'
        path.write_bytes(b'partial')
        panel.refresh()
        self.until(lambda:panel.result.get('read_error') is True)
        self.assertIsNone(panel.result['total'])
        self.assertIn('暂不可读',panel.summary.text())
        self.assertIn('原文件保留',panel.notice.text())
        path.unlink()  # Isolated fixture only; no real recording is opened.
        self.service.index.write("UPDATE recordings SET comments_state='legacy' WHERE id=?",(record['id'],))
        self.service.comment_cache.pop(record['id'],None)
        self.until(lambda:self.controller.comments_worker is None)
        panel.refresh()
        self.until(lambda:panel.result.get('comments_state')=='legacy' and not panel.result['read_error'])
        self.assertEqual(panel.result['total'],0)
        self.assertFalse(panel.timeline.bins)
        self.assertIn('旧录像不会补录',panel.notice.text())
        self.service.index.write("UPDATE recordings SET comments_state='disabled' WHERE id=?",(record['id'],))
        self.until(lambda:self.controller.comments_worker is None)
        panel.refresh()
        self.until(lambda:panel.result.get('comments_state')=='disabled')
        self.assertIn('本场未开启',panel.notice.text())
        dialog.reject()

    def test_reconnect_discards_old_read_and_viewer_can_refresh_again(self):
        dialog,panel,record=self.open_panel()
        entered,release=threading.Event(),threading.Event()
        original=self.controller.dispatcher
        def delayed(operation,args):
            if operation=='comments':entered.set();release.wait(4)
            return original(operation,args)
        self.controller.dispatcher=delayed
        panel.refresh();self.until(entered.is_set)
        self.controller.reconnect()
        self.until(lambda:self.controller.connected and self.controller.worker is None)
        release.set()
        self.until(lambda:self.controller.comments_worker is None)
        self.controller.dispatcher=original
        panel.refresh()
        self.until(lambda:not panel.reading and self.controller.comments_worker is None)
        self.assertEqual(panel.result['total'],3)
        dialog.reject()

    def test_reconnect_during_export_releases_buttons_without_replaying_export(self):
        dialog,panel,_=self.open_panel()
        entered,release=threading.Event(),threading.Event()
        original=self.controller.dispatcher
        exports=[]
        def delayed(operation,args):
            result=original(operation,args)
            if operation=='comments_export':
                exports.append(args['path']);entered.set();release.wait(4)
            return result
        self.controller.dispatcher=delayed
        target=self.root/'连接变化时导出.txt'
        try:
            with patch('src.ui.live_comments_panel.QFileDialog.getSaveFileName',
                       return_value=(str(target),'TXT 文件 (*.txt)')):
                panel.txt_action.trigger()
            self.until(entered.is_set)
            self.assertTrue(target.exists())
            self.assertTrue(panel.exporting)
            self.controller.reconnect()
            self.until(lambda:self.controller.connected and self.controller.worker is None)
            release.set()
            self.until(lambda:self.controller.comments_worker is None and not panel.exporting)
            self.assertTrue(panel.export_button.isEnabled())
            self.assertEqual(exports,[str(target)])
            self.assertIn('请检查导出位置',panel.notice.text())
        finally:
            release.set()
            self.controller.dispatcher=original
        dialog.reject()
