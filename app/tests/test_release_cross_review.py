"""Release-review counterexamples. Synthetic callbacks, local files, no accounts."""
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWidgets import QApplication, QLabel
from src.core.collect.models import CollectedItem
from src.core.live_recording.comments import CommentLog, read_comments
from src.ui.collection_page import CollectionPage
from src.ui.approved_collection import ApprovedCollection
from src.ui.approved_live_workspace import ApprovedLiveWorkspace
from tests.test_live_comments import chat


class Signal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self, *args):
        for callback in self.callbacks: callback(*args)


class DeferredWorker:
    def __init__(self, call):
        self.call = call
        self.result, self.finished = Signal(), Signal()
    def start(self): pass
    def isRunning(self): return True
    def deleteLater(self): pass
    def finish(self):
        self.result.emit(True, self.call())
        self.finished.emit()


class ReleaseCrossReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_late_feishu_preparation_cannot_open_after_record_switch(self):
        item = CollectedItem('1002', work_id='1002', title='乙作品', author_name='样例')
        for mode in ('different_record','away_and_back','shutdown'):
            with self.subTest(mode=mode):
                shown = []
                owner = NS(status=QLabel(), cfg=NS(), _shutting_down=False,
                    _history_selection_generation=0,
                    _history_snapshot=NS(selected_record_id='乙',selected_record=NS(name='乙记录')),
                    _checked_items=lambda: [item], _transcript_text_map=lambda: {},
                    _open_feishu_save=lambda *args: shown.append(args))
                with patch('src.ui.feishu_save_dialog._CallWorker', DeferredWorker):
                    CollectionPage._on_export_bitable(owner)
                    if mode == 'different_record':
                        owner._history_snapshot=NS(selected_record_id='甲',selected_record=NS(name='甲记录'))
                    elif mode == 'away_and_back':
                        owner._history_selection_generation=2
                    else:
                        owner._shutting_down=True
                    owner._feishu_prepare_worker.finish()
                self.assertEqual(shown, [])
                if mode != 'shutdown':
                    self.assertIn('记录已切换', owner.status.text())

    def test_feishu_preparation_keeps_record_name_for_same_author(self):
        shown = []
        item = CollectedItem('1002', work_id='1002', title='乙作品', author_name='同一作者')
        owner = NS(status=QLabel(), cfg=NS(), _shutting_down=False,
            _history_snapshot=NS(selected_record_id='乙',selected_record=NS(name='乙记录')),
            _checked_items=lambda: [item], _transcript_text_map=lambda: {},
            _open_feishu_save=lambda *args: shown.append(args))
        with patch('src.ui.feishu_save_dialog._CallWorker', DeferredWorker):
            CollectionPage._on_export_bitable(owner)
            owner._feishu_prepare_worker.finish()
        self.assertIn('乙记录', shown[0][1])
        self.assertEqual(shown[0][0][0].work_id, '1002')

    def test_legacy_comment_keys_survive_search_page_append_and_distinguish_duplicates(self):
        with TemporaryDirectory() as folder:
            recording={'id':'r', 'directory':str(Path(folder).resolve())}
            log=CommentLog(recording)
            rows=[dict(chat(1,text=text),message_id='') for text in ('甲','乙','乙')]
            log.append(rows)
            all_rows=read_comments(recording)['messages']
            self.assertEqual(len({r['row_key'] for r in all_rows}),3)
            selected=read_comments(recording,'乙')['messages']
            self.assertEqual([r['row_key'] for r in selected],[r['row_key'] for r in all_rows[1:]])
            log.append([chat(4)])
            self.assertEqual(read_comments(recording)['messages'][1]['row_key'],selected[0]['row_key'])
            path=next(Path(folder).rglob('*.jsonl'))
            before=path.read_bytes();path.write_bytes(before.replace('甲'.encode(),'丙'.encode()))
            self.assertNotEqual(read_comments(recording)['messages'][0]['row_key'],all_rows[0]['row_key'])

    def test_comment_projection_does_not_reuse_a_key_for_different_content(self):
        def project(text):
            owner=NS(_data_folder=('r','fixture'),_ready_session='s',controller=NS(session='s'))
            owner._call=lambda done,op,project,**kwargs: done(project(dict(messages=[chat(1,text=text)|{'message_id':''}],
                total=2,matching=1,page=1,pages=1,comments_state='ended',read_error=False)))
            values=[]
            ApprovedLiveWorkspace._request_comments(owner,values.append,id='r',query=text,page=1)
            return values[0]['messages'][0]
        self.assertNotEqual(project('甲')['key'],project('乙')['key'])

    def test_failed_collection_transcript_keeps_its_failure_state(self):
        item=CollectedItem('1')
        owner=NS(_transcribed=set(),_material_progress={},_material_cell_state=lambda *args:('missing',''))
        actual=ApprovedCollection._work_status(owner,item,{item.stable_key:NS(transcript='failed')})
        self.assertEqual(actual['text'],'failed')

    def test_collection_duration_matches_export_rounding(self):
        for milliseconds in (500,1500,2500,3500,59999):
            item=CollectedItem('1',duration_ms=milliseconds)
            owner=NS(_items=[item],_work_task_states=lambda:{},_work_status=lambda *args:{})
            row=ApprovedCollection._work_rows(owner)[0]
            self.assertEqual(row['durationText'],item.duration_text)
            self.assertEqual(row['duration_ms'],milliseconds)

    def test_rejected_feishu_credentials_stay_rejected_after_memory_cache_reset(self):
        from src.core import feishu
        config=dict(app_id='fixture',app_secret='fixture-secret',refresh_token='fixture-token')
        stored=dict(config)
        def save(_key,value): stored.clear();stored.update(value)
        with patch.object(feishu.secrets,'get_config',side_effect=lambda *a,**k:dict(stored)), \
             patch.object(feishu.secrets,'set_config',side_effect=save), \
             patch.dict(feishu._user_cache,{},clear=True), \
             patch.object(feishu,'_oauth_token',side_effect=feishu._RefreshTokenRejected('invalid_grant','fixture')):
            with self.assertRaises(feishu._RefreshTokenRejected):feishu._refresh()
            feishu._user_cache.clear()
            self.assertTrue(feishu.needs_reauthorization(config))
            self.assertFalse(feishu.needs_reauthorization(dict(config,refresh_token='different')))
            feishu._store(dict(access_token='fixture-access'))
            self.assertTrue(feishu.authorization_verified(config))
            feishu._user_cache.clear()
            self.assertFalse(feishu.needs_reauthorization(config))
            self.assertFalse(feishu.authorization_verified(config))

    def test_dialog_text_scales_once_and_returns_to_original_size(self):
        from src.ui import dialogs, theme
        theme.apply_fonts(self.app,130)
        dialog=dialogs._build(None,'标题','正文','确认',None)
        try:
            dialog.show();self.app.processEvents()
            labels=dialog.findChildren(QLabel)
            body=next(label for label in labels if label.text()=='正文')
            self.assertEqual(body.font().pixelSize(),17)
            dialog.hide();dialog.show();self.app.processEvents()
            self.assertEqual(body.font().pixelSize(),17)
            theme.apply_fonts(self.app,100);self.app.processEvents()
            self.assertEqual(body.font().pixelSize(),13)
        finally:
            dialog.close();dialog.deleteLater();theme.apply_fonts(self.app,100)

    def test_non_cocoa_window_ids_never_reach_native_pointer_calls(self):
        from src.ui import macos
        for platform in ('offscreen','minimal'):
            with self.subTest(platform=platform), \
                    patch.object(macos.QGuiApplication,'instance',return_value=NS(platformName=lambda:platform)), \
                    patch.object(macos.ctypes.cdll,'LoadLibrary') as load:
                self.assertFalse(macos.activate_regular_app())
                self.assertFalse(macos.unify_titlebar(object()))
                self.assertFalse(macos.set_page_titlebar_immersive(object(),True))
                self.assertIsNone(macos.native_safe_area_top(object()))
                load.assert_not_called()

    def test_font_drag_coalesces_window_repaints_and_keeps_last_choice(self):
        from PySide6.QtWidgets import QWidget
        from src.ui.main_window import MainWindow
        widget=QWidget();values=[]
        widget._apply_interface_font=values.append
        try:
            for value in range(90,131):MainWindow._schedule_interface_font(widget,value)
            self.assertEqual(values,[])
            loop=QEventLoop();QTimer.singleShot(90,loop.quit);loop.exec()
            self.assertEqual(values,[130])
        finally:widget.deleteLater()

    def test_attention_tracks_changed_work_even_when_unknown_count_is_unchanged(self):
        from copy import deepcopy
        from src.ui.extraction.batches import WorkspaceBatches
        notices=[]
        owner=NS(views={},pages={},selections={},_notice=lambda _:None,
            main=NS(isActiveWindow=lambda:False,completion_feedback=NS(
                needs_attention=lambda **kwargs:notices.append(kwargs))))
        base=dict(batch_id='b',items=[],workspace={},counts={},engine='dashscope',
                  plan_hash='fixture',visible_total=2,total=2,offset=0,unconfirmed_count=0,
                  attention_keys=[])
        for keys in ([],['0:a'],['0:a'],['1:b'],[],[]):
            row=deepcopy(base);row.update(attention_keys=keys,unconfirmed_count=len(keys))
            WorkspaceBatches._accept(owner,row)
        self.assertEqual(len(notices),2)
        owner.views.clear()
        WorkspaceBatches._accept(owner,dict(base,attention_keys=['1:b'],unconfirmed_count=1))
        self.assertEqual(len(notices),2)


