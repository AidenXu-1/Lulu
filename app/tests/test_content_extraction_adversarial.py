"""Faults and competing actions in the real page, with isolated files/Core only."""
import json
import threading
import unittest
from unittest.mock import patch

from tests.test_extraction_batches_ui import ExtractionBatchUITests
from src.ui.main_window import MainWindow
from tests.test_extraction_main_window import ExtractionMainWindowTests
from tests.test_cli_expansion import ExpansionTests
from src.core.agent_workspace.service import WorkspaceService
from src.core import library


class ExtractionAdversarialTests(ExtractionBatchUITests):
    def link_entry(self):
        self.widget.timer.stop()
        self.settle()
        self.main.extraction_workspace = self.widget
        self.main._grab_link = lambda text: MainWindow._grab_link(self.main, text)
        self.js("stageLink('https://example.com/submitted.mp4')")

    def test_failed_submission_preserves_link_and_has_no_task(self):
        self.link_entry()
        original = self.main._workspace_batch_dispatcher
        def fail(operation, **arguments):
            if operation == 'batch_stage':
                raise OSError('隔离测试：磁盘暂不可写')
            return original(operation, **arguments)
        with patch.object(self.main, '_workspace_batch_dispatcher', fail):
            self.js('grab({preventDefault(){}})')
            self.until(lambda: bool(self.notices))
            self.until(lambda: self.widget.batches.worker is None)
            self.tick()
        self.assertEqual(self.js('document.querySelector("#url").value'), 'https://example.com/submitted.mp4')
        self.assertEqual(self.widget.batches.views, {})

    def test_slow_submission_preserves_new_draft_and_prevents_duplicate_submit(self):
        self.link_entry()
        entered, release = threading.Event(), threading.Event()
        original = self.main._workspace_batch_dispatcher
        stages = []
        def delayed(operation, **arguments):
            if operation == 'batch_stage':
                stages.append(arguments); entered.set(); release.wait(4)
            return original(operation, **arguments)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.js("grab({preventDefault(){}});grab({preventDefault(){}});stageLink('https://example.com/new-draft.mp4')")
                self.until(entered.is_set)
                self.assertEqual(self.js('document.querySelector("#url").value'), 'https://example.com/new-draft.mp4')
                release.set()
                self.until(lambda: self.widget.batches.worker is None and not self.widget.batches.commands)
                self.tick()
                self.assertEqual(len(stages), 1)
                self.assertEqual(self.js('document.querySelector("#url").value'), 'https://example.com/new-draft.mp4')
        finally:
            release.set()
            self.until(lambda: self.widget.batches.worker is None and not self.widget.batches.commands)

    def test_failed_choice_restores_authoritative_checkbox(self):
        self.stage(2); self.widget.timer.stop(); self.settle()
        original = self.main._workspace_batch_dispatcher
        def fail(operation, **arguments):
            if operation == 'batch_configure':
                raise RuntimeError('隔离测试：设置保存失败')
            return original(operation, **arguments)
        with patch.object(self.main, '_workspace_batch_dispatcher', fail):
            self.click('[aria-label="提取视频：作品0"]')
            self.until(lambda: bool(self.notices))
            self.until(lambda: self.widget.batches.worker is None)
            self.tick()
        self.assertNotIn('video', self.widget.batches.views[self.bid]['items'][0]['selection']['outputs'])
        self.assertFalse(self.js('document.querySelector(\'[aria-label="提取视频：作品0"]\').checked'))

    def test_choice_pending_blocks_start_until_saved_and_other_rows_stay_usable(self):
        self.stage(2); self.widget.timer.stop(); self.settle()
        original = self.main._workspace_batch_dispatcher
        entered, release = threading.Event(), threading.Event()
        controls = []
        def delayed(operation, **arguments):
            if operation == 'batch_configure':
                entered.set(); release.wait(4)
            if operation == 'batch_control': controls.append(arguments)
            return original(operation, **arguments)
        try:
            with patch.object(self.main, '_workspace_batch_dispatcher', delayed):
                self.click('[aria-label="提取视频：作品0"]'); self.until(entered.is_set)
                self.assertFalse(self.js('document.querySelector(\'[data-native-row="0"] .nx-action\').disabled'))
                self.assertFalse(self.js('document.querySelector(\'[data-native-row="1"] .nx-action\').disabled'))
                self.click('[data-native-row="0"] .nx-action')
                self.assertEqual(controls, [], '开始操作必须等待最新选项保存')
                self.assertEqual(self.run_calls, [])
                release.set()
                self.until(lambda: len(self.run_calls) == 1)
                self.assertEqual(self.run_calls, [(0, ['text', 'video'])])
                self.assertEqual(len(controls), 1)
                self.assertFalse(self.js('document.querySelector(\'[data-native-row="1"] .nx-action\').disabled'))
        finally:
            release.set(); self.until(lambda: self.widget.batches.worker is None)

    def test_window_blur_releases_lost_pointer_and_applies_deferred_view(self):
        self.stage(1, group=False); self.widget.timer.stop(); self.settle()
        self.js('''document.querySelector('.nx-single').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:9}));
            workspaceBatches[0].items[0].title='后台更新的标题';renderBoard();
            window.dispatchEvent(new Event('blur'));''')
        self.tick()
        self.assertIn('后台更新的标题', self.js('document.querySelector(".nx-single").textContent'))

    def test_external_removal_cannot_leave_wrong_selection_count(self):
        self.stage(3); self.widget.timer.stop(); self.settle()
        adapter = self.widget.batches
        adapter.select(self.bid, None, True); adapter.select(self.bid, 0, False)
        view = adapter.views[self.bid]; item = view['items'][0]
        self.core.batch_control(request_id='external-removal', batch_id=self.bid,
            expected_hash=view['plan_hash'], action='dismiss', item_indices=[0],
            expected_versions={'0':item['version']})
        self.settle()
        snapshot = adapter.snapshot()[0]
        visible_selected = sum((i['index'] not in snapshot['selection']) if snapshot['selectAll']
                               else (i['index'] in snapshot['selection']) for i in snapshot['items'])
        self.assertEqual(snapshot['selectedCount'], visible_selected)

    def test_rejected_file_import_explains_what_was_skipped(self):
        self.widget.timer.stop(); self.settle()
        invalid = self.root/'unsupported.txt'; invalid.write_text('isolated')
        self.widget.add_files([str(invalid), str(self.root/'missing.mp4'), str(self.root)])
        self.assertTrue(self.notices, 'Rejected imports must give a visible reason')
        self.assertEqual(self.widget.batches.views, {})

    def test_bad_file_does_not_prevent_valid_files_being_added(self):
        self.widget.timer.stop(); self.settle()
        valid = self.root/'valid.wav'; valid.write_bytes(b'isolated media placeholder')
        empty = self.root/'empty.mp4'; empty.touch()
        shortcut = self.root/'shortcut.wav'; shortcut.symlink_to(valid)
        self.widget.add_files([str(valid), str(empty), str(shortcut)])
        self.until(lambda: self.widget.batches.worker is None and not self.widget.batches.commands)
        self.assertEqual(len(self.widget.batches.views), 1)
        view = next(iter(self.widget.batches.views.values()))
        self.assertEqual(view['total'], 1)
        self.assertIn('2 项', ' '.join(self.notices))
        self.assertTrue(valid.exists()); self.assertTrue(shortcut.is_symlink())
        self.assertEqual(self.run_calls, [])

    def test_malformed_stage_reply_releases_submit_and_keeps_link(self):
        self.link_entry()
        original = self.main._workspace_batch_dispatcher
        for incomplete in ({}, {'batch_id':'extract-incomplete'}):
            with self.subTest(reply=incomplete):
                self.notices.clear()
                def malformed(operation, **arguments):
                    return incomplete.copy() if operation == 'batch_stage' else original(operation, **arguments)
                with patch.object(self.main, '_workspace_batch_dispatcher', malformed):
                    self.js('grab({preventDefault(){}})')
                    self.until(lambda: bool(self.notices))
                    self.until(lambda: not self.js('linkSubmitting') and self.widget.batches.worker is None)
                self.assertEqual(self.js('document.querySelector("#url").value'), 'https://example.com/submitted.mp4')
                self.assertEqual(self.widget.batches.views, {})

    def test_invalid_queued_arguments_do_not_block_later_valid_commands(self):
        self.widget.timer.stop(); self.settle()
        adapter = self.widget.batches
        with patch.object(adapter, '_pump'):
            adapter.set_model('missing-batch', 'local_sensevoice_q8')
            adapter.stage([dict(source='https://example.com/after-bad-command.mp4')])
        adapter._pump()
        self.until(lambda: adapter.worker is None and not adapter.commands)
        self.assertEqual(len(adapter.views), 1)
        self.assertTrue(self.notices)

    def test_restored_work_cannot_expand_a_stale_whole_batch_start(self):
        self.stage(3); self.widget.timer.stop(); self.settle()
        view = self.widget.batches.views[self.bid]; item = view['items'][0]
        args = dict(batch_id=self.bid, expected_hash=view['plan_hash'], item_indices=[0],
                    expected_versions={'0':item['version']})
        self.core.batch_control(request_id='hide-first', action='dismiss', **args)
        self.settle()
        stale = self.widget.batches.views[self.bid]
        self.core.batch_control(request_id='restore-first', action='restore', batch_id=self.bid,
                               expected_hash=stale['plan_hash'], dismissal_id='hide-first')
        with self.assertRaisesRegex(library.RevisionConflict, '范围已变化'):
            self.core.batch_control(request_id='stale-whole-start', action='start', batch_id=self.bid,
                expected_hash=stale['plan_hash'], expected_visibility=stale['visibility_version'],
                network_consent=True)
        self.assertEqual(self.run_calls, [])


class ExtractionWindowAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ExtractionMainWindowTests.setUpClass()

    def setUp(self):
        self.fixture = ExtractionMainWindowTests(); self.fixture.setUp()
        self.main, self.widget = self.fixture.main, self.fixture.widget
        self.widget.timer.stop()

    def tearDown(self):
        self.fixture.until(lambda: self.widget.batches.worker is None and self.main._feed_worker is None)
        self.fixture.tearDown()

    def test_close_saves_accepted_commands_queued_behind_a_read(self):
        fixture = self.fixture
        main, widget = fixture.main, fixture.widget
        widget.timer.stop()
        runtime = ExpansionTests.runtime(fixture, lambda *_: self.fail('recognition must not start'))
        core = WorkspaceService(runtime=runtime)
        entered, release = threading.Event(), threading.Event()
        def dispatch(operation, **arguments):
            if operation == 'batch_poll':
                entered.set(); release.wait(4)
            return getattr(core, operation)(**arguments)
        main._workspace_batch_dispatcher = dispatch
        try:
            widget.batches.refresh(force=True); fixture.until(entered.is_set)
            widget.batches.stage([dict(source='https://example.com/queued-before-close.mp4')])
            self.assertEqual(len(widget.batches.commands), 1)
            timer = threading.Timer(.05, release.set); timer.start()
            with patch.object(main, '_choose_xhs_exit_action', return_value='keep'), \
                    patch.object(main, '_settle_xhs_exit', return_value=True), \
                    patch('src.ui.main_window.dialogs.confirm', return_value=True):
                main.close()
                fixture.until(lambda: not main.isVisible())
            timer.join(1)
            self.assertEqual(len(core.batch_list()['batches']), 1, 'Accepted imports must be saved before closing')
        finally:
            release.set()
            fixture.until(lambda: widget.batches.worker is None)

    def test_rss_cancel_preserves_input(self):
        from types import SimpleNamespace
        text = 'https://example.com/show.xml'
        with patch('src.core.podcast.looks_like_feed', return_value=True), \
                patch('src.core.podcast.fetch_feed', return_value=SimpleNamespace(title='隔离播客',episodes=[])), \
                patch('src.ui.podcast_dialog.PodcastPicker', return_value=SimpleNamespace(exec=lambda:0,chosen=[])):
            self.fixture.js(f'stageLink({json.dumps(text)});grab({{preventDefault(){{}}}})')
            self.fixture.until(lambda: self.main._feed_worker is None and not self.fixture.js('linkSubmitting'))
        self.assertEqual(self.fixture.js('document.querySelector("#url").value'), text)
        self.assertEqual(self.main.tasks, [])

    def test_rss_fallback_waits_for_actual_task_save_and_keeps_failed_link(self):
        runtime = ExpansionTests.runtime(self.fixture, lambda *_: self.fail('recognition must not start'))
        core = WorkspaceService(runtime=runtime)
        notices = []; self.widget.bridge.notice.connect(notices.append)
        def dispatch(operation, **arguments):
            if operation == 'batch_stage':
                raise OSError('隔离测试：任务保存失败')
            return getattr(core, operation)(**arguments)
        self.main._workspace_batch_dispatcher = dispatch
        text = 'https://example.com/fallback.xml'
        with patch('src.core.podcast.looks_like_feed', return_value=True), \
                patch('src.core.podcast.fetch_feed', return_value=None):
            self.fixture.js(f'stageLink({json.dumps(text)});grab({{preventDefault(){{}}}})')
            self.fixture.until(lambda: bool(notices))
            self.fixture.until(lambda: not self.fixture.js('linkSubmitting'))
        self.assertEqual(self.fixture.js('document.querySelector("#url").value'), text)
        self.assertEqual(core.batch_list()['batches'], [])

    def test_failed_task_save_cancels_close_and_preserves_input(self):
        runtime = ExpansionTests.runtime(self.fixture, lambda *_: self.fail('recognition must not start'))
        core = WorkspaceService(runtime=runtime)
        entered, release = threading.Event(), threading.Event()
        def dispatch(operation, **arguments):
            if operation == 'batch_stage':
                entered.set(); release.wait(4)
                raise OSError('隔离测试：磁盘暂不可写')
            return getattr(core, operation)(**arguments)
        self.main._workspace_batch_dispatcher = dispatch
        try:
            self.fixture.js("stageLink('https://example.com/unsaved.mp4');grab({preventDefault(){}})")
            self.fixture.until(entered.is_set)
            with patch.object(self.main, '_choose_xhs_exit_action', return_value='keep'):
                self.main.close(); release.set()
                self.fixture.until(lambda: not self.main._close_settling)
            self.assertTrue(self.main.isVisible())
            self.assertFalse(self.widget.batches.draining)
            self.assertEqual(self.fixture.js('document.querySelector("#url").value'), 'https://example.com/unsaved.mp4')
        finally:
            release.set(); self.fixture.until(lambda: self.widget.batches.worker is None)

    def test_cancelled_exit_reopens_intake_instead_of_leaving_it_locked(self):
        with patch.object(self.main, '_choose_xhs_exit_action', return_value='keep'), \
                patch.object(self.main, '_settle_xhs_exit', return_value=False):
            self.main.close()
        self.assertTrue(self.main.isVisible())
        self.assertFalse(self.widget.batches.draining)
        self.assertFalse(self.main._close_settling)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(ExtractionAdversarialTests(name) for name in ExtractionAdversarialTests.__dict__
                              if name.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(ExtractionWindowAdversarialTests))
    return suite
