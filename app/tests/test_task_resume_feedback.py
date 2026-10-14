"""Explicit recovery through the real GUI, isolated ledger and rendered WebChannel."""
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer
from tests.test_gui_submission_repair import GuiAsyncSubmissionTests as AsyncFixture
from tests.test_extraction_workspace import ExtractionWorkspaceFixture
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from tests.test_shared_core_gui_task_ledger import _runtime
from src.core import library
from src.core.agent_entry.native_transport import XpcTransportError
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.models import Transcript
from src.core.task import Task, TaskState


class TaskResumeTests(unittest.TestCase):
    setUpClass = AsyncFixture.__dict__["setUpClass"]
    tearDown = AsyncFixture.tearDown
    wait_saves = AsyncFixture.wait_saves
    group = AsyncFixture.group

    def setUp(self):
        AsyncFixture.setUp(self)
        self.window._task_queue_sync_timer.stop()
        self.control = JobControlService(JobLedgerStore(self.root.resolve() / "jobs", FakeIntegrityAnchorStore()))
        self.runtime = _runtime(self.control, [f"resume-{i}" for i in range(100)])
        self.store = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.window._task_queue_store = self.store

    def dispatch(self, command, arguments):
        if command == "gui.jobs.batch":
            return {"results": [self.dispatch(op["command"], op["arguments"]) for op in arguments["operations"]], "error": None}
        return getattr(self.runtime, command.replace(".", "_"))(**arguments)

    def queued(self, count=1):
        tasks = [self.store.prepare(Task(i, f"fixture {i}", f"fixture-{i}.wav", "file")) for i in range(count)]
        for task in tasks:
            self.window._freeze_task_policy(task)
            task.state = TaskState.QUEUED
        self.store.save(tasks, [])
        self.window.tasks = tasks
        self.window._task_queue_connection_failed("CORE_TASK_SERVICE_UNAVAILABLE:load:30")
        return tasks

    def test_continue_reconnects_once_and_starts_only_selected_after_save(self):
        first, other = self.queued(2)
        old_id = library.save_document("原有文稿正文", "原稿")
        model = self.root / "models/unchanged.bin"
        model.parent.mkdir(); model.write_bytes(b"existing model fixture")
        protected = {p: p.read_bytes() for p in library.LIBRARY_DIR.rglob("*") if p.is_file()}
        protected[model] = model.read_bytes()
        entered, release = threading.Event(), threading.Event()
        original = self.store.load
        def slow_load():
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture deadline")
            return original()
        def transcribe(*args, **kwargs):
            self.assertEqual(self.control.show(first.id).state.value, "running")
            return Transcript(text="恢复后新文稿", segments=[])
        with patch.object(self.store, "load", side_effect=slow_load) as load, \
                patch("src.ui.worker.transcribe_source", side_effect=transcribe) as run:
            try:
                self.window._start_task(first.id)
                self.assertTrue(entered.wait(1), "Continue must initiate a real connection read")
                self.window._start_task(first.id)
                self.assertTrue(first._core_recovered_queue)
                run.assert_not_called()
            finally:
                release.set()
                self.wait_saves()
            self.assertEqual(load.call_count, 1)
            self.assertEqual(run.call_count, 1, [(t.state, t.message, t.error) for t in self.window.tasks])
        self.assertEqual(first.state, TaskState.DONE)
        self.assertTrue(other._core_recovered_queue)
        self.assertEqual(library.load(old_id).text, "原有文稿正文")
        self.assertTrue(all(p.read_bytes() == data for p, data in protected.items()))
        restored = CoreTaskQueueStore(self.dispatch, async_write=True).load()
        done = next(t for t in restored.tasks if t.id == first.id)
        self.assertEqual(done.transcript.text, library.load(first.library_id).text)
        self.assertIn("恢复后新文稿", done.transcript.text)

    def test_failed_continue_remains_paused_without_background_retry(self):
        task, = self.queued()
        with patch.object(self.store, "load", side_effect=XpcTransportError(30)) as load:
            self.window._start_task(task.id)
            self.wait_saves()
            self.assertEqual(load.call_count, 1)
            self.assertTrue(task._core_recovered_queue)
            self.assertFalse(self.window._task_queue_auto_sync_allowed)
            self.window._sync_shared_tasks()
            self.assertEqual(load.call_count, 1)
        with patch("src.ui.worker.transcribe_source", return_value=Transcript(text="重试正文", segments=[])) as run:
            self.window._start_task(task.id)
            self.wait_saves()
            self.assertEqual(run.call_count, 1)
        self.assertEqual(task.state, TaskState.DONE)

    def test_new_pending_start_shows_connection_pause_until_service_recovers(self):
        task = self.store.prepare(Task(1, "新输入", "new-input.wav", "file"))
        self.window.tasks = [task]
        self.window._task_queue_connection_failed("CORE_TASK_SERVICE_UNAVAILABLE:load:30")
        entered, release = threading.Event(), threading.Event()
        original = self.store.load
        def slow_load():
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture deadline")
            return original()
        with patch.object(self.store, "load", side_effect=slow_load), \
                patch("src.ui.worker.transcribe_source", return_value=Transcript(text="新输入已恢复", segments=[])) as run:
            try:
                self.window._start_task(task.id)
                self.assertTrue(entered.wait(1))
                row = self.window.extraction_workspace.rows()[0]
                self.assertEqual(row["state"], "queued")
                self.assertTrue(row["recovered"])
                self.assertIn("重新连接", row["message"])
                run.assert_not_called()
            finally:
                release.set(); self.wait_saves()
            self.assertEqual(run.call_count, 1)
        self.assertEqual(task.state, TaskState.DONE)

    def test_cancel_during_reconnect_revokes_start_intent(self):
        task, = self.queued()
        entered, release = threading.Event(), threading.Event()
        original = self.store.load
        def slow_load():
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture deadline")
            return original()
        with patch.object(self.store, "load", side_effect=slow_load), \
                patch("src.ui.worker.transcribe_source") as run:
            try:
                self.window._start_task(task.id)
                self.assertTrue(entered.wait(1))
                self.window._cancel_task(task.id)
            finally:
                release.set(); self.wait_saves()
            run.assert_not_called()
        self.assertEqual(task.state, TaskState.CANCELLED)

    def test_close_during_reconnect_revokes_start_intent(self):
        task, = self.queued()
        entered, release = threading.Event(), threading.Event()
        original = self.store.load
        def slow_load():
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture deadline")
            return original()
        with patch.object(self.store, "load", side_effect=slow_load), \
                patch("src.ui.worker.transcribe_source") as run:
            try:
                self.window._start_task(task.id)
                self.assertTrue(entered.wait(1))
                self.window._close_settling = True
            finally:
                release.set(); self.wait_saves()
            run.assert_not_called()
        self.assertTrue(task._core_recovered_queue)
        self.assertFalse(self.window._task_queue_resume_ids)

    def test_delete_during_reconnect_does_not_resurrect_or_execute(self):
        task, = self.queued()
        entered, release = threading.Event(), threading.Event()
        original = self.store.load
        def slow_load():
            snapshot = original()
            entered.set()
            if not release.wait(3): raise RuntimeError("fixture deadline")
            return snapshot
        with patch.object(self.store, "load", side_effect=slow_load), \
                patch("src.ui.worker.transcribe_source") as run:
            try:
                self.window._start_task(task.id)
                self.assertTrue(entered.wait(1))
                self.window._delete_task(task.id)
            finally:
                release.set(); self.wait_saves()
            run.assert_not_called()
        self.assertIsNone(self.window._task(task.id))
        self.assertEqual(CoreTaskQueueStore(self.dispatch, async_write=True).load().tasks, ())
        self.assertEqual(len(self.control.list()), 1, "Removing a card must preserve Core history")

    def test_continue_waits_for_previous_reader_to_finish_before_reconnecting(self):
        from src.ui.worker import CoreTaskStartupWorker
        task, = self.queued()
        release = threading.Event()
        class FinishingReader(CoreTaskStartupWorker):
            def run(worker):
                worker.failed.emit("CORE_TASK_SERVICE_UNAVAILABLE:load:30")
                release.wait(3)
        previous = FinishingReader(self.window, store=self.store)
        self.window._task_queue_startup_worker = previous
        self.window._task_queue_startup_pending = True
        previous.failed.connect(self.window._task_queue_connection_failed)
        previous.start()
        loop = QEventLoop(); previous.failed.connect(loop.quit); QTimer.singleShot(1000, loop.quit); loop.exec()
        with patch.object(self.store, "load", wraps=self.store.load) as load, \
                patch("src.ui.worker.transcribe_source", return_value=Transcript(text="连接收尾后继续", segments=[])) as run:
            previous_finished = False
            try:
                self.window._start_task(task.id)
                self.assertTrue(task._core_recovered_queue)
                release.set()
                previous.wait(1000)
                previous_finished = True
                # Deliver finish, then allow its queued reconnection to start.
                deadline = time.monotonic() + 1
                while not load.call_count and time.monotonic() < deadline:
                    loop = QEventLoop(); QTimer.singleShot(10, loop.quit); loop.exec()
                self.assertEqual(load.call_count, 1)
                self.wait_saves()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()
                if not previous_finished: previous.wait(1000)

    def test_reconnected_but_save_failed_keeps_input_and_never_executes(self):
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        task, = self.queued()
        self.window._task_queue_recovery = GuiTaskRecoveryStore(self.root / "unsynced.json")
        self.window._task_queue_recovery_checked = True
        dispatch = self.store._dispatch
        def fail_write(command, arguments):
            if command == "gui.jobs.batch":
                return {"results": [], "error": "JOB_CORE_ACCESS_INVALID"}
            return dispatch(command, arguments)
        with patch.object(self.store, "_dispatch", side_effect=fail_write), \
                patch("src.ui.worker.transcribe_source") as run:
            self.window._start_task(task.id); self.wait_saves()
            run.assert_not_called()
        self.assertTrue(self.window._task_queue_store_failed)
        self.assertTrue(task._core_recovered_queue)
        self.assertIn(task.source, (self.root / "unsynced.json").read_text())

    def test_reopened_batch_continues_from_visible_button_and_preserves_results(self):
        group = self.group(2)
        self.wait_saves()
        for task in self.window.tasks: task.state = TaskState.QUEUED
        self.store.save(self.window.tasks, self.window.task_groups)
        self.window.tasks = []; self.window.task_groups = []
        reopened = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.window._task_queue_connection_ready(reopened, reopened.load())
        widget = self.window.extraction_workspace
        widget.setParent(None); widget.resize(1200, 800); widget.show()
        def wait(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate(): return
                loop = QEventLoop(); QTimer.singleShot(15, loop.quit); loop.exec()
            self.fail("Rendered recovery did not settle")
        def js(source):
            result = []; widget.page.runJavaScript(source, result.append); wait(lambda: bool(result)); return result[0]
        try:
            wait(lambda: widget.ready)
            widget.publish(force=True)
            wait(lambda: js('Boolean(document.querySelector(".group-actions .primary"))'))
            self.assertEqual(js('document.querySelector(".group-status").textContent'), "2 条已暂停")
            with patch("src.ui.worker.transcribe_source", return_value=Transcript(text="整组恢复正文", segments=[])) as run:
                self.window._pump(); self.assertEqual(run.call_count, 0)
                js('document.querySelector(".group-actions .primary").click()')
                self.wait_saves()
                self.assertEqual(run.call_count, 2)
            snapshot = CoreTaskQueueStore(self.dispatch, async_write=True).load()
            self.assertTrue(all(t.state is TaskState.DONE for t in snapshot.tasks))
            expected = {t.id: library.load(t.library_id).text for t in self.window.tasks}
            self.assertTrue(all("整组恢复正文" in body for body in expected.values()))
            self.assertEqual({t.id: t.transcript.text for t in snapshot.tasks}, expected)
            self.assertEqual(len(snapshot.groups[0].members), 2)
        finally:
            widget.close(); widget.setParent(self.window)


class ResumeRenderingTests(ExtractionWorkspaceFixture, unittest.TestCase):
    def test_group_individual_and_all_continue_exclude_readonly_and_completed(self):
        paths = [self.root / f"{i}.wav" for i in range(4)]
        for path in paths: path.write_bytes(b"fixture")
        self.widget.add_files([str(p) for p in paths])
        for task in self.main.tasks:
            task.state = TaskState.QUEUED; task._core_recovered_queue = True
        self.main.tasks[2]._core_view_only = True
        self.main.tasks[3].state = TaskState.DONE
        self.widget.publish(force=True)
        self.until(lambda: self.js('tasks.length') == 4)
        self.js('toggleGroup(tasks[0].id)')
        self.assertEqual(self.js('[...document.querySelectorAll(".member button")].filter(n=>n.textContent.trim()==="继续").length'), 2)
        self.js('startAll()')
        self.until(lambda: len([c for c in self.main.calls if isinstance(c, tuple) and c[0] == "start"]) == 2)
        self.assertEqual([c for c in self.main.calls if isinstance(c, tuple) and c[0] == "start"], [("start", 1), ("start", 2)])

    def test_paused_queue_does_not_count_ahead_and_count_refreshes_on_resume(self):
        self.add(2)
        first, second = self.main.tasks
        for task in self.main.tasks: task.state = TaskState.QUEUED
        first._core_recovered_queue = True
        self.widget.publish(force=True)
        self.until(lambda: self.js('document.querySelectorAll(".queue-number").length') == 1)
        self.assertEqual(self.js('document.querySelector(".queue-number").firstChild.textContent'), "0")
        first._core_recovered_queue = False
        self.widget.publish(force=True)
        self.until(lambda: self.js('document.querySelectorAll(".queue-number").length') == 2)
        self.assertEqual(self.js('document.querySelector(`[data-job="${tasks[1].id}"] .queue-number`).firstChild.textContent'), "1")
