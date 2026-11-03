"""User actions during delayed replies; no real credentials, network or billing."""
from contextlib import ExitStack
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_agent_workspace import AgentWorkspaceTests
from tests.test_cli_expansion import ExpansionTests
from src.core.agent_workspace.service import WorkspaceService
from src.core.models import Transcript


class CloudExperienceTests(unittest.TestCase):
    setUp = AgentWorkspaceTests.setUp
    runtime = ExpansionTests.runtime

    def setup_dialog(self):
        from PySide6.QtWidgets import QApplication, QWidget, QMessageBox
        from src.ui.cloud_batch import CloudBatchDialog
        self.app=QApplication.instance() or QApplication([])
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.runtime_instance=self.runtime(lambda item,batch,*args:Transcript('完整测试结果',[],engine=batch['engine']))
        self.service=WorkspaceService(runtime=self.runtime_instance)
        self.calls=[];self.block=None;self.fail_operation=None;self.arrived=threading.Event();self.release=threading.Event()
        def dispatch(operation,**args):
            self.calls.append((operation,args))
            result=getattr(self.service,operation)(**args)
            if operation == self.block:
                self.arrived.set();self.release.wait(5)
            if operation == self.fail_operation:
                raise RuntimeError('Isolated delayed read failure')
            return result
        self.stack.enter_context(patch('src.core.agent_workspace.client.dispatch',side_effect=dispatch))
        self.stack.enter_context(patch.object(QMessageBox,'question',return_value=QMessageBox.Yes))
        self.main=QWidget();self.main.cfg=SimpleNamespace(engine_name='dashscope');self.main.open_material_entry=Mock()
        self.dialog=CloudBatchDialog(self.main);self.dialog.timer.stop()
        def cleanup():
            self.block=None;self.release.set();self.dialog.shutdown()
            end=time.monotonic()+6
            while self.dialog.worker is not None and time.monotonic()<end:
                self.app.processEvents();time.sleep(.005)
            self.dialog.deleteLater();self.main.deleteLater();self.app.processEvents()
        self.addCleanup(cleanup)
        self.until(lambda:self.dialog.worker is None)

    def until(self,predicate):
        end=time.monotonic()+8
        while time.monotonic()<end:
            self.app.processEvents()
            if predicate():return
            time.sleep(.005)
        self.fail('User action did not settle')

    def prepared(self,name):
        return self.service.batch_prepare(request_id=name,items=[dict(source=f'https://example.com/{name}.mp3')],engine='dashscope')

    def load(self):
        self.dialog.refresh();self.until(lambda:self.dialog.worker is None and self.dialog.selection.count()>0)

    def test_cancel_click_during_slow_poll_is_kept_and_coalesced(self):
        self.setup_dialog();plan=self.prepared('cancel-queued');self.load()
        self.block='batch_show';self.dialog.show_selected();self.until(self.arrived.is_set)
        self.dialog.cancel();self.dialog.cancel()
        self.block=None;self.release.set()
        self.until(lambda:self.dialog.worker is None)
        cancels=[a for op,a in self.calls if op=='batch_cancel']
        self.assertEqual(cancels,[dict(batch_id=plan['batch_id'])])
        self.assertEqual(self.service.batch_show(plan['batch_id'])['state'],'cancelled')

    def test_start_click_during_refresh_creates_one_batch_after_reply(self):
        self.setup_dialog();self.block='batch_list';self.dialog.refresh();self.until(self.arrived.is_set)
        self.dialog.sources.setPlainText('https://example.com/new.mp3')
        self.dialog.create();self.dialog.create()
        self.block=None;self.release.set()
        self.until(lambda:self.dialog.worker is None)
        prepares=[a for op,a in self.calls if op=='batch_prepare']
        starts=[a for op,a in self.calls if op=='batch_start']
        self.assertEqual(len(prepares),1);self.assertEqual(len(starts),1)
        self.assertEqual(self.dialog.sources.toPlainText(),'')

    def test_second_created_batch_stays_selected_after_refresh(self):
        self.setup_dialog()
        self.dialog.sources.setPlainText('https://example.com/first.mp3');self.dialog.create()
        self.until(lambda:self.dialog.worker is None)
        first=self.dialog.selection.currentData()
        self.dialog.sources.setPlainText('https://example.com/second.mp3');self.dialog.create()
        self.until(lambda:self.dialog.worker is None)
        newest=[a['batch_id'] for op,a in self.calls if op=='batch_start'][-1]
        self.assertNotEqual(first,newest)
        self.assertEqual(self.dialog.selection.currentData(),newest)
        self.assertEqual(self.dialog.batch['batch_id'],newest)

    def test_late_poll_does_not_replace_newly_selected_batch(self):
        self.setup_dialog();self.prepared('first');self.prepared('second');self.load()
        self.dialog.selection.setCurrentIndex(0);self.until(lambda:self.dialog.worker is None)
        self.block='batch_show';self.dialog.show_selected();self.until(self.arrived.is_set)
        self.dialog.selection.setCurrentIndex(1);selected=self.dialog.selection.currentData()
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertEqual(self.dialog.batch['batch_id'],selected)

    def test_queued_force_stop_is_not_downgraded_and_targets_original_batch(self):
        self.setup_dialog();first=self.prepared('force-first');second=self.prepared('force-second');self.load()
        self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(first['batch_id']))
        self.until(lambda:self.dialog.worker is None)
        self.block='batch_show';self.dialog.show_selected();self.until(self.arrived.is_set)
        self.dialog.force();self.dialog.cancel()
        self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(second['batch_id']))
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        cancels=[a for op,a in self.calls if op=='batch_cancel']
        self.assertEqual(cancels,[dict(batch_id=first['batch_id'],force=True)])
        self.assertEqual(self.dialog.batch['batch_id'],second['batch_id'])

    def test_closing_busy_view_stops_polling_and_keeps_accepted_action(self):
        self.setup_dialog();plan=self.prepared('hidden-cancel');self.load();self.dialog.timer.start()
        self.block='batch_show';self.dialog.show_selected();self.until(self.arrived.is_set)
        self.dialog.cancel();self.dialog.close()
        self.assertFalse(self.dialog.timer.isActive())
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertEqual(self.service.batch_show(plan['batch_id'])['state'],'cancelled')

    def test_short_window_keeps_controls_reachable_with_expanded_quota(self):
        self.setup_dialog();self.dialog.resize(780,520);self.dialog.quota.setChecked(True)
        self.dialog.show();self.app.processEvents()
        self.assertLessEqual(self.dialog.height(),550)
        self.assertGreater(self.dialog.scroller.verticalScrollBar().maximum(),0)
        self.dialog.scroller.ensureWidgetVisible(self.dialog.completed)
        self.app.processEvents()
        position=self.dialog.completed.mapTo(self.dialog.scroller.viewport(),self.dialog.completed.rect().bottomLeft())
        self.assertLessEqual(position.y(),self.dialog.scroller.viewport().height())

    def test_uncertain_retry_never_uses_previous_batch_after_selection_changes(self):
        self.setup_dialog();first=self.prepared('unknown-first');second=self.prepared('unknown-second')
        service=self.runtime_instance.expansion_batches();batch=service._read(first['batch_id'])
        batch['state']='partial';batch['items'][0].update(state='recovery_required',cloud_attempt={'state':'unknown'})
        service._save(batch);self.load()
        self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(first['batch_id']))
        self.until(lambda:self.dialog.worker is None)
        self.block='batch_show';self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(second['batch_id']))
        self.until(self.arrived.is_set);self.dialog.retry_unknown()
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertFalse([a for op,a in self.calls if op=='batch_start'])

    def test_resume_reply_does_not_steal_newer_selection(self):
        self.setup_dialog();first=self.prepared('resume-first');second=self.prepared('resume-second');self.load()
        self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(first['batch_id']))
        self.until(lambda:self.dialog.worker is None)
        self.block='batch_show';self.dialog.show_selected();self.until(self.arrived.is_set)
        self.dialog.resume();self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(second['batch_id']))
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertEqual(self.dialog.selection.currentData(),second['batch_id'])
        self.assertEqual(self.dialog.batch['batch_id'],second['batch_id'])

    def test_document_open_waits_for_selected_batch_snapshot(self):
        self.setup_dialog();first=self.prepared('document-first');second=self.prepared('document-second')
        self.service.batch_start(batch_id=first['batch_id'],expected_hash=first['plan_hash'],network_consent=True,cloud_upload_consent=True)
        self.until(lambda:not self.runtime_instance.expansion_batches().workers);self.load()
        self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(first['batch_id']))
        self.until(lambda:self.dialog.worker is None);self.assertTrue(self.dialog.completed.currentData())
        self.block='batch_show';self.dialog.selection.setCurrentIndex(self.dialog.selection.findData(second['batch_id']))
        self.until(self.arrived.is_set);self.dialog.open_document();self.main.open_material_entry.assert_not_called()
        self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)

    def test_new_batch_is_selected_before_slow_list_and_cancel_targets_it(self):
        self.setup_dialog();old=self.prepared('old-selected');self.load()
        self.block='batch_list';self.dialog.sources.setPlainText('https://example.com/new-selected.mp3');self.dialog.create()
        self.until(self.arrived.is_set)
        new=[a['batch_id'] for op,a in self.calls if op=='batch_start'][-1]
        self.dialog.cancel();self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertEqual([a['batch_id'] for op,a in self.calls if op=='batch_cancel'],[new])
        self.assertEqual(self.service.batch_show(old['batch_id'])['state'],'prepared')

    def test_unrelated_read_failure_does_not_unlock_queued_new_submission(self):
        self.setup_dialog();self.block='batch_list';self.dialog.refresh();self.until(self.arrived.is_set)
        self.dialog.sources.setPlainText('https://example.com/locked-submit.mp3');self.dialog.create()
        old_release=self.release;self.release=threading.Event();self.arrived.clear()
        self.block='batch_prepare';self.fail_operation='batch_list';old_release.set()
        self.until(self.arrived.is_set)
        self.assertTrue(self.dialog.submitting);self.assertFalse(self.dialog.start.isEnabled())
        self.fail_operation=None;self.block=None;self.release.set();self.until(lambda:self.dialog.worker is None)
        self.assertEqual(self.dialog.sources.toPlainText(),'')

    def test_finished_count_updates_without_refreshing_the_batch_list(self):
        self.setup_dialog();plan=self.prepared('finish-count');self.load()
        self.service.batch_start(batch_id=plan['batch_id'],expected_hash=plan['plan_hash'],network_consent=True,cloud_upload_consent=True)
        self.until(lambda:not self.runtime_instance.expansion_batches().workers)
        self.dialog.show_selected();self.until(lambda:self.dialog.worker is None)
        self.assertEqual(self.dialog.selection.currentData(),plan['batch_id'])
        self.assertIn('完成1条',self.dialog.selection.currentText())

    def test_large_gui_intake_preserves_whole_batch_without_upload(self):
        self.setup_dialog()
        # Stop after prepare: this verifies whole-batch intake and last-page
        # access without sending a public request or creating thousands of docs.
        self.dialog.begin=Mock()
        links=[f'https://example.com/large-gui-{i}.wav' for i in range(10001)]
        with patch('src.core.secrets.get_config',side_effect=AssertionError('intake must not read credentials')), patch('src.core.pipeline.transcribe_source',side_effect=AssertionError('intake must not upload')):
            self.dialog.sources.setPlainText('\n'.join(links));self.dialog.create()
            self.until(lambda:self.dialog.worker is None and self.dialog.begin.called)
        plan=self.dialog.begin.call_args.args[0]
        self.assertEqual(plan['total'],10001)
        last=self.service.batch_show(batch_id=plan['batch_id'],offset=10000,limit=1)
        self.assertEqual(last['items'][0]['source'],links[-1])
        self.assertEqual(last['items'][0]['index'],10000)
        self.assertFalse(self.runtime_instance.expansion_batches().workers)
