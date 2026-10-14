"""Homepage imports use a real isolated ledger, with no network or recognition."""
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest

from src.core.diagnostics import failure_code
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.gui_task_recovery import GuiTaskRecoveryStore
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.task import Task, TaskState
from src.core.task_queue_store import TaskQueueSnapshot
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from tests.test_shared_core_gui_task_ledger import _runtime
from tests import test_task_groups as group_fixture

_FakeWorker = group_fixture._FakeWorker
_candidate = group_fixture._candidate


class HomepageBatchSaveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = group_fixture.TaskGroupRuntimeTests(methodName="runTest")
        self.fixture.setUpClass()
        self.fixture.setUp()
        self.window = self.fixture.window
        self.root = Path(self.fixture.home.name).resolve()
        self.control = JobControlService(JobLedgerStore(self.root / "jobs", FakeIntegrityAnchorStore()))
        self.runtime = _runtime(self.control, [f"fixture-{i}" for i in range(1000)])
        self.commands = []
        self.reject = None
        self.store = CoreTaskQueueStore(self.dispatch, async_write=True)
        self.store.load()
        self.window._task_queue_store = self.store
        self.window._task_queue_startup_pending = False
        self.window._task_queue_auto_sync_allowed = True
        self.window._task_queue_initial_connection = False
        self.window._task_queue_sync_timer.stop()
        self.window._task_queue_recovery = GuiTaskRecoveryStore(self.root / "recovery" / "unsynced.json")
        self.window._task_queue_recovery_checked = True
        self.messages = []
        self.window._toast = self.messages.append
        self.worker_patch = patch("src.ui.worker.TranscribeWorker", _FakeWorker)
        self.worker_patch.start()

    def dispatch(self, command, arguments):
        self.commands.append(command)
        if command == "gui.jobs.batch":
            results = []
            for operation in arguments["operations"]:
                try:
                    results.append(self.dispatch(operation["command"], operation["arguments"]))
                except Exception as exc:
                    return {"results": results, "error": failure_code(exc)}
            return {"results": results, "error": None}
        if self.reject is not None and command in {"gui.jobs.create", "gui.jobs.transition"}:
            self.reject(command, arguments)
        return getattr(self.runtime, command.replace(".", "_"))(**arguments)

    def wait_idle(self):
        deadline = time.monotonic() + 90
        while (self.window._core_save_worker is not None
               or self.window._task_queue_startup_pending) and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertIsNone(self.window._core_save_worker)
        self.assertFalse(self.window._task_queue_startup_pending)

    def tearDown(self):
        try:
            self.wait_idle()
            if self.window.current:
                task, worker = self.window.current
                self.window._cancel_task(task.id)
                worker.finish_cancelled()
                self.wait_idle()
            self.window._close_settling = True
        finally:
            self.worker_patch.stop()
            self.fixture.tearDown()

    def test_long_captions_save_start_and_keep_original_collection_text(self):
        candidates = [replace(_candidate(0), title="甲" * 501),
                      replace(_candidate(1), title="乙" * 3000)]
        self.window._collect_transcribe_group(candidates, "隔离批次")
        self.wait_idle()
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertEqual(len(self.control.list()), 2)
        group = self.window.task_groups[0]
        self.assertEqual([m.title for m in group.members], ["甲" * 501, "乙" * 3000])
        self.assertEqual([c.title for c in candidates], ["甲" * 501, "乙" * 3000])
        draft = json.loads(self.window._task_queue_recovery.path.read_text())
        self.assertEqual([m["title"] for m in draft["groups"][0]["members"]],
                         ["甲" * 501, "乙" * 3000])
        self.window._start_task_group(group.id)
        self.wait_idle()
        self.assertIsNotNone(self.window.current)
        task, worker = self.window.current
        self.assertTrue(worker.started)
        self.assertEqual(self.control.show(task.id).state.value, "running")
        self.assertTrue(all(len(t.title) <= 80 for t in self.window.tasks))

    def test_partial_save_failure_stops_polling_and_manual_continue_keeps_all_inputs(self):
        def reject_second(command, _arguments):
            if command == "gui.jobs.create" and self.commands.count(command) == 2:
                raise RuntimeError("GUI_TASK_PAYLOAD_INVALID")
        self.reject = reject_second
        self.window._collect_transcribe_group([_candidate(0), _candidate(1)], "隔离失败批次")
        self.wait_idle()
        self.assertTrue(self.window._task_queue_store_failed)
        self.assertEqual(len(self.control.list()), 1)
        self.assertIsNone(self.window.current)
        draft = self.window._task_queue_recovery.path.read_bytes()
        self.assertIn(b"https://example.invalid/1", draft)
        self.assertFalse(self.window._task_queue_auto_sync_allowed)
        self.assertIn("任务信息", self.messages[-1])
        self.assertIn("重新加入", self.window.tasks[1].message)
        commands, messages = list(self.commands), list(self.messages)
        for _ in range(4):
            self.window._sync_shared_tasks()
            QTest.qWait(10)
        self.assertEqual(self.commands, commands)
        self.assertEqual(self.messages, messages)
        self.assertEqual(self.window._task_queue_recovery.path.read_bytes(), draft)

        self.reject = None
        first, other = self.window.tasks
        self.window._start_task(first.id)
        self.wait_idle()
        self.assertEqual(len(self.control.list()), 2, "saved prefix must not be duplicated")
        self.assertEqual(self.window.current[0].id, first.id)
        self.assertEqual(other.state, TaskState.PENDING, "only the selected task may start")
        self.assertTrue(self.window._task_queue_auto_sync_allowed)

    def test_old_unsaved_long_title_reopens_saves_without_losing_original_draft(self):
        original = "旧采集正文" * 200
        task = self.store.prepare(Task(1, original, "https://example.invalid/old", "link"))
        recovery = self.window._task_queue_recovery
        recovery.save([task], [])
        self.assertEqual(json.loads(recovery.path.read_text())["tasks"][0]["task"]["title"], original)
        restored, pending = recovery.merge(TaskQueueSnapshot((), ()))
        self.assertTrue(pending)
        self.assertEqual(restored.tasks[0].title, original)
        self.window.tasks = list(restored.tasks)
        self.window._persist_task_queue()
        self.wait_idle()
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertEqual(len(self.control.list()), 1)
        self.assertEqual(self.store.load().tasks[0].source, "https://example.invalid/old")
        self.assertEqual(len(self.store.load().tasks[0].title), 500)
        self.assertEqual(self.window.tasks[0].title, original)
        self.assertEqual(json.loads(recovery.path.read_text())["tasks"][0]["task"]["title"], original)

    def test_long_skipped_caption_and_group_name_do_not_block_executable_members(self):
        candidates = [_candidate(0), _candidate(1),
                      replace(_candidate(2, skipped_reason="图文作品无音频"), title="图文正文" * 1000)]
        self.window._collect_transcribe_group(candidates, "长作者" * 200)
        self.wait_idle()
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertEqual(len(self.control.list()), 2)
        self.assertEqual(self.window.task_groups[0].members[2].title, "图文正文" * 1000)
        self.assertEqual(len(self.store.load().groups[0].members), 3)

    def test_43_long_captions_and_later_normal_batch_save_and_start_independently(self):
        candidates = [replace(_candidate(i), title="完整作品说明" * 100) for i in range(43)]
        self.window._collect_transcribe_group(candidates, "43条作品")
        self.wait_idle()
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertEqual(len(self.control.list()), 43)
        self.window._collect_transcribe_group([_candidate(43), _candidate(44)], "后一批")
        self.wait_idle()
        self.assertEqual(len(self.control.list()), 45)
        groups = self.store.load().groups
        self.assertEqual([len(group.members) for group in groups], [43, 2])
        self.window._start_task_group(self.window.task_groups[1].id)
        self.wait_idle()
        self.assertEqual(self.window.current[0].stable_key, "douyin:43")
        self.assertTrue(all(t.state is TaskState.PENDING for t in self.window.tasks[:43]))
        self.assertFalse(any("尚未保存" in message for message in self.messages))

    def test_long_skipped_caption_reopens_once_after_executable_members_are_removed(self):
        original = "完整图文说明" * 600
        self.window._collect_transcribe_group([
            _candidate(0), _candidate(1),
            replace(_candidate(2, skipped_reason="图文作品无音频"), title=original),
        ], "长批次名" * 150)
        self.wait_idle()
        for task in list(self.window.tasks):
            self.window._delete_task(task.id)
            self.wait_idle()
        for reopen_index in range(3):
            reopened = CoreTaskQueueStore(self.dispatch, async_write=True)
            snapshot = reopened.load()
            self.window.tasks = []
            self.window.task_groups = []
            self.window._core_hidden_intents = set()
            self.window._core_saved_signature = None
            self.window._task_queue_recovery_checked = False
            self.window._task_queue_connection_ready(reopened, snapshot)
            self.wait_idle()
            self.assertEqual(len(self.window.task_groups), 1)
            if reopen_index == 0:
                self.assertEqual(self.window.task_groups[0].members[0].title, original)
            self.assertEqual(len(reopened.load().groups), 1)
            self.window._sync_shared_tasks()
            self.wait_idle()
            self.assertEqual(len(self.window.task_groups), 1)
        self.assertTrue(any(original in path.read_text()
                            for path in self.window._task_queue_recovery.path.parent.glob("recovered-*.json")))

    def test_unconfirmed_start_remains_paused_and_can_resume_from_its_own_card(self):
        self.window._collect_transcribe_group([_candidate(0), _candidate(1)], "启动故障")
        self.wait_idle()
        def reject_running(command, arguments):
            if command == "gui.jobs.transition" and arguments["state"] == "running":
                raise RuntimeError("CORE_TRANSPORT_UNAVAILABLE")
        self.reject = reject_running
        task = self.window.tasks[0]
        row = next(row for row in self.window.extraction_workspace.rows()
                   if row["name"] == task.title)
        self.window.extraction_workspace.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        self.assertIsNone(self.window.current)
        self.assertTrue(self.window._task_queue_store_failed)
        self.assertEqual(self.control.show(task.id).state.value, "queued")
        row = next(row for row in self.window.extraction_workspace.rows()
                   if row["name"] == task.title)
        self.assertEqual(row["state"], "queued", "paused card must expose Continue")
        self.assertTrue(row["recovered"])
        self.reject = None
        self.window.extraction_workspace.dispatch("start", {"ids": [row["id"]]})
        self.wait_idle()
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertEqual(self.window.current[0].id, task.id)
        self.assertEqual(self.control.show(task.id).state.value, "running")
