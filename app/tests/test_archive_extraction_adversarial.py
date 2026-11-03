"""Real isolated WebEngine with slow/failing writes; no installed files or network."""
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_trash_actions_ui as archive
from tests.test_extraction_batches_ui import ExtractionBatchUITests
from src.core import library


class ExtractionInteractionTests(ExtractionBatchUITests):
    def test_choice_save_keeps_start_appearance_and_start_waits_for_latest_choice(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        entered, release = threading.Event(), threading.Event()
        dispatch = self.main._workspace_batch_dispatcher
        def delayed(op, **args):
            if op == 'batch_configure':
                entered.set(); release.wait(5)
            return dispatch(op, **args)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.js('window.startNode=document.querySelector(".nx-single-footer .primary");'
                        'window.startLook=[getComputedStyle(startNode).opacity,startNode.offsetWidth,startNode.offsetHeight];')
                self.click('[aria-label="提取视频：作品0"]'); self.until(entered.is_set)
                self.assertFalse(self.js('startNode.disabled'), '保存选项不应让开始按钮闪成禁用样式')
                self.assertTrue(self.js('startNode===document.querySelector(".nx-single-footer .primary")'))
                self.assertTrue(self.js('JSON.stringify(startLook)===JSON.stringify([getComputedStyle(startNode).opacity,startNode.offsetWidth,startNode.offsetHeight])'))
                self.click('[aria-label="提取封面：作品0"]')
                self.click('.nx-single-footer .primary')
                self.click('.nx-single-footer .primary')
                self.assertEqual(self.run_calls, [])
                release.set()
                self.until(lambda: len(self.run_calls) == 1)
                self.assertEqual(self.run_calls[0][1], ['text', 'video', 'cover'])
        finally:
            release.set(); self.settle()

    def test_failed_choice_save_does_not_start_with_previous_settings(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        entered, release = threading.Event(), threading.Event()
        dispatch = self.main._workspace_batch_dispatcher
        def delayed(op, **args):
            if op == 'batch_configure':
                entered.set(); release.wait(5); raise RuntimeError('模拟选项保存失败')
            return dispatch(op, **args)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.click('[aria-label="提取视频：作品0"]'); self.until(entered.is_set)
                self.assertFalse(self.js('document.querySelector(".nx-single-footer .primary").disabled'))
                self.click('.nx-single-footer .primary'); release.set()
                self.until(lambda: not self.js('nativeChoiceBusy(workspaceBatches[0].batch_id,0)'))
                self.until(lambda: not self.js('nativePending.size'))
                self.assertEqual(self.run_calls, [])
                self.assertEqual(self.widget.batches.views[self.bid]['items'][0]['selection']['outputs'], ['text'])
                self.assertFalse(self.js('document.querySelector(".nx-single-footer .primary").disabled'))
        finally:
            release.set(); self.settle()

    def test_remove_during_choice_save_finishes_once_without_restoring_card(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        entered, release = threading.Event(), threading.Event()
        dispatch = self.main._workspace_batch_dispatcher
        def delayed(op, **args):
            if op == 'batch_configure': entered.set(); release.wait(5)
            return dispatch(op, **args)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.click('[aria-label="提取视频：作品0"]'); self.until(entered.is_set)
                self.click('.nx-remove'); release.set()
                self.until(lambda: not self.js('nativePending.size'))
                self.assertEqual(self.widget.batches.views[self.bid]['visible_total'], 0)
                self.assertEqual(self.runtime.expansion_batches().show(self.bid, include_hidden=True)['total'], 1)
                self.assertEqual(self.run_calls, [])
        finally:
            release.set(); self.settle()

    def test_clear_captures_current_category_including_unloaded_members(self):
        self.stage(1, group=False); single = self.bid
        future = self.widget.batches.stage([dict(source=f'https://example.com/group-{i}.mp4', title=f'批量{i}', outputs=['text'])
                                           for i in range(80)], name='批量', group=True)
        self.until(future.done); future.result(); self.settle()
        batch = next(bid for bid in self.widget.batches.views if bid != single)
        self.assertLess(len(self.widget.batches.views[batch]['items']), 80)
        self.js('setTaskFilter("single")')
        with patch('src.ui.dialogs.confirm', return_value=True):
            self.click('.clear-tasks')
            self.js('setTaskFilter("batch")')
            self.until(lambda: self.widget.batches.views[single]['visible_total'] == 0)
            self.until(lambda: not self.js('clearingTasks'))
        self.assertEqual(self.widget.batches.views[batch]['visible_total'], 80)
        self.assertEqual(self.run_calls, [])
        with patch('src.ui.dialogs.confirm', return_value=True):
            self.click('.clear-tasks')
            self.until(lambda: self.widget.batches.views[batch]['visible_total'] == 0)
        self.assertEqual(self.runtime.expansion_batches().show(batch, include_hidden=True)['total'], 80)

    def test_clear_legacy_groups_and_invalid_scope_preserve_other_category(self):
        from types import SimpleNamespace
        self.widget.timer.stop()
        one = self.main._add_task('/single.wav', 'file', '独立')
        two = self.main._add_task('/group.wav', 'file', '成组')
        self.main.task_groups = [SimpleNamespace(id='group', name='分组', expanded=True,
            members=[SimpleNamespace(task_id=two.id, is_skipped=False)])]
        for scope in ({}, {'scope':'all'}, {'scope':[]}, {'scope':True}):
            with self.assertRaises(ValueError): self.widget.dispatch('clear_tasks', scope)
        self.assertEqual(self.widget._deleting, {})
        with patch('src.ui.dialogs.confirm', return_value=True):
            future = self.widget.dispatch('clear_tasks', {'scope':'single'})
            self.until(future.done); future.result()
        self.assertIn(one.id, self.widget._deleting)
        self.assertNotIn(two.id, self.widget._deleting)

    def test_clear_category_failure_keeps_affected_and_other_tasks_visible(self):
        self.stage(1, group=False); single = self.bid
        future = self.widget.batches.stage([dict(source='https://example.com/group.mp4', title='批量', outputs=['text'])], group=True)
        self.until(future.done); self.settle()
        batch = next(bid for bid in self.widget.batches.views if bid != single)
        dispatch = self.main._workspace_batch_dispatcher
        def fail(op, **args):
            if op == 'batch_control': raise OSError('模拟保存失败')
            return dispatch(op, **args)
        with patch.object(self.main, '_workspace_batch_dispatcher', fail):
            completion = self.widget.batches.clear_visible('single')
            self.until(completion.done)
            with self.assertRaises(RuntimeError): completion.result()
        self.assertEqual(self.widget.batches.views[single]['visible_total'], 1)
        self.assertEqual(self.widget.batches.views[batch]['visible_total'], 1)


class ArchiveInteractionTests(unittest.TestCase):
    setUpClass = archive.TrashActionsUiTests.__dict__['setUpClass']
    setUp = archive.TrashActionsUiTests.setUp
    tearDown = archive.TrashActionsUiTests.tearDown
    until = archive.TrashActionsUiTests.until
    tick = archive.TrashActionsUiTests.tick
    js = archive.TrashActionsUiTests.js
    trash_rows = archive.TrashActionsUiTests.trash_rows

    def archive_menu(self, entry_id=None):
        self.until(lambda: self.js('!window.lulu.state.loading && Boolean(document.querySelector("[data-entry]")?.oncontextmenu)'))
        selector = '[data-entry="' + entry_id + '"]' if entry_id else '[data-entry]'
        self.js('document.querySelector(' + json.dumps(selector) + ').dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:80,clientY:250}))')
        self.until(lambda: self.js('Boolean(document.querySelector(".lulu-menu"))'))
        self.js('window.archiveButton=[...document.querySelectorAll(".lulu-menu button")].find(b=>b.textContent.trim()==="归档")')

    def test_archive_is_direct_and_repeated_clicks_do_not_repeat_write(self):
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
        entered, release = threading.Event(), threading.Event()
        dispatch, calls = self.widget.dispatch, []
        def delayed(op, **args):
            if op == 'delete':
                calls.append(args['document_id']); entered.set(); release.wait(5)
            return dispatch(op, **args)
        try:
            self.widget.dispatch = delayed
            self.archive_menu()
            self.js('archiveButton.click();archiveButton.click()')
            self.assertFalse(self.js('document.getElementById("dialog").open'))
            self.until(entered.is_set)
            release.set()
            self.until(lambda: len(library.list_deleted()) == 1)
            self.until(lambda: self.js('window.lulu.state.total') == 0)
            self.assertEqual(calls, [self.eid])
            self.assertIsNone(library.document_snapshot(self.eid))
        finally:
            release.set()

    def test_archive_partial_failure_retains_failed_selection_and_can_retry(self):
        other = library.save_document('保留正文', '不能归档')
        self.widget.refresh()
        self.until(lambda: self.js('window.lulu.state.total') == 2)
        self.widget.open_entry(self.eid)
        self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid)
        dispatch = self.widget.dispatch
        def failing(op, **args):
            if op == 'delete' and args['document_id'] == other:
                raise OSError('模拟不可写')
            return dispatch(op, **args)
        self.widget.dispatch = failing
        self.js('document.getElementById("select-mode").click()')
        self.until(lambda: self.js('window.lulu.state.batch'))
        self.js('document.getElementById("select-all").click()')
        self.until(lambda: self.js('window.lulu.state.selected.size') == 2)
        self.js('document.getElementById("batch-more").click()')
        self.until(lambda: self.js('Boolean(document.querySelector(".lulu-menu"))'))
        self.js('[...document.querySelectorAll(".lulu-menu button")].find(b=>b.textContent.trim()==="归档").click()')
        self.until(lambda: len(library.list_deleted()) == 1)
        self.until(lambda: self.js('window.lulu.state.total') == 1)
        self.assertEqual(library.document_snapshot(other)['text'], '保留正文')
        self.assertEqual(json.loads(self.js('JSON.stringify([...window.lulu.state.selected])')), [other])
        self.assertIn('1 项保留', self.js('document.getElementById("dialog-body").textContent'))
        self.widget.dispatch = dispatch
        self.js('document.getElementById("dialog").close()')
        self.archive_menu(); self.js('archiveButton.click()')
        self.until(lambda: len(library.list_deleted()) == 2)

    def test_archive_completion_preserves_new_document_open_already_in_progress(self):
        from PySide6.QtCore import QTimer
        other = library.save_document('另一篇正文', '继续阅读的文稿')
        self.widget.refresh(); self.widget.open_entry(self.eid)
        self.until(lambda: self.js('window.lulu.state.doc?.id') == self.eid and not self.js('window.lulu.state.loading') and self.js('window.lulu.state.total') == 2)
        archiving, opening = threading.Event(), threading.Event()
        release_archive, release_open = threading.Event(), threading.Event()
        dispatch = self.widget.dispatch
        def delayed(op, **args):
            if op == 'delete': archiving.set(); release_archive.wait(5)
            if op == 'open' and args.get('document_id') == other:
                opening.set(); release_open.wait(5)
            return dispatch(op, **args)
        try:
            self.widget.dispatch = delayed
            self.archive_menu(self.eid); self.js('archiveButton.click()'); self.until(archiving.is_set)
            self.js('window.openDone=false;window.lulu.open(' + json.dumps(other) + ').finally(()=>openDone=true)'); self.until(opening.is_set)
            release_archive.set()
            QTimer.singleShot(150, release_open.set)
            self.until(lambda: self.js('openDone && document.getElementById("toast").textContent.includes("已归档，可随时恢复") && window.lulu.state.total===1'))
            self.assertEqual(self.js('window.lulu.state.doc?.id'), other)
            self.assertEqual(library.document_snapshot(other)['text'], '另一篇正文')
        finally:
            release_archive.set(); release_open.set()

    def test_filtered_keyboard_navigation_never_opens_hidden_items(self):
        self.trash_rows(4)
        self.js('document.getElementById("trash-search").value="文稿1";'
                'document.getElementById("trash-search").dispatchEvent(new Event("input"))')
        self.until(lambda: self.js('document.getElementById("trash-title").textContent') == '文稿1')
        self.js('window.visibleItem=document.querySelector("[data-trash]:not([hidden])");visibleItem.focus();'
                'visibleItem.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowUp",bubbles:true}))')
        self.assertEqual(self.js('document.getElementById("trash-title").textContent'), '文稿1')
        self.assertTrue(self.js('document.activeElement===visibleItem'))

    def test_repeated_checkbox_clicks_keep_preview_and_do_not_reread_body(self):
        self.trash_rows(40)
        self.until(lambda: self.js('document.getElementById("trash-preview-empty").hidden'))
        dispatch, reads = self.widget.dispatch, []
        def counted(op, **args):
            if op == 'trash' and args.get('token'): reads.append(args['token'])
            return dispatch(op, **args)
        self.widget.dispatch = counted
        self.js('window.previewBody=document.getElementById("trash-text");'
                'window.choicePaint=[];window.choiceLatencies=[];'
                'window.check=document.querySelector("[data-previewed=true] .trash-check");'
                'for(let n=0;n<40;n++){const start=performance.now();check.dispatchEvent(new MouseEvent("click",{bubbles:true}));choiceLatencies.push(performance.now()-start)}'
                'window.paintStart=performance.now();requestAnimationFrame(()=>choicePaint.push(performance.now()-paintStart))')
        self.until(lambda: self.js('choicePaint.length') == 1)
        self.assertEqual(reads, [], '勾选相同资料不应重读正文或重播预览动画')
        self.assertTrue(self.js('previewBody===document.getElementById("trash-text") && !previewBody.hidden'))
        self.assertLess(self.js('Math.max(...choiceLatencies)'), 100)
        self.assertLess(self.js('choicePaint[0]'), 200)
        print('ARCHIVE_CLICK_MEASUREMENT', self.js('JSON.stringify({clicks:choiceLatencies,paint:choicePaint[0],rows:40,bodyReloads:0})'))

    def test_batch_restore_partial_failure_keeps_only_failed_items_selected(self):
        self.trash_rows(3)
        tokens = json.loads(self.js('JSON.stringify([...document.querySelectorAll("[data-trash]")].map(b=>b.dataset.trash))'))
        dispatch = self.widget.dispatch
        def failing(op, **args):
            if op == 'restore_deleted' and args['token'] == tokens[1]: raise OSError('模拟原位置冲突')
            return dispatch(op, **args)
        self.widget.dispatch = failing
        self.js('document.getElementById("trash-select-visible").click();document.getElementById("trash-bulk-restore").click()')
        self.until(lambda: len(library.list_deleted()) == 1)
        self.until(lambda: self.js('document.querySelectorAll("[data-trash]").length') == 1)
        self.assertEqual(self.js('document.querySelector("[data-trash][data-checked=true]").dataset.trash'), tokens[1])
        self.assertIn('1 项仍在归档中', self.js('document.getElementById("trash-status").textContent'))
        self.widget.dispatch = dispatch
        self.js('document.getElementById("trash-bulk-restore").click()')
        self.until(lambda: len(library.list_deleted()) == 0)

    def test_navigation_during_exit_animation_is_not_taken_back_to_archive(self):
        self.trash_rows(2)
        self.until(lambda: self.js('document.getElementById("trash-title").textContent.startsWith("文稿")'))
        self.js('window.exitObserver=new MutationObserver(()=>{'
                'if(document.querySelector(".trash-removing")){exitObserver.disconnect();'
                'window.lulu.setKind("voice")}});'
                'exitObserver.observe(document.getElementById("trash-list"),{subtree:true,attributes:true,attributeFilter:["class"]});'
                'document.getElementById("trash-restore").click()')
        self.until(lambda: len(library.list_deleted()) == 1)
        self.until(lambda: self.js('window.lulu.state.kind') == 'voice')
        self.js('window.exitDone=false;setTimeout(()=>exitDone=true,350)')
        self.until(lambda: self.js('exitDone'))
        self.assertEqual(self.js('window.lulu.state.page'), 'library')
        self.assertFalse(self.js('document.getElementById("workspace").hidden'))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (ExtractionInteractionTests, ArchiveInteractionTests)
                              for name in cls.__dict__ if name.startswith('test_'))
