"""Isolated Qt checks for the unified Feishu surface; fake account and API only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer,QEventLoop
from src.ui.feishu_save_dialog import FeishuSaveSession
from src.ui.feishu_connection import FeishuConnectionPanel
from src.core.feishu_storage import WorkArchiveRecord
from src.core import feishu_save as saving


APP=QApplication.instance() or QApplication([])
def settle(dialog):
    loop=QEventLoop();timer=QTimer();timer.setInterval(5)
    timer.timeout.connect(lambda:loop.quit() if not dialog.busy else None)
    timer.start();QTimer.singleShot(1500,loop.quit);loop.exec();timer.stop()
    assert not dialog.busy


class UnifiedUITests(unittest.TestCase):
    def setUp(self):
        self.cfg=SimpleNamespace(feishu_last_save={},feishu_table_targets=[],feishu_storage_preferences={},feishu_creation_presets=[],feishu_creation_target={},feishu_target={},save=lambda:True)
        self.account=patch.object(saving,'account_info',return_value={'account_id':'test','name':'测试账号'})
        self.account.start();self.addCleanup(self.account.stop)
        self.sessions=[]
    def tearDown(self):
        for s in self.sessions:
            settle(s.dialog);s.dialog.reject()
        APP.processEvents()
    def session(self,context='single',records=None):
        records=(WorkArchiveRecord(title='测试作品',transcript_text='正文'),) if records is None else records
        s=FeishuSaveSession(None,self.cfg,records,'测试作品',context);self.sessions.append(s);s.start();settle(s.dialog);return s



class ResultUITests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session

class RedesignedSurfaceTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session

    def test_accessible_toggle_updates_form_and_save_selection(self):
        from PySide6.QtGui import QAccessible, QAccessibleActionInterface
        d = self.session().dialog
        for control, expected in ((d.mode, 'new'), (d.format, 'document'), (d.repeat, 'copy')):
            action = QAccessible.queryAccessibleInterface(control.buttons[1]).actionInterface()
            self.assertIn(QAccessibleActionInterface.toggleAction(), action.actionNames())
            action.doAction(QAccessibleActionInterface.toggleAction())
            APP.processEvents()
            self.assertEqual(control.currentData(), expected)
            self.assertTrue(control.buttons[1].isChecked())
        self.assertFalse(d.url.isVisible())
        self.assertFalse(d.template.isVisible())
        self.assertTrue(d.name.isVisible())
        self.assertEqual(d._selection()['format'], 'document')
        self.assertEqual(d._selection()['repeat_mode'], 'copy')

    def test_named_saved_location_cannot_replace_requested_content_name(self):
        d = self.session().dialog
        d.location.insertItem(1, 'Saved folder', {'name': 'Saved folder', 'destination_kind': 'drive', 'folder_token': 'FOLDER'})
        d.location.setCurrentIndex(1)
        d.mode.setCurrentIndex(1)
        for format_index in (0, 1):
            d.format.setCurrentIndex(format_index)
            d.name.setText('Requested content name')
            selection = d._selection()
            self.assertEqual(selection['name'], 'Requested content name')
            self.assertEqual(selection['folder_token'], 'FOLDER')
            self.assertEqual(selection['destination_kind'], 'drive')

    def test_resolved_location_keeps_requested_document_name(self):
        d = self.session().dialog
        d.format.setCurrentIndex(1)
        d.name.setText('Requested document name')
        d.location.setCurrentIndex(d.location.count() - 1)
        d.location_url.setText('https://my.feishu.cn/wiki/NODE')
        location = {'name': 'Wiki location title', 'destination_kind': 'wiki', 'space_id': 'SPACE', 'node_token': 'NODE'}
        with patch.object(saving, 'resolve_location', return_value=location), patch.object(saving, 'save_records', return_value=saving.SaveResult(1, row_failed=1)) as save:
            d._save(); settle(d)
        selection = save.call_args.args[1]
        self.assertEqual(selection['name'], 'Requested document name')
        self.assertEqual(selection['space_id'], 'SPACE')









class SaveLogicAuditTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session






    def loaded(self):
        d=self.session().dialog
        d.discovery={'app_token':'APP'}
        d.fields=[{'field_id':'title','field_name':'标题','type':1}]
        d.table.blockSignals(True);d.table.addItem('作品','T');d.table.setCurrentIndex(1);d.table.blockSignals(False)
        d._mapping_widgets({'title':{'field_id':'title','type':1}});d._layout()
        return d















if __name__=='__main__':unittest.main()


class LastDestinationTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session




    def test_document_restore_does_not_wait_for_unrelated_previous_table(self):
        self.cfg.feishu_last_save={'account_id':'test','format':'document','table':{'name':'上次的表','url':'https://my.feishu.cn/base/A?table=T','table_id':'T','app_token':'A','field_map':{'title':{'field_id':'f','type':1}}}}
        with patch.object(saving,'discover_tables',return_value={'app_token':'A','table_id':'T','tables':[{'name':'素材','table_id':'T'}]}) as discover, patch.object(saving,'table_fields',return_value=[{'field_name':'标题','field_id':'f','type':1}]) as fields:
            d=self.session().dialog
            self.assertEqual(d.format.currentData(),'document')
            discover.assert_not_called();fields.assert_not_called()
            self.assertNotIn('列对应',d.status.text())
            self.assertEqual(d.url.text(),'https://my.feishu.cn/base/A?table=T')
            d.format.setCurrentIndex(0);settle(d)
            discover.assert_called_once();fields.assert_called_once()
            self.assertEqual(d._selection()['field_map']['title']['field_id'],'f')
            d.format.setCurrentIndex(1);d.format.setCurrentIndex(0);settle(d)
            discover.assert_called_once();fields.assert_called_once()

    def test_new_table_does_not_fetch_remembered_existing_table(self):
        self.cfg.feishu_last_save={'account_id':'test','format':'document','table':{'url':'https://my.feishu.cn/base/A?table=T','table_id':'T','app_token':'A'}}
        with patch.object(saving,'discover_tables',return_value={'app_token':'A','table_id':'T','tables':[{'name':'素材','table_id':'T'}]}) as discover, patch.object(saving,'table_fields',return_value=[{'field_name':'标题','field_id':'f','type':1}]):
            d=self.session().dialog;d.mode.setCurrentIndex(1);d.format.setCurrentIndex(0);settle(d)
            discover.assert_not_called()
            d.mode.setCurrentIndex(0);settle(d)
            discover.assert_called_once()
