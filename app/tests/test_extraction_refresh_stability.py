"""Refresh races in the real webpage; isolated Core, no media or credentials."""
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_extraction_batches_ui as fixtures
from src.ui.extraction.batches import WorkspaceBatches


class WorkspaceReadinessTests(unittest.TestCase):
    def test_background_read_keeps_ready_workspace_but_startup_and_failure_stay_blocked(self):
        main = SimpleNamespace(_task_queue_store=SimpleNamespace(async_write=True),
                               _task_queue_startup_pending=True, _task_queue_initial_connection=False)
        adapter = SimpleNamespace(main=main)
        with patch('src.ui.extraction.batches.sys.frozen', True, create=True), \
                patch('src.ui.extraction.batches.sys.executable', '/fixture/Lulu'):
            self.assertTrue(WorkspaceBatches.enabled.fget(adapter))
            main._task_queue_initial_connection = True
            self.assertFalse(WorkspaceBatches.enabled.fget(adapter))
            main._task_queue_initial_connection = False
            for flag in ('_task_queue_store_failed', '_install_preparation_pending', '_install_preparation_error'):
                setattr(main, flag, True)
                self.assertFalse(WorkspaceBatches.enabled.fget(adapter), flag)
                setattr(main, flag, False)


class ExtractionRefreshStabilityTests(fixtures.ExtractionBatchUITests):
    def test_result_menu_survives_unchanged_refresh_and_closes_when_result_changes(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        self.js('''workspaceBatches[0].items[0].state='recovery_required';
          workspaceBatches[0].items[0].assets.text.state='unknown';renderBoard();
          nativeDetail(workspaceBatches[0].batch_id,0);window.resultMenu=document.querySelector('#nx-action-popover');renderBoard()''')
        self.assertTrue(self.js('resultMenu===document.querySelector("#nx-action-popover")'))
        self.js("workspaceBatches[0].items[0].state='succeeded';workspaceBatches[0].items[0].assets.text.state='ready';renderBoard()")
        self.assertFalse(self.js('Boolean(document.querySelector("#nx-action-popover"))'))

    def test_control_failure_releases_busy_state_and_keeps_task(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        original = self.main._workspace_batch_dispatcher
        def failing(operation, **arguments):
            if operation == 'batch_control':
                raise RuntimeError('隔离测试：任务服务暂未完成')
            return original(operation, **arguments)
        with patch.object(self.main, '_workspace_batch_dispatcher', failing):
            self.click('.nx-single-footer .primary')
            self.until(lambda: bool(self.notices))
            self.until(lambda: self.widget.batches.worker is None)
            self.until(lambda: not self.js('document.querySelector(".nx-single-footer .primary").disabled'))
        self.assertEqual(self.js('document.querySelectorAll(".nx-single").length'), 1)
        self.assertEqual(self.run_calls, [])

    def test_control_stays_busy_until_core_finishes_not_just_bridge_acceptance(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        entered, release = threading.Event(), threading.Event()
        original = self.main._workspace_batch_dispatcher
        def delayed(operation, **arguments):
            if operation == 'batch_control':
                entered.set(); release.wait(4)
            return original(operation, **arguments)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.click('.nx-single-footer .primary')
                self.until(entered.is_set)
                self.until(lambda: self.js('nativePending.size') == 1)
                self.assertTrue(self.js('document.querySelector(".nx-single-footer .primary").disabled'))
                self.assertEqual(self.js('document.querySelector(".nx-single-footer .primary").textContent'), '处理中…')
        finally:
            release.set()
            self.until(lambda: self.widget.batches.worker is None)

    def test_background_read_keeps_layout_intake_and_visible_controls(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        original = self.js('document.querySelector(".nx-single").offsetWidth')
        media = self.root/'isolated.wav'; media.write_bytes(b'never transcribed')
        with patch('src.ui.extraction.batches.sys.frozen', True, create=True), \
                patch('src.ui.extraction.batches.sys.executable', '/fixture/Lulu'), \
                patch.object(self.main, '_workspace_batch_dispatcher', None), \
                patch.object(self.main, '_task_queue_store', SimpleNamespace(async_write=True), create=True), \
                patch.object(self.main, '_task_queue_startup_pending', True, create=True), \
                patch.object(self.main, '_task_queue_initial_connection', False, create=True), \
                patch.object(self.widget.batches, 'refresh'), \
                patch.object(self.widget.batches, 'stage') as stage, \
                patch.object(self.widget.batches, 'select') as select:
            self.widget.publish(force=True); self.tick()
            self.assertEqual(self.js('getComputedStyle(document.querySelector(".pagehead>button")).display'), 'none')
            self.assertEqual(self.js('document.querySelector(".nx-single").offsetWidth'), original)
            self.widget.add_files([str(media)])
            self.assertEqual(stage.call_count, 1); self.assertEqual(self.main.tasks, [])
            self.widget.dispatch('batch_select', dict(batch_id=self.bid, index=0, selected=True))
            select.assert_called_once_with(self.bid, 0, True)

    def test_summary_keeps_complete_old_view_until_detail_arrives(self):
        self.stage(2); self.widget.timer.stop(); self.settle()
        adapter = self.widget.batches
        service = self.runtime.expansion_batches()
        batch = service._read(self.bid)
        for item in batch['items']:
            item.update(state='paused', auto_paused=False, message='用户暂停')
        batch['state'] = 'paused'; service._save(batch)
        with patch.object(adapter, 'page'):
            adapter.refresh(force=True)
            self.until(lambda: adapter.worker is None)
        view = adapter.views[self.bid]
        self.assertEqual(view['counts']['draft'], 2)
        self.assertEqual([i['state'] for i in view['items']], ['draft', 'draft'])
        adapter._accept(service.show(self.bid, limit=50))
        self.widget.publish(force=True)
        self.until(lambda: self.js('workspaceBatches[0].items[0].state') == 'paused')
        self.assertEqual(self.js('document.querySelector(".nx-action").textContent'), '继续')

    def test_newly_discovered_summary_is_not_rendered_as_a_short_empty_card(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        adapter = self.widget.batches
        adapter.views.clear()
        with patch.object(adapter, 'page'):
            adapter.refresh(force=True)
            self.until(lambda: adapter.worker is None)
        self.assertEqual(adapter.snapshot(), [])
        adapter._accept(self.runtime.expansion_batches().show(self.bid, limit=50))
        self.assertEqual(len(adapter.snapshot()[0]['items']), 1)

    def test_member_state_change_keeps_other_rows_and_controls(self):
        self.stage(3); self.widget.timer.stop(); self.settle()
        result = json.loads(self.js('''JSON.stringify((()=>{
          const row=document.querySelector('[data-native-row="2"]'),input=row.querySelector('input');
          workspaceBatches[0].items[0].state='paused';workspaceBatches[0].counts.paused=1;renderBoard();
          return {row:row===document.querySelector('[data-native-row="2"]'),input:input===document.querySelector('[data-native-row="2"] input')};
        })())'''))
        self.assertTrue(result['row']); self.assertTrue(result['input'])

    def test_refresh_preserves_collapse_at_midpoint(self):
        self.stage(3); self.widget.timer.stop(); self.settle()
        self.until(lambda: not self.js('document.querySelector(".nx-batch").classList.contains("task-enter")'))
        self.js('nativeToggle(workspaceBatches[0].batch_id)')
        self.until(lambda: self.js('document.querySelector(".nx-batch-body").getAnimations().some(a=>a.transitionProperty==="max-height")'))
        result = json.loads(self.js('''JSON.stringify((()=>{
          const body=document.querySelector('.nx-batch-body'),a=body.getAnimations().find(a=>a.transitionProperty==='max-height');
          a.pause();a.currentTime=120;const before=body.getBoundingClientRect().height;
          renderBoard();return {same:body===document.querySelector('.nx-batch-body'),before,after:body.getBoundingClientRect().height,alive:body.getAnimations().includes(a)};
        })())'''))
        self.assertTrue(result['same']); self.assertTrue(result['alive'])
        self.assertAlmostEqual(result['before'], result['after'], delta=1)

    def test_filter_roundtrip_has_one_card_and_no_repeated_entrance(self):
        self.widget.timer.stop(); self.stage(1, group=False)
        self.until(lambda: not self.js('document.querySelector(".nx-single").classList.contains("task-enter")'))
        with patch.object(self.widget.batches, 'refresh'):
            self.widget.publish(force=True); self.tick()
            self.js("setTaskFilter('batch');setTaskFilter('single')")
        self.assertEqual(self.js('document.querySelectorAll(".nx-single").length'), 1)
        self.assertEqual(self.js('document.querySelectorAll(".task-enter,.task-exit").length'), 0)

    def test_open_model_menu_tracks_trigger_and_closes_when_locked(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        self.js('nativeModels(workspaceBatches[0].batch_id);workspaceBatches[0].items[0].title="更新标题";renderBoard()')
        self.assertEqual(self.js('document.querySelector(".nx-model").getAttribute("aria-expanded")'), 'true')
        self.js('workspaceBatches[0].model_locked=true;renderBoard()')
        self.assertFalse(self.js('Boolean(document.querySelector("#nx-model-popover"))'))
        self.assertTrue(self.js('document.querySelector(".nx-model").disabled'))


def load_tests(loader, tests, pattern):
    # Reuse the safe fixture without repeating all inherited baseline tests.
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(WorkspaceReadinessTests))
    suite.addTests(ExtractionRefreshStabilityTests(name) for name in ExtractionRefreshStabilityTests.__dict__
                   if name.startswith('test_'))
    return suite
