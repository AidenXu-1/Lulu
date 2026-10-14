"""Task synchronization needs no refresh or reconnect user action."""
import time
import threading
from unittest.mock import patch
from PySide6.QtTest import QTest
from tests.test_task_queue_ui_recovery import TaskQueueUiRecoveryTests
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.models import Transcript
from src.core.task import TaskState
from src.ui.main_window import MainWindow
from src.core.agent_entry.native_transport import XpcTransportError
from PySide6.QtWidgets import QPushButton
from tests.test_extraction_workspace import ExtractionWorkspaceTests


def job(job_id="cli-job", state="succeeded", revision=4):
    return {"job_id": job_id, "job_revision": revision, "state": state,
            "job_kind": "local_transcription", "input_ref": "source:sha256:" + "0" * 64,
            "gui_task": {}, "result_ref": "lulu-result:" + job_id + ":sha256:" + "0" * 64,
            "created_by": "agent_cli"}


class SharedTaskDeliveryUiTests(TaskQueueUiRecoveryTests):
    tick = ExtractionWorkspaceTests.tick
    until = ExtractionWorkspaceTests.until
    js = ExtractionWorkspaceTests.js
    def settle(self, worker):
        end = time.monotonic() + 3
        while worker.isRunning() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertFalse(worker.isRunning())
        self.app.processEvents()

    def test_timer_adds_cli_job_once_and_preserves_local_active_object(self):
        jobs = []
        queue = CoreTaskQueueStore(lambda command, arguments: {"jobs": list(jobs)})
        w = MainWindow(task_queue_store=queue); self.windows.append(w); w.show()
        self.assertTrue(w._task_queue_sync_timer.isActive())
        self.assertFalse(any(b.text() in {"刷新任务", "重新连接"} for b in w.findChildren(QPushButton)))
        from src.core.task import Task
        local = Task("gui-local", "正在转录", "/local.wav", "file", state=TaskState.RUNNING)
        w.tasks.append(local)
        jobs.append(job())
        for _ in range(2):
            w._task_queue_sync_timer.timeout.emit()
            self.settle(w._task_queue_startup_worker)
        self.assertEqual([t.id for t in w.tasks], ["gui-local", "cli-job"])
        self.assertIs(w.tasks[0], local)
        self.assertEqual(local.state, TaskState.RUNNING)

    def test_timer_recovers_transient_failure_without_restarting_window(self):
        with patch("src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
                   side_effect=XpcTransportError(20)):
            import sys
            with patch.object(sys, "frozen", True, create=True):
                w = MainWindow();self.windows.append(w);w.show()
                QTest.qWait(30)
                self.settle(w._task_queue_startup_worker)
        self.assertTrue(w._task_queue_auto_sync_allowed)
        def recovered_list(command, arguments):
            self.assertEqual(command, "gui.jobs.list")
            self.assertEqual(arguments, {"include_view": True})
            return {"jobs": [job(state="prepared")], "view": {"revision": 0, "groups": [], "hidden_ids": []}}
        with patch("src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
                   return_value=recovered_list):
            w._task_queue_sync_timer.timeout.emit()
            self.settle(w._task_queue_startup_worker)
        self.assertFalse(w._task_queue_store_failed)
        self.assertEqual([t.id for t in w.tasks], ["cli-job"])

    def test_timer_does_not_repeat_credential_or_integrity_failure(self):
        with patch("src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
                   side_effect=RuntimeError("private credential denied")) as connect, \
                patch.object(MainWindow, "_toast") as toast:
            import sys
            with patch.object(sys, "frozen", True, create=True):
                w = MainWindow(); self.windows.append(w); w.show()
                QTest.qWait(30)
                self.settle(w._task_queue_startup_worker)
            for _ in range(3):
                w._task_queue_sync_timer.timeout.emit()
                self.app.processEvents()
            self.assertEqual(connect.call_count, 1)
            self.assertFalse(w._task_queue_auto_sync_allowed)
            toast.assert_any_call("任务服务暂时不可用，原记录已保留")
            self.assertNotIn("private", str(toast.call_args_list))
            self.widget = w.extraction_workspace
            self.until(lambda: self.widget.ready)
            self.assertNotIn("private credential denied", self.js("document.body.textContent"))

    def test_user_submission_during_sync_waits_for_read_and_is_saved_once(self):
        queue = CoreTaskQueueStore(lambda command, arguments: {
            "jobs": [], "view": {"revision": 0, "groups": [], "hidden_ids": []}
        }, async_write=True)
        w = MainWindow(task_queue_store=queue); self.windows.append(w); w.show()
        snapshot = queue.load()
        entered, release = threading.Event(), threading.Event()
        def read():
            entered.set()
            release.wait(2)
            return snapshot
        with patch.object(queue, "load", side_effect=read), patch.object(queue, "save", return_value=True) as save:
            w._task_queue_sync_timer.timeout.emit()
            self.assertTrue(entered.wait(1))
            from src.core.task import Task
            task = Task("gui-new", "新任务", "/isolated.wav", "file")
            w.tasks.append(task)
            self.assertFalse(w._persist_task_queue())
            save.assert_not_called()
            release.set()
            self.settle(w._task_queue_startup_worker)
            worker = getattr(w, "_core_save_worker", None)
            if worker is not None:
                self.settle(worker)
            save.assert_called_once()
            self.assertIs(w.tasks[0], task)
            self.assertEqual(task.state, TaskState.PENDING)

    def test_completed_cli_task_has_working_view_copy_and_export_buttons(self):
        jobs = []
        queue = CoreTaskQueueStore(lambda command, arguments: {"jobs": list(jobs)})
        w = MainWindow(task_queue_store=queue);self.windows.append(w);w.show()
        jobs.append(job())  # A completion arriving during this session stays visible.
        w._restore_task_queue(queue.load())
        self.widget = w.extraction_workspace
        self.until(lambda: self.widget.ready)
        self.widget.publish(force=True)
        self.until(lambda: self.js('tasks.length') == 1)
        self.js('document.querySelector(".process-footer button").click()')
        self.assertIn('查看文稿', self.js('document.querySelector("#modal-content").textContent'))
        self.assertIn('复制文案', self.js('document.querySelector("#modal-content").textContent'))
        self.assertIn('导出文案', self.js('document.querySelector("#modal-content").textContent'))
        transcript = Transcript(text="最后一句也完整。", segments=[], language="zh", engine="local_sensevoice", source="")
        def click(label):
            self.js(f'[...document.querySelectorAll(".result-assets button")].find(b=>b.textContent.includes("{label}")).click()')
        # Copy is the first action, before opening or saving a library document.
        with patch.object(queue, "read_transcript", return_value=transcript), patch("src.ui.main_window.QGuiApplication.clipboard") as clipboard:
            click('复制文案')
            self.until(lambda: clipboard.return_value.setText.called)
            clipboard.return_value.setText.assert_called_once_with(transcript.text)
            self.settle(w._core_result_worker)
        target = self.root / "export.txt"
        with patch("src.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            click('导出文案')
            self.until(target.is_file)
        self.assertEqual(target.read_text().strip(), transcript.text)
        with patch.object(w, "open_transcript_in_library", return_value="saved-document") as opened:
            click('查看文稿')
            self.until(lambda: opened.called)
            self.assertIs(opened.call_args.args[0], transcript)
        self.assertEqual(w.tasks[0].library_id, "saved-document")