class ReleaseFrontendTests(unittest.TestCase):
    """Execute the shipped functions in Chromium; no network or service calls."""
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        from src.ui.agent_workspace.runtime import prepare_webengine_runtime
        prepare_webengine_runtime()
        self.view=QWebEngineView()
        profile=QWebEngineProfile(self.app)
        page=QWebEnginePage(profile,self.view)
        page.destroyed.connect(profile.deleteLater)
        self.view.setPage(page)
        ready=[]
        self.view.loadFinished.connect(ready.append)
        self.view.setHtml('<!doctype html><html><body></body></html>')
        self.until(lambda: bool(ready))

    def tearDown(self):
        self.view.close();self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.app.processEvents()

    def until(self, predicate):
        end=time.monotonic()+10
        while time.monotonic()<end:
            if predicate(): return
            loop=QEventLoop();QTimer.singleShot(10,loop.quit);loop.exec()
        self.fail('Browser did not settle')

    def js(self, source):
        values=[]
        self.view.page().runJavaScript(source,values.append)
        self.until(lambda: bool(values))
        return values[0]

    def source(self, path):
        return (Path(__file__).parents[1]/'src/ui/assets/approved_workspaces'/path).read_text()

    def function(self, path, start, end):
        text=self.source(path);a=text.index(start)
        return text[a:text.index(end,a)]

    def test_relative_dates_follow_midnight_without_reloading(self):
        self.js("""const OriginalDate=Date;let now=Date.parse('2026-11-06T23:59:00+08:00');
        window.Date=class extends OriginalDate {constructor(...args){super(...(args.length?args:[now]))}static now(){return now}};""")
        self.js(self.source('collection/backend-contract.js'))
        self.js(self.source('collection/filters.js'))
        self.js("now=Date.parse('2026-11-07T00:01:00+08:00')")
        self.assertEqual(self.js('CollectionContract.today'),'2026-11-07')
        self.assertEqual(self.js("CollectionFilters.filter([{id:'today',create_time:now/1000}],{...CollectionFilters.initial(),date:'7'}).length"),1)

    def test_repeated_rename_submits_once_and_failure_allows_retry(self):
        self.js("""let managementEditing={key:'r',version:'v1'},records={r:{}},requests=[];
        const input={value:'新名字',isConnected:true,disabled:false};const $=()=>input;
        const perform=(op,args)=>new Promise(resolve=>requests.push({op,args,resolve}));
        const renderManagement=()=>{},managementFocus=()=>{},updateRecordState=()=>{};""")
        self.js(self.function('collection/collection.js','async function managementSaveName()',"$('#collectStop')"))
        self.js('managementSaveName();managementSaveName();')
        self.assertEqual(self.js('requests.length'),1)
        self.assertTrue(self.js('input.disabled'))
        self.js('requests[0].resolve(null)')
        self.until(lambda: self.js('!managementEditing.saving'))
        self.assertFalse(self.js('input.disabled'))
        self.js('managementSaveName()')
        self.assertEqual(self.js('requests.length'),2)
        self.js('requests[1].resolve({})')
        self.until(lambda: self.js('managementEditing===null'))

    def test_refused_navigation_preserves_displayed_page_and_secret(self):
        self.js("""let state={page:'feishu',feishu:{secret:'fixture',step:2}},navigationPending=false,pageAnimation;
        const pages={general:{title:'通用'},feishu:{title:'飞书'}},errors=[];
        const node={value:'',textContent:''},$=()=>node,$$=()=>[];
        const toast=t=>errors.push(t),renderCurrent=()=>{},moveNavInk=()=>{},loadSkills=()=>{},animateContent=()=>{};
        let api={call:async()=>{throw Error('请等待位置切换完成')}};""")
        self.js(self.function('settings/settings.js','async function navigate(', '\nfunction renderCurrent'))
        self.js("navigate('general')")
        self.until(lambda:self.js('errors.length===1'))
        self.assertEqual(self.js('state.page'),'feishu')
        self.assertEqual(self.js('state.feishu.secret'),'fixture')
        self.js("api.call=async()=>true;navigate('general')")
        self.until(lambda:self.js("state.page==='general'"))
        self.assertEqual(self.js('state.feishu.secret'),'')


