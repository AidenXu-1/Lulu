"""Real WebEngine, delayed isolated Core; never accesses installed user data."""
import threading
import unittest
from unittest.mock import patch
from tests.test_extraction_batches_ui import ExtractionBatchUITests


class ImmediateChoiceTests(ExtractionBatchUITests):
    def test_choice_keeps_latest_value_during_delayed_save_and_refresh(self):
        self.stage(2); self.widget.timer.stop(); self.settle()
        entered, release = threading.Event(), threading.Event()
        dispatch = self.main._workspace_batch_dispatcher
        def delayed(op, **args):
            if op == 'batch_configure':
                entered.set(); release.wait(4)
            return dispatch(op, **args)
        selector = '[aria-label="提取视频：作品0"]'
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.js('window.savedChoiceNode=document.querySelector(\'[aria-label="提取视频：作品0"]\')')
                self.click(selector); self.until(entered.is_set)
                self.assertTrue(self.js('savedChoiceNode.checked'), '点击后被旧快照画回')
                self.assertFalse(self.js('savedChoiceNode.disabled'), '简单勾选不应锁住整行')
                self.js('renderBoard()')
                self.assertTrue(self.js('savedChoiceNode===document.querySelector(\'[aria-label="提取视频：作品0"]\') && savedChoiceNode.checked'))
                self.click(selector)
                self.assertFalse(self.js('savedChoiceNode.checked'))
                release.set()
                self.until(lambda: self.widget.batches.worker is None and not self.widget.batches.commands)
                self.until(lambda: not self.js('nativeChoiceBusy(workspaceBatches[0].batch_id,0)'))
                self.assertNotIn('video', self.widget.batches.views[self.bid]['items'][0]['selection']['outputs'])
        finally:
            release.set(); self.until(lambda: self.widget.batches.worker is None and not self.widget.batches.commands)

    def test_busy_controls_do_not_use_wait_cursor(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        for selector in ('.url-form button','.nx-remove'):
            import json
            self.js('window.busyControl=document.querySelector('+json.dumps(selector)+');busyControl.disabled=true;busyControl.setAttribute("aria-busy","true")')
            self.assertNotIn(self.js('getComputedStyle(busyControl).cursor'),['progress','wait'])

    def test_twenty_rapid_choices_coalesce_and_last_choice_is_saved(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        dispatch=self.main._workspace_batch_dispatcher
        entered,release=threading.Event(),threading.Event();calls=[]
        def delayed(op,**args):
            if op=='batch_configure':
                calls.append(args);entered.set();release.wait(4)
            return dispatch(op,**args)
        try:
            with patch.object(self.main,'_workspace_batch_dispatcher',delayed):
                self.js('window.choiceFrames=[];window.choiceStart=performance.now();document.querySelector(\'[aria-label="提取视频：作品0"]\').click();requestAnimationFrame(()=>choiceFrames.push(performance.now()-choiceStart))')
                self.until(entered.is_set)
                self.js('for(let n=0;n<19;n++)document.querySelector(\'[aria-label="提取视频：作品0"]\').click()')
                self.assertFalse(self.js('document.querySelector(\'[aria-label="提取视频：作品0"]\').checked'))
                self.until(lambda:self.js('choiceFrames.length')==1)
                self.assertLess(self.js('choiceFrames[0]'),200)
                release.set()
                self.until(lambda:not self.js('nativeChoiceBusy(workspaceBatches[0].batch_id,0)'))
                self.assertLessEqual(len(calls),2)
                self.assertNotIn('video',self.widget.batches.views[self.bid]['items'][0]['selection']['outputs'])
        finally:
            release.set();self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)

    def test_clear_all_covers_members_outside_loaded_page_and_keeps_results(self):
        self.stage(80);self.widget.timer.stop();self.settle()
        self.assertLess(len(self.widget.batches.views[self.bid]['items']),80)
        result=self.widget.batches.clear_visible()
        self.until(result.done);result.result()
        self.assertEqual(self.widget.batches.views[self.bid]['visible_total'],0)
        self.assertEqual(self.runtime.expansion_batches().show(self.bid,include_hidden=True)['total'],80)
        self.assertEqual(self.run_calls,[])

    def test_clear_all_failure_keeps_unremoved_tasks_visible(self):
        self.stage(2);self.widget.timer.stop();self.settle()
        dispatch=self.main._workspace_batch_dispatcher
        def fail(op,**args):
            if op=='batch_control':raise RuntimeError('临时记录不可写')
            return dispatch(op,**args)
        with patch.object(self.main,'_workspace_batch_dispatcher',fail):
            result=self.widget.batches.clear_visible();self.until(result.done)
            with self.assertRaises(RuntimeError):result.result()
        self.assertEqual(self.widget.batches.views[self.bid]['visible_total'],2)

    def test_dismiss_is_immediate_and_failed_save_restores_card(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        entered,release=threading.Event(),threading.Event();dispatch=self.main._workspace_batch_dispatcher
        def delayed(op,**args):
            if op=='batch_control':entered.set();release.wait(4);raise RuntimeError('记录不可写')
            return dispatch(op,**args)
        try:
            with patch.object(self.main,'_workspace_batch_dispatcher',delayed):
                self.click('.nx-remove');self.until(entered.is_set)
                self.assertEqual(self.js('document.querySelectorAll("[data-native-batch]:not(.task-exit)").length'),0)
                release.set();self.until(lambda:self.widget.batches.worker is None)
                self.until(lambda:self.js('document.querySelectorAll("[data-native-batch]:not(.task-exit)").length')==1)
                self.assertEqual(self.widget.batches.views[self.bid]['visible_total'],1)
        finally:
            release.set();self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)

    def test_new_intent_is_visible_before_slow_save_and_same_card_becomes_actionable(self):
        self.stage(1,group=False);self.widget.timer.stop();self.settle()
        self.js('window.oldIntent=document.querySelector("[data-native-batch]")')
        entered,release=threading.Event(),threading.Event();dispatch=self.main._workspace_batch_dispatcher
        def delayed(op,**args):
            if op=='batch_stage':entered.set();release.wait(4)
            return dispatch(op,**args)
        try:
            with patch.object(self.main,'_workspace_batch_dispatcher',delayed):
                saved=self.widget.batches.stage([dict(source='https://example.com/new.mp4',title='新任务',outputs=['text'])],group=False)
                self.until(entered.is_set)
                self.until(lambda:self.js('document.querySelectorAll("[data-native-batch][inert]").length')==1)
                self.js('window.newIntent=document.querySelector("[data-native-batch][inert]");window.intentSize=[newIntent.offsetWidth,newIntent.offsetHeight]')
                self.assertTrue(self.js('document.querySelector(".task-feed").firstElementChild===newIntent'))
                self.assertTrue(self.js('Boolean(oldIntent._reflowAnimation)'))
                self.assertIn('正在加入',self.js('newIntent.textContent'))
                release.set();self.until(saved.done);saved.result()
                self.until(lambda:self.js('document.querySelectorAll("[data-native-batch]:not([inert])").length')==2)
                self.assertTrue(self.js('newIntent===document.querySelector(".task-feed").firstElementChild'))
                self.assertTrue(self.js('intentSize[0]===newIntent.offsetWidth && intentSize[1]===newIntent.offsetHeight'))
        finally:
            release.set();self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)

    def test_local_intake_keeps_file_identity_and_geometry_before_and_after_save(self):
        self.widget.timer.stop();self.settle()
        source=self.root/'pending-local.wav';source.write_bytes(b'owned temporary input')
        entered,release=threading.Event(),threading.Event();dispatch=self.main._workspace_batch_dispatcher
        def delayed(op,**args):
            if op=='batch_stage':entered.set();release.wait(4)
            return dispatch(op,**args)
        try:
            with patch.object(self.main,'_workspace_batch_dispatcher',delayed):
                saved=self.widget.batches.stage([dict(source=str(source),title=source.name,outputs=['text'])],group=False)
                self.until(entered.is_set)
                self.until(lambda:self.js('document.querySelectorAll("[data-native-batch][inert]").length')==1)
                self.js('window.localIntent=document.querySelector("[data-native-batch]");window.localSize=[localIntent.offsetWidth,localIntent.offsetHeight]')
                self.assertIn('本地文件',self.js('localIntent.querySelector(".nx-single-source").textContent'))
                release.set();self.until(saved.done);saved.result()
                self.until(lambda:self.js('document.querySelectorAll("[data-native-batch]:not([inert])").length')==1)
                self.assertTrue(self.js('localIntent===document.querySelector("[data-native-batch]")'))
                self.assertTrue(self.js('localSize[0]===localIntent.offsetWidth && localSize[1]===localIntent.offsetHeight'))
        finally:
            release.set();self.until(lambda:self.widget.batches.worker is None and not self.widget.batches.commands)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ImmediateChoiceTests(name) for name in ImmediateChoiceTests.__dict__ if name.startswith('test_'))
