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
    def test_document_and_table_share_dialog_preserve_input(self):
        s=self.session();d=s.dialog
        d.mode.setCurrentIndex(1);d.name.setText('用户输入')
        d.format.setCurrentIndex(1)
        self.assertTrue(d.location.isVisible());self.assertFalse(d.template.isVisible())
        d.format.setCurrentIndex(0);self.assertEqual(d.name.text(),'用户输入');self.assertTrue(d.template.isVisible())
    def test_existing_base_without_table_allows_choice_and_mapping(self):
        d=self.session().dialog;d.url.setText('https://my.feishu.cn/base/APP')
        with patch.object(saving,'discover_tables',return_value={'app_token':'APP','url':d.url.text(),'table_id':'','tables':[{'table_id':'a','name':'一'},{'table_id':'b','name':'二'}]}):
            d._discover();settle(d)
        self.assertEqual(d.table.count(),3)
        with patch.object(saving,'table_fields',return_value=[{'field_id':'f','field_name':'视频名称','type':1}]):
            d.table.setCurrentIndex(2);settle(d)
        selection=d._selection()
        self.assertEqual(selection['table_id'],'b');self.assertEqual(selection['field_map']['title']['field_id'],'f')
        self.assertFalse(d.location.isVisible())
    def test_homepage_is_same_form_and_no_second_content_picker(self):
        d=self.session('homepage').dialog
        self.assertEqual(d.template.currentData(),'research')
        self.assertFalse(hasattr(d,'selected_works_table'))
    def test_small_window_keeps_footer_and_scrolls(self):
        d=self.session().dialog;d.resize(540,500);d.mode.setCurrentIndex(1);APP.processEvents()
        self.assertLess(d.save_button.geometry().bottom(),d.height())
        self.assertGreater(d.scroll.verticalScrollBar().maximum(),0)
    def test_management_creates_empty_library_only(self):
        d=self.session('create',()).dialog
        self.assertEqual(d.mode.currentData(),'new');self.assertFalse(d.format.isVisible());self.assertEqual(d.records,())
    def test_connection_page_has_no_library_or_destination_controls(self):
        from PySide6.QtWidgets import QPushButton, QComboBox
        with patch('src.ui.feishu_connection.secrets.configuration_loaded',return_value=False), patch('src.ui.feishu_connection.secrets.get_config') as read:
            w=FeishuConnectionPanel(self.cfg);w.resize(680,600);w.show();APP.processEvents()
            read.assert_not_called()
            texts=[b.text() for b in w.findChildren(QPushButton)]
            self.assertFalse(set(texts)&{'新建作品库','添加已有表格','管理预设'})
            self.assertEqual(w.findChildren(QComboBox),[])
            w.close()



class ResultUITests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session
    def test_many_errors_scroll_without_growing_dialog(self):
        d=self.session().dialog;d.resize(540,500);d._last_selection={'remember':False}
        d._saved(saving.SaveResult(100,row_failed=100,errors=['某条内容未完成，请重试']*100));APP.processEvents()
        self.assertEqual(d.height(),500);self.assertLess(d.save_button.geometry().bottom(),d.height())
        self.assertGreater(d.result_details.verticalScrollBar().maximum(),0)
        self.assertTrue(d.adjust_button.isVisible());d._adjust();self.assertTrue(d.format.isEnabled())
    def test_stop_still_delivers_completed_results(self):
        from threading import Event
        s=self.session();d=s.dialog;d._last_selection={'remember':False};delivered=[];s.done.connect(lambda ok,result:delivered.append(result))
        ready=Event();release=Event()
        def call():ready.set();release.wait(1);return saving.SaveResult(2,row_success=1,row_cancelled=1)
        d._run(call,d._saved);self.assertTrue(ready.wait(1));d.reject();release.set();settle(d)
        self.assertEqual(len(delivered),1);self.assertTrue(d.isVisible());self.assertIn('已停止 1',d.status.text())
    def test_config_failure_does_not_claim_remembered(self):
        s=self.session();d=s.dialog;self.cfg.save=lambda:False;d._last_selection={'remember':True,'mode':'existing'}
        target={'name':'库','url':'https://my.feishu.cn/base/A?table=T','app_token':'A','table_id':'T','field_map':{}}
        d._saved(saving.SaveResult(1,row_success=1,target=target));APP.processEvents()
        self.assertIn('未能记住',d.result_details.toPlainText());self.assertEqual(self.cfg.feishu_last_save,{})
    def test_partial_attachments_are_not_success_toast(self):
        s=self.session();d=s.dialog;notices=[];s.toast=notices.append;d._last_selection={'remember':False}
        d._saved(saving.SaveResult(1,row_success=1,attachment_failed=1,errors=['封面未完成']))
        self.assertIn('附件待补充',notices[0]);self.assertEqual(d.save_button.text(),'重试未完成项')

class RedesignedSurfaceTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session

    def test_cards_work_with_keyboard_and_preserve_inputs(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        d=self.session().dialog
        d.mode.buttons[1].click();d.name.setText('保留我的名称')
        d.format.buttons[1].setFocus();QTest.keyClick(d.format.buttons[1],Qt.Key_Space)
        self.assertEqual(d.format.currentData(),'document')
        self.assertFalse(d.template.isVisible());self.assertTrue(d.location.isVisible())
        d.format.buttons[0].click()
        self.assertEqual(d.name.text(),'保留我的名称')
        self.assertEqual(d.mode.currentData(),'new')

    def test_single_column_resize_preserves_form_and_has_no_overview(self):
        d=self.session().dialog;d.resize(760,810);d.mode.setCurrentIndex(1)
        d.name.setText('我的库');d.repeat.setCurrentIndex(1);APP.processEvents()
        self.assertFalse(hasattr(d,'rail'));self.assertFalse(hasattr(d,'overview_button'))
        for width,height in ((540,500),(1020,760),(760,810)):
            d.resize(width,height);APP.processEvents()
            self.assertEqual(d.size().toTuple(),(width,height))
            self.assertLessEqual(d.body.width(),d.scroll.viewport().width())
            self.assertEqual(d.name.text(),'我的库');self.assertEqual(d.repeat.currentData(),'copy')

    def test_long_mapping_names_do_not_expand_window(self):
        d=self.session().dialog;d.resize(540,500)
        d.discovery={'app_token':'APP'};d.fields=[{'field_id':'f','field_name':'很长的自定义作品标题'*12,'type':1}]
        d._mapping_widgets({'title':{'field_id':'f','type':1}})
        d.mapping_body.show();d._layout();APP.processEvents()
        self.assertEqual(d.width(),540)
        self.assertLessEqual(d.body.width(),d.scroll.viewport().width())
        self.assertGreater(d.scroll.verticalScrollBar().maximum(),0)

    def test_partial_result_actions_fit_small_window(self):
        from PySide6.QtCore import QPoint
        d=self.session().dialog;d.resize(540,500);d._last_selection={'remember':False}
        d._saved(saving.SaveResult(100,row_success=2,row_failed=98,urls=['https://my.feishu.cn/base/A'],errors=['未完成']*98))
        APP.processEvents()
        self.assertEqual(d.size().toTuple(),(540,500))
        for button in (d.open_button,d.adjust_button,d.cancel_button,d.save_button):
            end=button.mapTo(d,QPoint(button.width(),button.height()))
            self.assertLess(end.x(),d.width());self.assertLess(end.y(),d.height())
        self.assertGreater(d.result_details.verticalScrollBar().maximum(),0)

    def test_long_dropdown_is_bounded_scrollable_and_last_option_selectable(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        d=self.session().dialog;d.resize(540,500);d.mode.setCurrentIndex(1)
        combo=d.template
        for i in range(40):combo.addItem('很长的自定义模板名称'*10+str(i),str(i))
        APP.processEvents();before=d.size()
        combo.showPopup();APP.processEvents()
        view=combo.view();popup=view.window()
        self.assertLessEqual(popup.width(),combo.width())
        self.assertLessEqual(popup.height(),268)
        self.assertTrue(combo.screen().availableGeometry().contains(popup.geometry()))
        self.assertGreater(view.verticalScrollBar().maximum(),0)
        QTest.keyClick(view,Qt.Key_End);QTest.keyClick(view,Qt.Key_Return);APP.processEvents()
        self.assertEqual(combo.currentData(),'39');self.assertEqual(d.size(),before)
        self.assertFalse(view.isVisible())

    def test_scrolling_over_closed_dropdown_does_not_change_choice(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        d=self.session().dialog;d.mode.setCurrentIndex(1);APP.processEvents()
        combo=d.template;before=combo.currentData()
        event=QWheelEvent(QPointF(10,10),QPointF(combo.mapToGlobal(QPoint(10,10))),
                          QPoint(),QPoint(0,-120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        APP.sendEvent(combo,event)
        self.assertEqual(combo.currentData(),before)
        self.assertFalse(event.isAccepted())

    def test_form_width_stays_stable_when_scroll_is_needed(self):
        d=self.session().dialog;d.resize(760,760);d.mode.setCurrentIndex(1)
        APP.processEvents();width=d.scroll.viewport().width()
        d.format.setCurrentIndex(1);APP.processEvents()
        self.assertEqual(d.scroll.viewport().width(),width)
        d.format.setCurrentIndex(0);d.location.setCurrentIndex(d.location.count()-1)
        APP.processEvents()
        self.assertEqual(d.scroll.viewport().width(),width)
        self.assertEqual(d.size().toTuple(),(760,760))

    def test_empty_management_has_no_source_or_repeat(self):
        d=self.session('create',()).dialog
        self.assertFalse(d.source_strip.isVisible());self.assertFalse(d.repeat_section.isVisible())
        self.assertEqual(d.heading.text(),'新建作品库');self.assertEqual(d.save_button.text(),'创建空库')

class SaveLogicAuditTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session

    def test_mapping_only_offers_data_present_in_selected_records(self):
        d=self.session(records=(WorkArchiveRecord(title='抖音作品',platform='douyin',like_count=0),)).dialog
        d.fields=[{'field_id':'t','field_name':'标题','type':1}]
        d._mapping_widgets({})
        self.assertEqual(set(d.map_widgets),{'title','like_count','platform'})
        d.records=(WorkArchiveRecord(title='B站作品',platform='bilibili',coin_count=0,danmaku_count=8),)
        d._mapping_widgets({})
        self.assertEqual(set(d.map_widgets),{'title','coin_count','danmaku_count','platform'})

    def test_nonessential_metadata_hidden_even_when_available_and_previously_mapped(self):
        record=WorkArchiveRecord(title='作品',author_name='作者',published_at_ms=1700000000000,
            captured_at_ms=1800000000000,work_id='id',author_id='handle',author_fans=123,
            author_total_likes=456,author_works=78,language='zh')
        d=self.session(records=(record,)).dialog
        excluded={'work_id','author_id','author_fans','author_total_likes','author_works','language'}
        d._mapping_widgets({key:{'field_id':key,'type':1} for key in excluded})
        self.assertEqual(set(d.map_widgets),{'title','author_name','published_at_ms','captured_at_ms'})
        self.assertNotIn('作品ID',d.mapping_summary.text())
        self.assertNotIn('语种',d.mapping_summary.text())

    def test_media_cover_and_source_links_are_available_separately(self):
        record=WorkArchiveRecord(title='完整作品',work_url='https://example.com/work',
            media_url='https://example.com/video.mp4',cover_url='https://example.com/cover.jpg',
            cover_local_path='covers/cover.jpg',media_local_path='media/video.mp4')
        d=self.session(records=(record,)).dialog
        d.fields=[{'field_id':'f'+str(i),'field_name':name,'type':kind} for i,(name,kind) in enumerate(
            [('标题',1),('原作品链接',15),('音视频直链',15),('封面链接',15),('封面',17),('音视频附件',17)])]
        from src.core import feishu_mapping as mapping
        d._mapping_widgets(mapping.suggest_mapping(d.fields)[0])
        self.assertEqual(set(d.map_widgets),{'title','work_url','media_url','cover_url','cover_local_path','media_local_path'})
        self.assertTrue(all(widget.currentData() for widget in d.map_widgets.values()))

    def test_batch_mapping_includes_data_from_any_selected_record(self):
        d=self.session(records=(WorkArchiveRecord(title='第一条'),WorkArchiveRecord(title='第二条',summary='已生成摘要'))).dialog
        d._mapping_widgets({})
        self.assertEqual(set(d.map_widgets),{'title','summary'})

    def test_popup_has_transparent_corners_and_lulu_paper(self):
        from PySide6.QtCore import Qt
        from src.ui.control_foundation import PaperTextureFrame
        d=self.session().dialog;d.mode.setCurrentIndex(1);APP.processEvents()
        d.template.showPopup();APP.processEvents()
        popup=d.template.view().window()
        self.assertTrue(popup.testAttribute(Qt.WA_TranslucentBackground))
        self.assertIsNotNone(popup.findChild(PaperTextureFrame))
        shot=popup.grab().toImage()
        self.assertEqual(shot.pixelColor(0,0).alpha(),0)
        self.assertGreater(shot.pixelColor(shot.width()//2,shot.height()//2).alpha(),240)
        selected=shot.pixelColor(shot.width()//2,int(20*shot.devicePixelRatio()))
        self.assertGreater(selected.red(),100)
        self.assertGreater(selected.green(),100)
        d.template.hidePopup()

    def loaded(self):
        d=self.session().dialog
        d.discovery={'app_token':'APP'}
        d.fields=[{'field_id':'title','field_name':'标题','type':1}]
        d.table.blockSignals(True);d.table.addItem('作品','T');d.table.setCurrentIndex(1);d.table.blockSignals(False)
        d._mapping_widgets({'title':{'field_id':'title','type':1}});d._layout()
        return d

    def test_account_reload_clears_previous_target_and_mapping(self):
        d=self.loaded();d.url.setText('https://my.feishu.cn/base/OLD')
        d._load_account();settle(d)
        self.assertFalse(d.discovery);self.assertFalse(d.fields);self.assertEqual(d.url.text(),'')
        self.assertFalse(d.mapping_card.isVisible())

    def test_failed_reread_cannot_use_previous_fields(self):
        d=self.loaded();d.url.setText('https://my.feishu.cn/base/APP')
        with patch.object(saving,'discover_tables',side_effect=ValueError('无法读取')):
            d._discover();settle(d)
        self.assertFalse(d.fields);self.assertFalse(d.mapping_card.isVisible())
        with self.assertRaises(ValueError):d._selection()

    def test_no_table_selection_hides_previous_mapping(self):
        d=self.loaded();d.table.setCurrentIndex(0);APP.processEvents()
        self.assertFalse(d.mapping_card.isVisible())

    def test_mapping_toggle_has_clear_collapse_action(self):
        d=self.loaded();d.edit_mapping.click();APP.processEvents()
        self.assertIn('收起',d.edit_mapping.text())
        d.edit_mapping.click();self.assertFalse(d.mapping_body.isVisible())

    def test_complete_result_has_one_dismiss_action(self):
        d=self.loaded();d._last_selection={'remember':False}
        d._saved(saving.SaveResult(1,row_success=1,urls=['https://my.feishu.cn/base/A']))
        APP.processEvents()
        self.assertEqual(d.save_button.text(),'完成');self.assertFalse(d.cancel_button.isVisible())
        self.assertTrue(d.open_button.isVisible());self.assertFalse(d.connect_button.isEnabled())

    def test_existing_duplicate_mapping_with_new_field_rejected_locally(self):
        d=self.loaded()
        d.records=(WorkArchiveRecord(title='测试作品',description='作品简介',like_count=0),)
        d._mapping_widgets({'title':{'field_id':'title','type':1}})
        for key in ('title','description'):
            c=d.map_widgets[key];c.setCurrentIndex(c.findData({'field_id':'title','type':1}))
        c=d.map_widgets['like_count'];c.setCurrentIndex(c.count()-1)
        with self.assertRaisesRegex(ValueError,'同一列'):d._selection()

    def test_mapping_summary_names_available_but_skipped_content(self):
        d=self.loaded();d._mapping_summary()
        self.assertIn('转录文稿',d.mapping_summary.text())

    def test_empty_common_target_has_no_redundant_dropdown(self):
        d=self.session().dialog
        self.assertFalse(d.target.isVisible());self.assertTrue(d.url.isVisible())
        self.assertTrue(d.read_button.isVisible())

    def test_single_data_table_is_selected_after_read(self):
        d=self.session().dialog;d.url.setText('https://my.feishu.cn/base/APP')
        with patch.object(saving,'discover_tables',return_value={'app_token':'APP','table_id':'','tables':[{'table_id':'only','name':'作品'}]}), patch.object(saving,'table_fields',return_value=[{'field_id':'t','field_name':'标题','type':1}]):
            d._discover();settle(d)
        self.assertEqual(d.table.currentData(),'only');self.assertTrue(d.fields)

    def test_failed_account_check_has_reconnect_action(self):
        d=self.session().dialog
        with patch.object(saving,'account_info',side_effect=ValueError('请登录')):
            d._load_account();settle(d)
        self.assertIn('未连接',d.account_label.text())
        self.assertTrue(d.connect_button.isEnabled());self.assertFalse(d.save_button.isEnabled())

    def test_manage_target_cannot_add_columns_or_opt_out_of_remembering(self):
        d=self.session('manage',()).dialog
        d.discovery={'app_token':'APP'};d.fields=[{'field_id':'t','field_name':'标题','type':1}]
        d.table.blockSignals(True);d.table.addItem('作品','T');d.table.setCurrentIndex(1);d.table.blockSignals(False)
        d._mapping_widgets({'title':{'field_id':'t','type':1}});d._layout()
        self.assertFalse(hasattr(d,"remember"))
        self.assertTrue(d._selection()['remember'])
        self.assertFalse(any(c.itemData(i) and c.itemData(i).get('create') for c in d.map_widgets.values() for i in range(c.count())))

    def test_format_card_icon_and_label_are_clickable(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel
        d=self.session().dialog
        button=d.format.buttons[1]
        for label in button.findChildren(QLabel):
            d.format.setCurrentIndex(0)
            QTest.mouseClick(button,Qt.LeftButton,pos=label.mapTo(button,label.rect().center()))
            self.assertEqual(d.format.currentData(),'document')

    def test_dropdown_click_selects_and_escape_keeps_previous_value(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        d=self.session().dialog;d.mode.setCurrentIndex(1);APP.processEvents()
        combo=d.template;combo.showPopup();APP.processEvents();view=combo.view()
        QTest.mouseClick(view.viewport(),Qt.LeftButton,pos=view.visualRect(combo.model().index(1,0)).center())
        self.assertEqual(combo.currentData(),'research');self.assertFalse(view.isVisible())
        combo.showPopup();QTest.keyClick(view,Qt.Key_Up);QTest.keyClick(view,Qt.Key_Escape)
        self.assertEqual(combo.currentData(),'research')

    def test_manual_url_edit_removes_old_common_target_label(self):
        d=self.loaded()
        d.target.blockSignals(True);d.target.addItem('旧表格',{'table_id':'T','url':'https://my.feishu.cn/base/OLD'});d.target.setCurrentIndex(1);d.target.blockSignals(False)
        d.url.setText('https://my.feishu.cn/base/NEW');d.url.textEdited.emit(d.url.text())
        self.assertFalse(d.target.currentData());self.assertFalse(d.fields)

if __name__=='__main__':unittest.main()


class LastDestinationTests(unittest.TestCase):
    setUp = UnifiedUITests.setUp
    tearDown = UnifiedUITests.tearDown
    session = UnifiedUITests.session

    def test_last_destination_survives_config_reload(self):
        import tempfile
        from pathlib import Path
        from src.config import AppConfig
        last={'account_id':'test','format':'table','table':{'name':'选题库','url':'https://my.feishu.cn/base/A','app_token':'A','table_id':'T','field_map':{'title':{'field_id':'f','type':1}}},'location':{'destination_kind':'drive','folder_token':'folder','name':'云文档'}}
        with tempfile.TemporaryDirectory() as folder, patch('src.config._CONFIG_PATH',Path(folder)/'config.json'):
            cfg=AppConfig();cfg.feishu_last_save=last
            self.assertTrue(cfg.save())
            self.assertEqual(AppConfig.load().feishu_last_save,last)

    def test_success_replaces_last_table_without_growing_library(self):
        d=self.session().dialog
        self.cfg.feishu_table_targets=[{'name':'旧配置保留'}]
        d._last_selection={'format':'table','mode':'existing'}
        for name in ['A','B']:
            target={'name':name,'url':'https://my.feishu.cn/base/'+name,'app_token':name,'table_id':'T','field_map':{'title':{'field_id':'f','type':1}}}
            self.assertTrue(d._remember_target(target))
        self.assertEqual(self.cfg.feishu_last_save['table']['name'],'B')
        self.assertEqual(self.cfg.feishu_table_targets,[{'name':'旧配置保留'}])
        self.assertFalse(hasattr(d,'remember'))

    def test_last_table_restores_and_revalidates_live_columns(self):
        self.cfg.feishu_last_save={'account_id':'test','format':'table','table':{'name':'上次的表','url':'https://my.feishu.cn/base/A?table=T','table_id':'T','app_token':'A','field_map':{'title':{'field_id':'f','type':1}}}}
        with patch.object(saving,'discover_tables',return_value={'app_token':'A','table_id':'T','tables':[{'name':'素材','table_id':'T'}]}), patch.object(saving,'table_fields',return_value=[{'field_name':'改名后的标题','field_id':'f','type':1}]) as fields:
            d=self.session().dialog
            self.assertEqual(d.table.currentData(),'T');self.assertEqual(d._selection()['field_map']['title']['field_id'],'f')
            fields.assert_called_once()

    def test_other_account_never_inherits_last_destination(self):
        self.cfg.feishu_last_save={'account_id':'other','table':{'name':'私有表','url':'https://my.feishu.cn/base/OTHER'},'location':{'destination_kind':'wiki','space_id':'old'}}
        d=self.session().dialog
        self.assertEqual(d.target.count(),1);self.assertEqual(d.url.text(),'');self.assertEqual(d.location.currentData(),{})

    def test_failure_does_not_replace_last_destination(self):
        d=self.session().dialog;self.cfg.feishu_last_save={'account_id':'test','table':{'name':'保留'}}
        d._last_selection={'format':'table','mode':'existing'}
        d._saved(saving.SaveResult(1,row_failed=1,target={'name':'失败的表'}))
        self.assertEqual(self.cfg.feishu_last_save['table']['name'],'保留')
