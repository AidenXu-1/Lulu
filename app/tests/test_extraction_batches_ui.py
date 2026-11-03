"""Actual Qt page against an isolated Core service; no real App or network."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_extraction_workspace as gui_fixtures
from tests import test_cli_expansion as cli_fixtures
from src.core import library
from src.core.models import Transcript
from src.core.agent_workspace.service import WorkspaceService


class ExtractionBatchUITests(gui_fixtures.ExtractionWorkspaceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root=self.root.resolve()
        self.main.cfg.media_dir=str(self.root)
        self.main.cfg.recording_dir=str(self.root)
        self.patches=[]
        for name,value in [('LIBRARY_DIR',self.root/'library'),('FOLDERS_FILE',self.root/'library'/'folders.json')]:
            p=patch.object(library,name,value);p.start();self.patches.append(p)
        p=patch('src.core.models_manager.app_data_dir',return_value=self.root/'appdata');p.start();self.patches.append(p)
        self.run_calls=[]
        def runner(item,batch,*args):
            chosen=item['requested_outputs'];self.run_calls.append((item['index'],chosen))
            assets={}
            for key in ('video','cover'):
                if key in chosen:
                    file=self.root/(str(item['index'])+'-'+key+'.bin');file.write_bytes(key.encode());assets[key]=str(file)
            return Transcript('完整的测试文稿' if 'text' in chosen else '',[],engine=batch['engine'],
                              meta={'extraction_assets':assets})
        self.runtime=cli_fixtures.ExpansionTests.runtime(self,runner)
        self.core=WorkspaceService(runtime=self.runtime)
        self.main._workspace_batch_dispatcher=lambda op,**args:getattr(self.core,op)(**args)
        self.main._view_entry=lambda eid:self.main.calls.append(('view',eid))
        self.main._reveal=lambda path:self.main.calls.append(('reveal',path))
        self.notices=[]
        self.widget.bridge.notice.connect(self.notices.append)
        self.widget.batches.refresh(force=True)

    def tearDown(self):
        self.widget.batches.closing=True
        self.until(lambda:self.widget.batches.worker is None)
        for worker in list(self.runtime.expansion_batches().workers.values()):worker.join(5)
        for p in reversed(self.patches):p.stop()
        super().tearDown()

    def stage(self,count=12,group=True):
        self.js(f"filter='{'batch' if group else 'single'}'")
        self.widget.batches.stage([dict(source=f'https://example.com/{n}.mp4',title=f'作品{n}',
            outputs=['text'],prefer_subtitles=False) for n in range(count)],name='主页采集测试',group=group)
        self.until(lambda:len(self.widget.batches.views)==1 or self.notices)
        self.assertFalse(self.notices, self.notices)
        self.bid=next(iter(self.widget.batches.views))
        try:
            self.until(lambda:self.js('Boolean(document.querySelector("[data-native-batch]"))'),seconds=3)
        except AssertionError:
            diagnostic=self.js('JSON.stringify({batches:workspaceBatches.length,cards:document.querySelectorAll("[data-native-batch]").length,render:(()=>{try{return nativeBatchCard(workspaceBatches[0]).slice(0,100)}catch(e){return e.stack}})()})')
            self.fail(str(diagnostic))
        self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)

    def settle(self):
        self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)
        self.widget.batches.refresh(force=True)
        self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.reads)

    def click(self, selector):
        self.js(f'document.querySelector({json.dumps(selector)}).click()')

    def test_homepage_button_stages_single_and_group_with_the_saved_work_identity(self):
        from types import MethodType
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        from src.ui.collection_page import CollectionPage
        from src.ui.main_window import MainWindow
        items = [CollectedItem(str(12345+n), title=f'主页作品{n}', author_name='作者') for n in range(3)]
        image = CollectedItem('12348', title='图文应跳过', content_kind='image')
        repository = CollectionRepository()
        repository.upsert_collected(items + [image])
        self.main.extraction_workspace = self.widget
        self.main._switch_page = lambda index:self.main.calls.append(('page', index))
        self.main._toast = self.notices.append
        self.main._collect_transcribe_group = MethodType(MainWindow._collect_transcribe_group, self.main)
        callback = MethodType(MainWindow._collect_transcribe, self.main)
        with patch('src.ui.collection_page.is_logged_in', return_value=False):
            page = CollectionPage(self.main.cfg, on_transcribe=callback, records_repository=repository)
        try:
            for chosen in [items[:1], items[1:]+[image]]:
                page._items = chosen
                page._selected_keys = {i.stable_key for i in chosen}
                page.transcribe_btn.setEnabled(True)
                QTest.mouseClick(page.transcribe_btn, Qt.LeftButton)
                self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)
                self.assertFalse(self.notices, self.notices)
            views = list(self.widget.batches.views.values())
            self.assertEqual(sorted(v['total'] for v in views), [1, 2])
            self.assertEqual({row['source'] for v in views for row in v['items']}, {i.work_url for i in items})
            self.assertTrue(all(v['workspace']['group'] for v in views))
            self.assertEqual(len(next(v for v in views if v['total']==2)['workspace']['skipped']), 1)
            self.assertEqual(self.run_calls, [])
            self.assertIn(('page', 0), self.main.calls)
            self.until(lambda:self.js('document.querySelectorAll("[data-native-batch]").length')==2)
        finally:
            page.shutdown_workers()
            page.deleteLater()
            self.app.processEvents()

    def test_rejected_homepage_identity_clears_preview_and_valid_retry_is_usable(self):
        from src.core.collect.models import CollectedItem
        from src.core.collect.repository import CollectionRepository
        item = CollectedItem('12345', title='重新选择的作品')
        CollectionRepository().upsert_collected([item])
        self.widget.batches.stage([dict(source='https://www.douyin.com/video/99999',
            stable_key=item.stable_key)], group=True)
        self.until(lambda:bool(self.notices))
        self.settle()
        self.until(lambda:self.js('pendingIntakes.size')==0)
        self.until(lambda:not self.js('Boolean(document.querySelector("[data-native-batch]"))'))
        self.assertEqual(self.runtime.expansion_batches().list(), [])
        self.notices.clear()
        self.widget.batches.stage([dict(source=item.work_url, title=item.title,
            stable_key=item.stable_key)], group=True)
        self.until(lambda:len(self.widget.batches.views)==1)
        self.settle()
        self.until(lambda:self.js('Boolean(document.querySelector("[data-native-row] .nx-action"))'))
        self.click('[data-native-row="0"] .nx-action')
        self.until(lambda:len(self.run_calls)==1 and not self.runtime.expansion_batches().workers)
        self.settle()
        view=next(iter(self.widget.batches.views.values()))
        self.assertEqual(view['items'][0]['assets']['text']['state'], 'ready')
        self.assertIn('完整的测试文稿', library.load(view['items'][0]['document_id']).text)
        self.assertFalse(self.notices, self.notices)



    def test_five_visible_rows_total_unlimited_and_scroll_survives_refresh(self):
        self.stage()
        geometry=json.loads(self.js('JSON.stringify((()=>{const v=document.querySelector(".nx-member-viewport"),r=v.querySelector(".nx-row");return [v.clientHeight,r.offsetHeight,workspaceBatches[0].total,v.scrollHeight]})())'))
        self.assertEqual(geometry[0],5*geometry[1])
        self.assertEqual(geometry[2],12)
        self.assertGreater(geometry[3],geometry[0])
        self.js('document.querySelector(".nx-member-viewport").scrollTop=366')
        self.settle()
        self.assertEqual(self.js('document.querySelector(".nx-member-viewport").scrollTop'),366)
        self.assertEqual(self.js('getComputedStyle(document.querySelector("#model-picker")).display'),'none')

    def test_each_work_owns_options_and_supplement_preserves_text(self):
        self.stage(2)
        self.click('[aria-label="优先字幕：作品0"]')
        self.until(lambda:self.widget.batches.views[self.bid]['items'][0]['selection']['prefer_subtitles'])
        self.settle()
        view=self.widget.batches.views[self.bid]
        self.assertTrue(view['items'][0]['selection']['prefer_subtitles'])
        self.assertFalse(view['items'][1]['selection']['prefer_subtitles'])
        self.click('[data-native-row="0"] .nx-action')
        self.until(lambda:len(self.run_calls)==1);self.settle()
        self.until(lambda:not self.runtime.expansion_batches().workers)
        self.settle()
        first=self.widget.batches.views[self.bid]['items'][0]
        old=first['assets']['text']['document_id'];text=library.load_markdown(old)
        self.click('[aria-label="提取视频：作品0"]')
        self.until(lambda:'video' in self.widget.batches.views[self.bid]['items'][0]['selection']['outputs']);self.settle()
        self.click('[data-native-row="0"] .nx-action')
        self.until(lambda:len(self.run_calls)==2);self.settle()
        self.until(lambda:not self.runtime.expansion_batches().workers);self.settle()
        first=self.widget.batches.views[self.bid]['items'][0]
        self.assertEqual(self.run_calls,[(0,['text']),(0,['video'])])
        self.assertEqual(first['assets']['text']['document_id'],old)
        self.assertEqual(library.load_markdown(old),text)
        self.assertTrue(Path(first['assets']['video']['path']).is_file())
        self.assertEqual(self.widget.batches.views[self.bid]['items'][1]['selection']['outputs'],['text'])

    def test_virtual_pages_are_not_task_limits(self):
        self.stage(140)
        row_height=self.js('document.querySelector(".nx-row").offsetHeight')
        self.js(f'document.querySelector(".nx-member-viewport").scrollTop={row_height}*118')
        self.until(lambda:self.widget.batches.pages.get(self.bid)==75)
        self.until(lambda:self.js('workspaceBatches[0].offset')==75)
        self.assertEqual(self.js('workspaceBatches[0].total'),140)
        self.assertEqual(self.js('document.querySelector(".nx-member-viewport").scrollTop'),row_height*118)
        self.assertLessEqual(self.js('document.querySelectorAll(".nx-row").length'),50)
        self.assertEqual(self.js('document.querySelector(".nx-member-viewport").clientHeight'),row_height*5)

    def test_single_cards_select_their_own_model_without_a_page_default(self):
        for name in ('A','B'):
            self.widget.batches.stage([dict(source=f'https://example.com/{name}.mp4',title=name)],name=name)
        self.until(lambda:len(self.widget.batches.views)==2)
        self.until(lambda:self.js('document.querySelectorAll(".nx-single").length')==2)
        self.settle()
        bid=next(k for k,v in self.widget.batches.views.items() if v['workspace']['name']=='A')
        other=next(k for k in self.widget.batches.views if k!=bid)
        trigger=f'document.querySelector("[data-native-batch=\\"{bid}\\"] .nx-model")'
        original_width=self.js(f'{trigger}.offsetWidth')
        self.js(f'nativeModels({json.dumps(bid)})')
        self.until(lambda:self.js('Boolean(document.querySelector("#nx-model-popover"))'))
        self.assertTrue(self.js('document.querySelector("#modalback").hidden'))
        self.assertEqual(self.js('document.querySelector(".nx-model-note").textContent'),'逐条识别，按顺序排队；音频不上传')
        self.assertLessEqual(self.js('document.querySelector(".nx-model-note").scrollWidth'),self.js('document.querySelector(".nx-model-note").clientWidth'))
        self.assertEqual(self.js(f'{trigger}.offsetWidth'),original_width)
        local_pop=self.js('document.querySelector("#nx-model-popover").offsetHeight')
        self.js(f'nativeModels({json.dumps(bid)},"cloud")')
        self.assertEqual(self.js('document.querySelector(".nx-model-note").textContent'),'并行识别，无需本地排队；音频上传并计费')
        self.assertLessEqual(self.js('document.querySelector(".nx-model-note").scrollWidth'),self.js('document.querySelector(".nx-model-note").clientWidth'))
        self.assertEqual(self.js('document.querySelector("#nx-model-popover").offsetHeight'),local_pop)
        self.js(f'nativeModels({json.dumps(bid)},"local")')
        self.js('[...document.querySelectorAll(".nx-model-options button")].find(b=>b.getAttribute("onclick").includes("local_whisper_mlx_4bit")).click()')
        self.until(lambda:self.widget.batches.views[bid]['engine']=='local_whisper_mlx_4bit')
        self.assertFalse(self.js('Boolean(document.querySelector("#nx-model-popover"))'))
        self.assertEqual(self.js(f'{trigger}.offsetWidth'),original_width)
        self.js(f'nativeModels({json.dumps(bid)})')
        self.until(lambda:self.js('Boolean(document.querySelector("#nx-model-popover"))'))
        self.js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
        self.assertFalse(self.js('Boolean(document.querySelector("#nx-model-popover"))'))
        self.assertEqual(self.widget.batches.views[other]['engine'],'local_sensevoice_q8')
        self.assertEqual(self.main.cfg.engine_name,'local_sensevoice_q8')
        for width in (1463,900,720,540):
            self.widget.resize(width,900);self.tick()
            geometry=json.loads(self.js('JSON.stringify((()=>{const c=document.querySelector(".nx-single"),s=c.querySelector(".nx-single-setting").getBoundingClientRect(),f=c.querySelector(".nx-single-footer").getBoundingClientRect(),b=c.querySelector(".nx-single-footer .primary").getBoundingClientRect(),r=c.getBoundingClientRect();return {cardBottom:r.bottom,settingBottom:s.bottom,footerTop:f.top,footerBottom:f.bottom,buttonBottom:b.bottom,scrollWidth:c.scrollWidth,clientWidth:c.clientWidth}})())'))
            self.assertLessEqual(geometry['settingBottom'],geometry['footerTop'])
            self.assertLessEqual(geometry['footerBottom'],geometry['cardBottom']-10)
            self.assertLessEqual(geometry['buttonBottom'],geometry['cardBottom']-10)
            self.assertLessEqual(geometry['scrollWidth'],geometry['clientWidth']+1)











    def test_group_remove_and_undo_restore_all_rows_without_running(self):
        self.stage(63)
        with patch('src.ui.dialogs.confirm',return_value=True):
            self.widget.batches.control(self.bid,'dismiss')
        self.until(lambda:self.widget.batches.undo_notice() is not None)
        self.assertEqual(self.widget.batches.undo_notice()['count'],63)
        self.until(lambda:self.js('document.querySelectorAll(".nx-batch").length')==0)
        token=self.widget.batches.undo_notice()['token']
        self.widget.batches.restore(token)
        self.until(lambda:self.widget.batches.views[self.bid]['visible_total']==63)
        self.settle()
        self.assertFalse(self.run_calls)
        self.assertEqual(self.js('workspaceBatches[0].total'),63)

    def test_narrow_layout_has_five_rows_and_visible_controls(self):
        self.stage(6)
        for width in (900,720,540):
            self.widget.resize(width,900)
            self.until(lambda:self.js('innerWidth')==self.widget.view.width())
            # Read one rendered frame. Separate JS calls could compare the old
            # scroll width with the next resize's client width under CPU load.
            geometry=json.loads(self.js('JSON.stringify((()=>{const b=document.querySelector(".nx-batch"),v=document.querySelector(".nx-member-viewport"),r=v.querySelector(".nx-row"),o=r.querySelector(".nx-outputs").getBoundingClientRect(),a=r.querySelector(".nx-action").getBoundingClientRect();return [v.clientHeight,r.offsetHeight,v.scrollWidth,v.clientWidth,o.right,a.right,r.getBoundingClientRect().right,b.scrollWidth,b.clientWidth]})())'))
            self.assertEqual(geometry[0],geometry[1]*5)
            self.assertLessEqual(geometry[2],geometry[3]+1)
            self.assertLessEqual(geometry[4],geometry[6]+1)
            self.assertLessEqual(geometry[5],geometry[6]+1)
            self.assertLessEqual(geometry[7],geometry[8]+1)

    def test_large_gui_submission_uses_a_manifest_without_count_truncation(self):
        self.stage(2601)
        self.assertEqual(self.widget.batches.views[self.bid]['total'],2601)
        self.assertEqual(self.js('workspaceBatches[0].total'),2601)
        self.assertFalse(self.run_calls)

    def test_old_cloud_history_remains_reachable_without_becoming_a_new_task(self):
        self.runtime.expansion_batches().prepare('old-history',[dict(source='https://example.com/old.mp4')],'dashscope')
        self.widget.batches.refresh(force=True)
        self.until(lambda:self.widget.batches.legacy_count==1)
        self.stage(2)
        self.assertEqual(len(self.widget.batches.views),1)
        self.assertFalse(self.js('document.querySelector(".boardbar").textContent.includes("旧版批次记录")'))
        self.assertTrue(any(b['batch_id']!=self.bid for b in self.runtime.expansion_batches().list()))
        self.assertFalse(self.run_calls)

    def test_workspace_activation_requires_product_identity_and_ready_core(self):
        from types import SimpleNamespace
        import sys
        dispatcher=self.main._workspace_batch_dispatcher
        del self.main._workspace_batch_dispatcher
        self.main._task_queue_store=SimpleNamespace(async_write=True)
        try:
            self.assertFalse(self.widget.batches.enabled)
            with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable','/fixture/Lulu.app/Contents/MacOS/Lulu'):
                self.main._install_preparation_pending=True
                self.assertFalse(self.widget.batches.enabled)
                self.main._install_preparation_pending=False
                self.assertTrue(self.widget.batches.enabled)
        finally:
            self.main._workspace_batch_dispatcher=dispatcher

    def test_remove_selected_row_updates_selection_and_remaining_work_can_start(self):
        self.stage(3)
        self.widget.batches.select(self.bid,0,True);self.widget.batches.select(self.bid,1,True)
        self.widget.batches.control(self.bid,'dismiss',[0]);self.settle()
        self.assertEqual(self.widget.batches.snapshot()[0]['selectedCount'],1)
        self.assertEqual(self.widget.batches.snapshot()[0]['selection'],[1])
        self.widget.batches.control(self.bid,'start',selected=True)
        self.until(lambda:bool(self.run_calls));self.settle()
        self.assertEqual(self.run_calls,[(1,['text'])])
        self.assertEqual(self.widget.batches.snapshot()[0]['selectedCount'],0)
        self.assertFalse(self.notices)

    def test_long_batch_title_does_not_increase_card_height(self):
        self.stage(6)
        original=self.js('document.querySelector(".nx-batch").offsetHeight')
        self.widget.batches.views[self.bid]['workspace']['name']='很长的任务名称'*70
        self.widget.publish(force=True)
        self.until(lambda:self.js('document.querySelector(".nx-title h3").textContent.length')>400)
        self.assertEqual(self.js('document.querySelector(".nx-batch").offsetHeight'),original)

    def test_manually_paused_work_does_not_block_starting_other_drafts(self):
        self.stage(2)
        self.widget.batches.control(self.bid,'pause',[1]);self.settle()
        self.assertIn('提取待处理',self.js('document.querySelector(".nx-footer .primary").textContent'))
        self.click('.nx-footer .primary')
        self.until(lambda:bool(self.run_calls));self.settle()
        self.until(lambda:not self.runtime.expansion_batches().workers);self.settle()
        self.assertEqual(self.run_calls,[(0,['text'])])
        self.assertEqual(self.widget.batches.views[self.bid]['items'][1]['state'],'paused')
        self.assertTrue(self.js('document.querySelector(".nx-footer .primary").disabled'))
        self.assertIn('逐条继续',self.js('document.querySelector(".nx-footer .primary").textContent'))
        self.click('[data-native-row="1"] .nx-action')
        self.until(lambda:len(self.run_calls)==2);self.settle()
        self.assertEqual(self.run_calls,[(0,['text']),(1,['text'])])

    def test_batch_pause_keeps_a_working_batch_resume_button(self):
        self.stage(3)
        self.widget.batches.control(self.bid,'pause');self.settle()
        self.assertEqual(self.widget.batches.views[self.bid]['auto_paused_count'],3)
        self.assertIn('继续本批',self.js('document.querySelector(".nx-footer .primary").textContent'))
        self.click('.nx-footer .primary')
        self.until(lambda:len(self.run_calls)==3);self.settle()
        self.assertEqual([index for index,_ in self.run_calls],[0,1,2])


if __name__=='__main__':unittest.main()
