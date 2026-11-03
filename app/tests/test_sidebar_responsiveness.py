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





    def test_rapid_collection_returns_publish_only_on_gui_and_retire_all_readers(self):
        from src.ui.collection_page import CollectionPage
        from src.core import library
        from PySide6.QtCore import QCoreApplication,QEvent
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d)),patch.object(library,'list_entries',return_value=[]):
            page=CollectionPage();updates=[];gui=threading.get_ident()
            page._items=[object()]
            page._fill_table=lambda **_:updates.append(threading.get_ident())
            try:
                for n in range(150):
                    page._transcribed={'old'}
                    page._request_transcribed_read();page._request_transcribed_read()
                    self.wait(lambda:page._transcribed_read is None)
                    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
                self.assertTrue(updates)
                self.assertEqual(set(updates),{gui})
                self.assertFalse(page._transcribed_poll.isActive())
            finally:page.shutdown_workers();page.close();page.deleteLater()
