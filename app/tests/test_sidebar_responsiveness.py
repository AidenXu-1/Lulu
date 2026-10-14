"""Temporary data, fake credentials and no network; real Qt controls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtGui import QImage,QColor
from src.ui.consumer_select import ConsumerSelect
from src.ui.settings_page import SettingsPage
from tests.test_settings_page import _Cfg,_Controller

class SidebarResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def wait(self,predicate):
        end=time.monotonic()+3
        while not predicate() and time.monotonic()<end:QTest.qWait(5)
        self.assertTrue(predicate())

    def test_closed_select_does_not_build_rows_and_first_open_keeps_latest_state(self):
        selector=ConsumerSelect([('a','甲'),('b','乙')],value='a',accessible_name='隔离选择')
        try:
            selector.set_value('b');selector.setEnabled(False);selector.setEnabled(True)
            selector.set_options([('b','乙'),('c','丙')],value='c')
            self.assertIsNone(selector._popup)
            popup=selector.popup
            self.assertEqual([r.value for r in popup.rows],['b','c'])
            self.assertTrue(popup.rows[1].property('selected'))
            self.assertIs(selector.popup,popup)
        finally:selector.close();selector.deleteLater()

    def test_lazy_settings_constructs_only_selected_category_and_forwards_update(self):
        with tempfile.TemporaryDirectory() as d,patch('src.core.secrets.get_config',return_value={}),patch('src.ui.skills_page.SkillsPage.refresh'):
            page=SettingsPage(_Cfg(Path(d)),update_controller=_Controller(),lazy_categories=True,agent_skill_status_provider=lambda:'not_installed')
            try:
                self.assertFalse(hasattr(page,'feishu_connection'));self.assertFalse(hasattr(page,'update_panel'))
                page.set_category('feishu');self.assertTrue(hasattr(page,'feishu_connection'))
                panel=page.feishu_connection;page.set_category('general');page.set_category('feishu');self.assertIs(page.feishu_connection,panel)
                page.set_category('about');calls=[];page.install_requested.connect(lambda:calls.append('install'))
                page.update_panel.install_requested.emit();self.assertEqual(calls,['install'])
                page.set_category('skills');self.assertTrue(hasattr(page,'skills_page'))
            finally:page.shutdown_workers();page.close();page.deleteLater()

    def test_collection_navigation_reads_history_off_thread_and_preserves_last_good_state(self):
        from src.ui.collection_page import CollectionPage
        from src.core import library
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d)):
            page=CollectionPage();calls=[]
            def read():
                calls.append(threading.get_ident());time.sleep(.2)
                return [{'kind':'transcript','source':'https://www.douyin.com/video/123456'}]
            try:
                with patch.object(library,'list_entries',side_effect=read):
                    start=time.monotonic();page.refresh();page.refresh()
                    self.assertLess(time.monotonic()-start,.12)
                    self.wait(lambda:'douyin:123456' in page._transcribed and page._transcribed_read is None)
                    self.assertTrue(all(t!=threading.get_ident() for t in calls))
                with patch.object(library,'list_entries',side_effect=OSError('fixture read failure')):
                    page.refresh();self.wait(lambda:page._transcribed_read is None)
                    self.assertIn('douyin:123456',page._transcribed)
            finally:page.shutdown_workers();page.close();page.deleteLater()

    def test_collection_closing_does_not_wait_for_slow_disk_or_apply_late_result(self):
        from src.ui.collection_page import CollectionPage
        from src.core import library
        entered,release=threading.Event(),threading.Event()
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d)):
            page=CollectionPage();page._transcribed={'old'}
            def read():
                entered.set();release.wait(3);return []
            try:
                with patch.object(library,'list_entries',side_effect=read):
                    page.refresh();self.assertTrue(entered.wait(1))
                    reader=page._transcribed_read
                    start=time.monotonic();page.shutdown_workers()
                    self.assertLess(time.monotonic()-start,.3)
                    release.set();self.assertTrue(reader.wait(2000));QTest.qWait(10)
                    self.assertEqual(page._transcribed,{'old'})
            finally:release.set();page.shutdown_workers();page.close();page.deleteLater()

    def test_collection_return_reuses_rows_and_updates_only_changed_status(self):
        from src.ui.collection_page import CollectionPage
        from src.core.collect.models import CollectedItem
        from src.core import library
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d)),patch.object(library,'list_entries',return_value=[]):
            page=CollectionPage()
            try:
                page._set_items([CollectedItem(str(i+1000),title='作品 '+str(i)) for i in range(200)])
                page._selected_keys.add(page._view[0].stable_key);page._sync_table_checks()
                title=page.table.item(0,2);check=page.table.item(0,0)
                page.refresh();self.wait(lambda:page._transcribed_read is None)
                self.assertIs(page.table.item(0,2),title)
                self.assertIs(page.table.item(0,0),check)
                self.assertIn(page._view[0].stable_key,page._selected_keys)
                first=page._view[0]
                with patch.object(library,'list_entries',return_value=[{'kind':'transcript','source':'https://www.douyin.com/video/'+first.aweme_id}]):
                    page.refresh();self.wait(lambda:page._transcribed_read is None)
                self.assertEqual(page.table.item(0,9).text(),'✓')
                self.assertIs(page.table.item(0,2),title)
            finally:page.shutdown_workers();page.close();page.deleteLater()

    def test_alpha_crop_preserves_threshold_sampling_and_row_padding(self):
        from src.ui.icons import _alpha_bbox
        from PySide6.QtCore import QRect
        for w,h in ((7,9),(256,256),(603,411)):
            image=QImage(w,h,QImage.Format_ARGB32);image.fill(QColor(0,0,0,0))
            image.setPixelColor(0,0,QColor(2,3,4,16))
            step=max(1,min(w,h)//200)
            image.setPixelColor(step,2*step,QColor(2,3,4,17))
            image.setPixelColor(3*step,4*step,QColor(2,3,4,255))
            self.assertEqual(_alpha_bbox(image),QRect(step,2*step,3*step,3*step))
            image.fill(QColor(0,0,0,0));self.assertIsNone(_alpha_bbox(image))
