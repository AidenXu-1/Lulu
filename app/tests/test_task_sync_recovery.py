"""Visible save recovery with isolated files, real ledger and no processing."""
import threading
from unittest import TestCase
from unittest.mock import patch

from src.core import library
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.gui_task_recovery import GuiTaskRecoveryStore
from src.core.models import Transcript
from src.core.task import TaskState
from tests import test_homepage_batch_save as batch_fixture
from tests.test_extraction_workspace import ExtractionWorkspaceFixture


class TaskSyncRecoveryTests(TestCase):
    tick = ExtractionWorkspaceFixture.tick
    until = ExtractionWorkspaceFixture.until
    js = ExtractionWorkspaceFixture.js

    def setUp(self):
        self.fixture = batch_fixture.HomepageBatchSaveTests("runTest")
        self.fixture.setUp()
        self.window = self.fixture.window
        self.widget = self.window.extraction_workspace
        self.control = self.fixture.control
        self.store = self.fixture.store
        self.fail_view = False
        self.store._dispatch = self.dispatch
        self.notify = patch("src.ui.main_window.MainWindow._notify_task_completed")
        self.notify.start()
        self.widget.setParent(None)
        self.widget.resize(1463, 925)
        self.widget.show()
        self.until(lambda: self.widget.ready)
        self.original_id = library.save_document("已有原稿", "原有资料")
        self.protected = {p: p.read_bytes() for p in library.LIBRARY_DIR.rglob("*") if p.is_file()}
        model = self.fixture.root / "model-fixture.bin"
        model.write_bytes(b"existing model")
        self.protected[model] = model.read_bytes()

    def tearDown(self):
        self.widget.hide()
        self.widget.setParent(self.window)
        self.notify.stop()
        self.fixture.tearDown()

    def dispatch(self, command, arguments):
        if command == "gui.jobs.view" and self.fail_view:
            raise RuntimeError("CORE_TRANSPORT_UNAVAILABLE")
        return self.fixture.dispatch(command, arguments)

    def wait_idle(self):
        self.fixture.wait_idle()

    def click_sync(self):
        self.widget.publish(force=True)
        self.until(lambda: self.js('Boolean(document.querySelector("#queue-sync:not([hidden]) button"))'), seconds=3)
        self.js('document.querySelector("#queue-sync button").click()')
        self.until(lambda: not self.window._task_queue_store_failed, seconds=5)
        self.wait_idle()
        self.widget.publish(force=True)
        self.until(lambda: self.js('document.querySelector("#queue-sync").hidden'))

    def assert_protected(self):
        self.assertTrue(all(p.read_bytes() == data for p, data in self.protected.items()))
        self.assertEqual(library.load(self.original_id).text, "已有原稿")

    def _completed_save_failure(self, *, committed):
        self.window._collect_transcribe_group([batch_fixture._candidate(0)], "最后一项")
        self.wait_idle()
        task = self.window.tasks[0]
        row = self.widget.rows()[0]
        self.widget.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        worker = self.window.current[1]
        def reject_done(command, arguments):
            if command == "gui.jobs.transition" and arguments["state"] == "succeeded":
                if committed:
                    self.fixture.runtime.gui_jobs_transition(**arguments)
                raise RuntimeError("CORE_TRANSPORT_UNAVAILABLE")
        self.fixture.reject = reject_done
        worker.finished_ok.emit(Transcript(text="已生成的完整文稿", segments=[], source=task.source))
        worker.finish_thread()
        self.wait_idle()
        self.assertEqual(task.state, TaskState.DONE)
        self.assertTrue(self.window._task_queue_store_failed)
        entry_id = task.library_id
        library.save_markdown(entry_id, "用户随后修改的稿件")
        self.fixture.reject = None
        starts = len(batch_fixture._FakeWorker.instances)
        self.click_sync()
        self.assertEqual(self.control.show(task.id).state.value, "succeeded")
        self.assertEqual(len(batch_fixture._FakeWorker.instances), starts)
        self.assertEqual(task.library_id, entry_id)
        self.assertEqual(library.load(entry_id).text, "用户随后修改的稿件")
        reopened = CoreTaskQueueStore(self.dispatch, async_write=True).load()
        merged, _ = self.window._task_queue_recovery.merge(reopened)
        self.assertEqual(merged.tasks[0].state, TaskState.DONE)
        self.assertEqual(merged.tasks[0].library_id, entry_id)
        self.assertEqual(merged.tasks[0].transcript.text, "用户随后修改的稿件")
        self.assert_protected()

    def test_completed_save_retry_acknowledges_without_recognising_or_overwriting(self):
        self._completed_save_failure(committed=False)

    def test_completed_reply_lost_is_read_back_without_replaying_or_overwriting(self):
        self._completed_save_failure(committed=True)

    def test_reopened_unsaved_body_is_saved_from_its_card_without_reprocessing(self):
        self.window._collect_transcribe_group([batch_fixture._candidate(0)], "保存文稿")
        self.wait_idle()
        row = self.widget.rows()[0]
        self.widget.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        task, worker = self.window.current
        with patch.object(library, "save", side_effect=OSError("disk full")):
            worker.finished_ok.emit(Transcript(text="重开后应保留的完整文稿", segments=[], source=task.source))
            worker.finish_thread()
            self.wait_idle()
        pending_id = task._pending_library_id
        self.assertFalse(task.library_id)
        reopened = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.window.tasks = []
        self.window.task_groups = []
        self.window._task_queue_recovery_checked = False
        self.window._core_saved_signature = None
        self.window._task_queue_connection_ready(reopened, reopened.load())
        self.wait_idle()
        row = self.widget.rows()[0]
        self.assertTrue(row["savePending"])
        self.widget.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        restored = self.window.tasks[0]
        self.assertEqual(restored.state, TaskState.DONE)
        self.assertEqual(restored.library_id, pending_id)
        self.assertEqual(restored.transcript.text, "重开后应保留的完整文稿")
        self.assertIn("重开后应保留的完整文稿", library.load(pending_id).text)
        self.assertEqual(len(batch_fixture._FakeWorker.instances), 1)
        self.assert_protected()

    def delete_last_group(self):
        self.window._collect_transcribe_group([batch_fixture._candidate(0), batch_fixture._candidate(1)], "删除最后一组")
        self.wait_idle()
        self.fail_view = True
        self.window._delete_task_group(self.window.task_groups[0].id)
        self.wait_idle()
        self.assertTrue(self.window._task_queue_store_failed)
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.task_groups, [])

    def test_empty_workspace_has_sync_button_and_deleted_cards_stay_removed(self):
        self.delete_last_group()
        self.fail_view = False
        self.click_sync()
        self.assertEqual(CoreTaskQueueStore(self.dispatch, async_write=True).load().tasks, ())
        self.assertEqual(len(self.control.list()), 2, "Keep the authoritative task history")
        self.assertTrue(all(j.state.value == "cancelled" for j in self.control.list()))
        self.assertFalse(batch_fixture._FakeWorker.instances)
        self.assert_protected()

    def test_deleted_cards_stay_hidden_when_reopened_before_save_recovery(self):
        self.delete_last_group()
        self.window._close_settling = True
        self.window._persist_task_queue(force=True)
        self.window._close_settling = False
        recovery = GuiTaskRecoveryStore(self.window._task_queue_recovery.path)
        self.window._task_queue_recovery = recovery
        self.window._task_queue_recovery_checked = False
        self.window._core_hidden_intents = set()
        self.window._core_saved_signature = None
        reopened = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.window._task_queue_connection_ready(reopened, reopened.load())
        self.wait_idle()
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.task_groups, [])
        self.assertTrue(self.window._task_queue_store_failed)
        self.fail_view = False
        self.click_sync()
        self.assertEqual(reopened.load().tasks, ())
        self.assertEqual(len(self.control.list()), 2)
        self.assertFalse(batch_fixture._FakeWorker.instances)
        self.assert_protected()

    def test_disk_failure_ends_sync_and_allows_manual_retry_without_losing_removals(self):
        self.delete_last_group()
        draft = self.window._task_queue_recovery.path.read_bytes()
        self.fail_view = False
        with patch.object(self.window._task_queue_recovery, "save", side_effect=OSError("disk full")):
            self.widget.dispatch("sync", {})
            self.wait_idle()
            self.assertFalse(self.window._task_queue_sync_retry_pending)
            self.assertTrue(self.window._task_queue_store_failed)
            self.assertFalse(self.window._task_queue_auto_sync_allowed)
            self.widget.publish(force=True)
            self.until(lambda: self.js('!document.querySelector("#queue-sync button").disabled'))
            self.assertEqual(self.window._task_queue_recovery.path.read_bytes(), draft)
        self.click_sync()
        self.assertEqual(self.store.load().tasks, ())
        self.assertFalse(batch_fixture._FakeWorker.instances)
        self.assert_protected()

    def test_rendered_continue_button_recovers_only_the_selected_member(self):
        self.window._collect_transcribe_group([batch_fixture._candidate(0), batch_fixture._candidate(1)], "恢复按钮")
        self.wait_idle()
        def fail_start(command, arguments):
            if command == "gui.jobs.transition" and arguments["state"] == "running":
                raise RuntimeError("CORE_TRANSPORT_UNAVAILABLE")
        self.fixture.reject = fail_start
        task = self.window.tasks[0]
        row = self.widget.rows()[0]
        self.widget.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        self.assertTrue(self.window._task_queue_store_failed)
        self.fixture.reject = None
        self.widget.dispatch("expanded", {"id": row["id"], "expanded": True})
        self.widget.publish(force=True)
        selector = '[...document.querySelectorAll(".member button")].find(b=>b.textContent.trim()==="继续")'
        self.until(lambda: self.js(f'Boolean({selector})'))
        self.js(f'({selector}).click()')
        self.until(lambda: self.window.current is not None)
        self.wait_idle()
        self.assertEqual(self.window.current[0].id, task.id)
        self.assertEqual(self.control.show(task.id).state.value, "running")
        self.assertEqual(self.window.tasks[1].state, TaskState.PENDING)
        self.assertEqual(len(batch_fixture._FakeWorker.instances), 1)
        self.assert_protected()

    def test_sync_clicks_share_one_read_and_do_not_retry_after_failure(self):
        self.delete_last_group()
        entered, release = threading.Event(), threading.Event()
        def fail_load():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture deadline")
            raise RuntimeError("JOB_CORE_ACCESS_INVALID")
        with patch.object(self.store, "load", side_effect=fail_load) as load:
            try:
                self.widget.dispatch("sync", {})
                self.assertTrue(entered.wait(1))
                self.widget.dispatch("sync", {})
            finally:
                release.set()
                self.wait_idle()
            self.assertEqual(load.call_count, 1)
            self.assertTrue(self.window._task_queue_store_failed)
            for _ in range(3):
                self.window._sync_shared_tasks()
            self.assertEqual(load.call_count, 1)
        self.fail_view = False
        self.click_sync()
        self.assertFalse(batch_fixture._FakeWorker.instances)
        self.assert_protected()