class ReleaseCollectionPageTests(unittest.TestCase):
    setUpClass=ReleaseFrontendTests.__dict__['setUpClass']
    until=ReleaseFrontendTests.until
    js=ReleaseFrontendTests.js

    def setUp(self):
        from src.config import AppConfig
        from src.core.collect.history import CollectionHistoryRepository
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.repo=CollectionHistoryRepository(self.root/'history.json')
        count=1500 if 'virtual' in self._testMethodName else 3
        self.repo.add_record([CollectedItem(str(i+1),work_id=str(i+1),
            title='超长作品标题用于验证两行显示和滚动位置'+str(i)+'内容'*50,author_name='测试作者')
            for i in range(count)],platform='douyin',homepage_url='https://www.douyin.com/user/fixture',
            source_method='anonymous_latest',completion='complete')
        self.login=patch('src.ui.collection_page.is_logged_in',return_value=False)
        self.login.start()
        self.page=ApprovedCollection(AppConfig(auto_update_enabled=False),history_repository=self.repo)
        self.page.resize(1463,900);self.page.show();self.view=self.page.approved
        self.until(lambda:self.js("typeof views!=='undefined' && views.length") == count)

    def tearDown(self):
        self.page.shutdown_workers();self.page.close();self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.app.processEvents()
        self.login.stop();self.temp.cleanup()

    def test_virtual_table_row_geometry_and_end_key_keep_last_work_reachable(self):
        for width,zoom in ((1463,1),(900,1),(1463,1.3),(900,1.3)):
            self.view.setZoomFactor(zoom)
            self.page.resize(width,900);self.app.processEvents()
            self.js('tableScroller.scrollTop=0;renderedViews=null;renderViewport()')
            actual=json.loads(self.js("JSON.stringify([...rowNodes.values()].map(row=>row.getBoundingClientRect().height))"))
            expected=self.js('rowHeight')
            self.assertTrue(actual)
            self.assertLess(max(abs(height-expected) for height in actual),1,(width,actual,expected))
            self.js("(()=>{const input=workBody.querySelector('tr[data-work] input');input.focus();input.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}))})()")
            self.assertEqual(self.js("document.activeElement.closest('tr').getAttribute('aria-rowindex')"),'1501')
            self.assertEqual(self.js('renderedEnd'),1500)

    def test_legacy_removed_records_and_items_restore_without_overwriting_active_work(self):
        snapshot=self.repo.load();key=snapshot.selected_record_id
        snapshot=self.repo.trash_items(key,[snapshot.selected_record.items[1].stable_key],expected_source_hash=snapshot.source_hash)
        snapshot=self.repo.trash_records([key],expected_source_hash=snapshot.source_hash)
        self.page._apply_history_snapshot(snapshot,restore_selected=True)
        self.assertTrue(self.page._payload()['legacyRemoved'])
        replies=[]
        with patch('src.ui.dialogs.confirm',return_value=True):
            self.page._restore_legacy(replies.append,version=snapshot.source_hash)
        restored=self.repo.load()
        self.assertEqual(restored.selected_record_id,key)
        self.assertEqual({row.work_id for row in restored.selected_record.items},{'1','2','3'})
        self.assertFalse(restored.trashed_records)
        self.assertFalse(restored.selected_record.item_trash)
        self.assertFalse(replies[0]['legacyRemoved'])
