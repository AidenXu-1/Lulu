"""Submission failure/retry invariants against the real temporary Core ledger."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.task import Task, TaskState
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from tests.test_shared_core_gui_task_ledger import _runtime, _dispatcher
from tests.test_frontend_functional_repair import FrontendFunctionalRepairTests
from PySide6.QtCore import QTimer, QEventLoop
from PySide6.QtTest import QTest
from unittest.mock import patch
import time
from types import SimpleNamespace


class GuiSubmissionRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = JobLedgerStore(Path(self.tmp.name).resolve() / "jobs", anchor_store=FakeIntegrityAnchorStore())
        self.control = JobControlService(self.ledger)
        self.runtime = _runtime(self.control, [f"id-{i}" for i in range(100)])
        self.store = CoreTaskQueueStore(_dispatcher(self.runtime))

    def test_retry_failed_and_cancelled_creates_prepared_attempt_same_job(self):
        for terminal in (TaskState.FAILED, TaskState.CANCELLED):
            task = self.store.register(Task(id=1, title="任务", source="file.wav", kind="file"))
            original_id = task.id
            task.state = TaskState.QUEUED
            self.store.save([task], [])
            task.state = TaskState.RUNNING
            self.store.save([task], [])
            task.state = terminal
            self.store.save([task], [])
            task.state = TaskState.PENDING
            self.store.save([task], [])
            job = self.control.show(original_id)
            self.assertEqual(job.state.value, "prepared")
            self.assertEqual(len(job.attempts), 2)
            self.assertEqual(job.attempts[0].state.value, terminal.value)
            task.state = TaskState.QUEUED
            self.store.save([task], [])
            task.state = TaskState.RUNNING
            self.store.save([task], [])
            self.assertEqual(self.control.show(original_id).state.value, "running")

    def test_request_identity_prevents_duplicate_after_lost_response(self):
        request_id = "gui-job-" + "a" * 32
        payload = {"title": "任务", "source": "file.wav", "kind": "file", "folder_id": None, "stable_key": "", "execution_policy": None}
        first = self.runtime.gui_jobs_create(payload, request_id=request_id)
        again = self.runtime.gui_jobs_create(payload, request_id=request_id)
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertEqual(len(self.control.list()), 1)

    def test_failure_reason_survives_reopen_and_is_in_the_job_receipt(self):
        task = self.store.register(Task(1, "失败样本", "sample.wav", "file"))
        for state in (TaskState.QUEUED, TaskState.RUNNING):
            task.state = state
            self.store.save([task], [])
        task.state, task.error = TaskState.FAILED, "平台暂时拒绝匿名访问（HTTP 403），请稍后重试。"
        self.store.save([task], [])
        restored = self.store.load().tasks[0]
        self.assertEqual(restored.error, task.error)
        self.assertEqual(self.runtime.jobs_receipt(task.id)["partial_failures"][0]["retry_hint"], task.error)

    def test_batch_failure_retains_only_closed_diagnostic_cause(self):
        for reply, expected in (('JOB_CORE_ACCESS_INVALID', 'JOB_CORE_ACCESS_INVALID'),
                                ('sensitive arbitrary reply', 'CORE_OPERATION_FAILED')):
            store = CoreTaskQueueStore(lambda *_: {'results': [], 'error': reply}, async_write=True)
            task = store.prepare(Task(1, 'fixture', 'fixture.wav', 'file'))
            with self.assertRaisesRegex(RuntimeError, 'CORE_TASK_BATCH_INCOMPLETE') as error:
                store.save([task], [])
            self.assertEqual(str(error.exception.__cause__), expected)

    def batch_dispatch(self, command, arguments):
        if command == "gui.jobs.batch":
            results = []
            for op in arguments["operations"]:
                results.append(self.batch_dispatch(op["command"], op["arguments"]))
            return {"results": results, "error": None}
        return getattr(self.runtime, {"gui.jobs.list": "gui_jobs_list", "gui.jobs.view": "gui_jobs_view", "gui.jobs.create": "gui_jobs_create", "gui.jobs.transition": "gui_jobs_transition"}[command])(**arguments)

    def test_reopen_restores_group_and_deleted_task_stays_hidden(self):
        from src.core.task import TaskGroup, TaskGroupMember
        store = CoreTaskQueueStore(self.batch_dispatch, async_write=True)
        tasks = [store.prepare(Task(id=i, title=f"作品{i}", source=f"file{i}.wav", kind="file")) for i in range(2)]
        group = TaskGroup(1, "批次", [TaskGroupMember("", t.title, t.source, "file", t.id) for t in tasks], expanded=True)
        store.save(tasks, [group])
        reopened = CoreTaskQueueStore(self.batch_dispatch, async_write=True)
        loaded = reopened.load()
        self.assertEqual([m.task_id for m in loaded.groups[0].members], [t.id for t in tasks])
        self.assertTrue(loaded.groups[0].expanded)
        reopened.save([], [])
        self.assertEqual(CoreTaskQueueStore(self.batch_dispatch, async_write=True).load().tasks, ())
        self.assertEqual(len(self.control.list()), 2, "CLI history remains authoritative")

    def test_dead_gui_recovers_but_live_gui_is_untouched(self):
        import os
        task = self.store.register(Task(id=1, title="中断", source="a.wav", kind="file"))
        task.state = TaskState.QUEUED
        self.store.save([task], [])
        self.runtime.gui_jobs_transition(job_id=task.id, expected_revision=task._core_job_revision,
            state="running", result_ref=None, owner_pid=os.getpid())
        self.assertEqual(self.runtime.gui_jobs_list(include_view=True)["jobs"][0]["state"], "running")
        with patch("src.core.gui_task_view.GuiTaskViewStore.owner_has_exited", return_value=True):
            snapshot = self.runtime.gui_jobs_list(include_view=True)
        job = snapshot["jobs"][0]
        self.assertEqual(job["state"], "needs_input")
        restored = self.runtime.gui_jobs_transition(job_id=task.id, expected_revision=job["job_revision"], state="prepared", result_ref=None)
        self.assertEqual(restored["state"], "prepared")

    def test_core_batch_reports_committed_prefix_on_failure(self):
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=Path(self.tmp.name), runtime_factory=lambda: self.runtime)
        dispatcher._runtime, dispatcher._runtime_loaded = self.runtime, True
        install = SimpleNamespace(paths=SimpleNamespace(state_root=Path(self.tmp.name)))
        payload = {"title": "有效任务", "source": "a.wav", "kind": "file", "folder_id": None, "stable_key": "", "execution_policy": None}
        ops = [{"command": "gui.jobs.create", "arguments": {"task": payload, "request_id": "gui-job-" + "b" * 32}},
               {"command": "gui.jobs.transition", "arguments": {"broken": True}}]
        with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=install), patch("src.core.agent_entry.product_transaction.assert_business_available"):
            result = dispatcher({"command": "gui.jobs.batch", "arguments": {"operations": ops}})
        self.assertEqual(len(result["results"]), 1)
        self.assertIsNotNone(result["error"])
        self.assertEqual(len(self.control.list()), 1)


class BundleVerificationCacheTests(unittest.TestCase):
    def test_deep_edit_add_delete_and_same_size_edit_invalidate(self):
        from src.core.agent_entry.verification_cache import BundleVerificationCache
        from unittest.mock import Mock
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Contents/Resources/runtime/file"
            path.parent.mkdir(parents=True)
            path.write_text("original")
            cache, verifier = BundleVerificationCache(root), Mock(return_value="verified")
            cache.verify(verifier)
            cache.verify(verifier)
            self.assertEqual(verifier.call_count, 1)
            path.write_text("modified")
            cache.verify(verifier)
            extra = path.with_name("new")
            extra.write_text("added")
            cache.verify(verifier)
            extra.unlink()
            cache.verify(verifier)
            self.assertEqual(verifier.call_count, 4)

    def test_verification_failure_and_concurrent_changes_never_cache(self):
        from src.core.agent_entry.verification_cache import BundleVerificationCache
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = BundleVerificationCache(root)
            def changing():
                (root / "changed").write_text("mutation")
            with self.assertRaisesRegex(RuntimeError, "GENERATION_CHANGED"):
                cache.verify(changing)
            def failing():
                raise RuntimeError("failed verification")
            with self.assertRaisesRegex(RuntimeError, "failed verification"):
                cache.verify(failing)
            self.assertEqual(cache.verify(lambda: "fresh verification"), "fresh verification")


class SnapshotSerializationTests(unittest.TestCase):
    def test_batch_reuses_unchanged_records_and_reopens_identical_snapshot(self):
        import json
        from src.core.job_ledger.contracts import JobV1, JobState
        from tests.test_phase5_job_ledger import _create_arguments
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "jobs"
            anchor = FakeIntegrityAnchorStore()
            ledger = JobLedgerStore(root, anchor)
            serialize = JobV1.as_dict
            with patch.object(JobV1, "as_dict", autospec=True, side_effect=serialize) as calls:
                for index in range(40):
                    args = _create_arguments()
                    args.update(job_id=f"job-{index}", attempt_id=f"attempt-{index}",
                                lineage_id=f"lineage-{index}", idempotency_key=f"idempotency-{index}")
                    ledger.create_job(**args)
                for job in ledger.list_jobs():
                    ledger.transition(job.job_id, expected_revision=job.job_revision,
                                      new_state=JobState.QUEUED, changed_at="2026-09-07T12:00:00Z")
                self.assertLessEqual(calls.call_count, 4 * 40)
            saved = json.loads(ledger.snapshot_path.read_text())
            jobs = ledger.list_jobs()
            self.assertEqual(saved["jobs"], [job.as_dict() for job in jobs])
            self.assertEqual(saved["events"], {job.job_id: [event.as_dict() for event in ledger.events(job.job_id)] for job in jobs})
            reopened = JobLedgerStore(root, anchor)
            self.assertEqual(reopened.list_jobs(), jobs)
            self.assertEqual(reopened.head, ledger.head)


class GuiAsyncSubmissionTests(unittest.TestCase):
    setUpClass = FrontendFunctionalRepairTests.__dict__["setUpClass"]
    setUp = FrontendFunctionalRepairTests.setUp
    def tearDown(self):
        self.window._close_settling = True
        if self.window.current:
            self.window.current[1].request_cancel()
            self.window.current[1].wait(5000)
        for _ in range(5):
            worker = getattr(self.window, "_core_save_worker", None)
            if worker is not None:
                worker.wait(10000)
            self.app.processEvents()
        FrontendFunctionalRepairTests.tearDown(self)
    group = FrontendFunctionalRepairTests.group

    def test_post_upgrade_acl_saves_starts_recovers_and_reopens_without_losing_inputs(self):
        from test_historical_acl import HistoricalAclTests, OLD, NEW, UNKNOWN
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        from src.core.models import Transcript
        from src.core import library
        class Anchor(FakeIntegrityAnchorStore):
            applications = (OLD,)
            def load(self):
                HistoricalAclTests().verify(partitions=(OLD,), applications=self.applications)
                return super().load()
        anchor = Anchor()
        control = JobControlService(JobLedgerStore(self.root.resolve() / 'jobs', anchor_store=anchor))
        runtime = _runtime(control, [f'fixture-{i}' for i in range(100)])
        failures = []
        def dispatch(command, arguments):
            if command == 'gui.jobs.batch':
                return {'results': [dispatch(op['command'], op['arguments']) for op in arguments['operations']], 'error': None}
            try:
                return getattr(runtime, command.replace('.', '_'))(**arguments)
            except Exception:
                import traceback
                failures.append(traceback.format_exc())
                raise
        store = CoreTaskQueueStore(dispatch, async_write=True)
        self.window._task_queue_store = store
        self.window._task_queue_recovery = GuiTaskRecoveryStore(self.root / 'unsynced.json')
        self.window._task_queue_recovery_checked = True
        # Like the running production Core, its cached ledger was initialized
        # before the previous build was remembered in this item's ordinary ACL.
        anchor.applications = (NEW, OLD)
        started = []
        def transcribe(source, **kwargs):
            self.assertEqual(control.show(self.window.current[0].id).state.value, 'running')
            started.append(source)
            return Transcript(text='隔离原稿完整正文', segments=[])
        with patch('src.ui.worker.transcribe_source', side_effect=transcribe):
            first = store.prepare(Task(1, '原稿任务', 'isolated-first.wav', 'file'))
            self.window.tasks.append(first)
            self.window._start_task(first.id)
            self.wait_saves()
            first = self.window.tasks[0]
            self.assertEqual(first.state, TaskState.DONE, failures)
            original_body = library.load(first.library_id).text
            # A genuinely unknown identity still pauses before any processing.
            anchor.applications = (UNKNOWN, OLD)
            task = store.prepare(Task(2, '保留输入', 'isolated-retry.wav', 'file'))
            self.window.tasks.append(task)
            self.window._start_task(task.id)
            self.wait_saves()
            self.assertTrue(self.window._task_queue_store_failed)
            self.assertTrue(task._core_recovered_queue)
            self.assertEqual(len(started), 1)
            self.assertIn('isolated-retry.wav', (self.root / 'unsynced.json').read_text())
            self.assertEqual(library.load(first.library_id).text, original_body)
            anchor.applications = (NEW, OLD)
            self.window._task_queue_connection_ready(store, store.load())
            self.wait_saves()
            self.assertEqual(len(started), 1, 'Recovery must still wait for Continue')
            self.window._start_task(task.id)
            self.wait_saves()
        self.assertEqual(len(started), 2)
        runtime = _runtime(JobControlService(JobLedgerStore(self.root.resolve() / 'jobs', anchor_store=anchor)),
                           [f'reopened-{i}' for i in range(100)])
        reopened = CoreTaskQueueStore(dispatch, async_write=True).load()
        self.assertEqual({t.id for t in reopened.tasks}, {first.id, task.id})
        self.assertTrue(all(t.state is TaskState.DONE for t in reopened.tasks))
        self.assertEqual(library.load(first.library_id).text, original_body)
        expected_bodies = {t.id: library.load(t.library_id).text for t in (first, task)}
        self.window.tasks = []
        self.window._session_hidden_completed = set()
        self.window._restore_task_queue(reopened)
        self.assertEqual({t.id for t in self.window.tasks}, {first.id, task.id})
        self.assertEqual({t.id: t.transcript.text for t in self.window.tasks}, expected_bodies)

    def wait_saves(self):
        # Use Qt's normal event loop. Repeated qWait calls can starve Python
        # workers of the GIL and manufacture minute-long storage delays.
        loop = QEventLoop()
        poll = QTimer()
        def check():
            if (getattr(self.window, "_core_save_worker", None) is None
                    and self.window.current is None and not self.window._task_queue_startup_pending):
                loop.quit()
        poll.timeout.connect(check)
        poll.start(10)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(60000)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertIsNone(getattr(self.window, "_core_save_worker", None), [(t.state.value, getattr(t, "_core_state", None)) for t in self.window.tasks])
        self.assertIsNone(self.window.current)
        self.assertFalse(self.window._task_queue_startup_pending)

    def test_slow_batch_does_not_block_ui_and_worker_waits_for_running_ack(self):
        ledger = JobLedgerStore(self.root.resolve() / "jobs", FakeIntegrityAnchorStore())
        control = JobControlService(ledger)
        runtime = _runtime(control, [f"test-{i}" for i in range(1000)])
        batches = []
        def dispatch(command, arguments):
            if command == "gui.jobs.view":
                return runtime.gui_jobs_view(**arguments)
            if command == "gui.jobs.list":
                return runtime.gui_jobs_list(**arguments)
            self.assertEqual(command, "gui.jobs.batch")
            time.sleep(0.04)
            results = []
            for op in arguments["operations"]:
                if op["command"] == "gui.jobs.create":
                    results.append(runtime.gui_jobs_create(**op["arguments"]))
                else:
                    results.append(runtime.gui_jobs_transition(**op["arguments"]))
            batches.append(len(results))
            return {"results": results, "error": None}
        self.window._task_queue_store = CoreTaskQueueStore(dispatch, async_write=True)
        beats = []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start(5)
        self.addCleanup(timer.stop)
        started = []
        def transcribe(source, **kwargs):
            current = self.window.current[0]
            self.assertEqual(control.show(current.id).state.value, "running")
            started.append(source)
            from src.core.models import Transcript
            return Transcript(text="批量结果", segments=[])
        with patch("src.ui.worker.transcribe_source", side_effect=transcribe):
            group = self.group(24)
            self.window._start_task_group(group.id)
            self.assertFalse(started)
            self.wait_saves()
        self.assertGreater(len(beats), 20)
        self.assertEqual(len(started), 24)
        self.assertEqual(batches[0], 24)
        self.assertTrue(all(t.state is TaskState.DONE for t in self.window.tasks))

    def test_failed_creation_never_starts_worker(self):
        def dispatch(*args):
            raise OSError("injected offline")
        self.window._task_queue_store = CoreTaskQueueStore(dispatch, async_write=True)
        with patch("src.ui.worker.TranscribeWorker.start") as start:
            group = self.group(24)
            self.window._start_task_group(group.id)
            self.wait_saves()
        start.assert_not_called()
        self.assertTrue(self.window._task_queue_store_failed)

    def test_initial_connection_failure_retries_real_connection_factory(self):
        ready = CoreTaskQueueStore(lambda command, arguments: {
            "jobs": [], "view": {"revision": 0, "groups": [], "hidden_ids": []},
        }, async_write=True)
        self.window._task_queue_store = CoreTaskQueueStore.unavailable()
        self.window._task_queue_initial_connection = True
        self.window._task_queue_startup_pending = True
        from src.core.agent_entry.native_transport import XpcTransportError
        with patch.object(CoreTaskQueueStore, "production", side_effect=[XpcTransportError(20)] * 3 + [ready]) as connect:
            self.window._start_task_queue_connection()
            self.wait_saves()
            self.window._task_queue_startup_worker.wait(1000)
            self.assertTrue(self.window._task_queue_store_failed)
            self.window._sync_shared_tasks()
            self.wait_saves()
        self.assertEqual(connect.call_count, 4)
        self.assertIs(self.window._task_queue_store, ready)
        self.assertFalse(self.window._task_queue_store_failed)
        self.assertFalse(self.window._task_queue_initial_connection)

    def _assert_reconnect_after_failure(self, stage, *, committed=False):
        from src.core.models import Transcript
        ledger = JobLedgerStore(self.root.resolve() / "jobs", FakeIntegrityAnchorStore())
        control = JobControlService(ledger)
        runtime = _runtime(control, [f"recover-{i}" for i in range(100)])
        fail = [True]
        def dispatch(command, arguments):
            if command == "gui.jobs.batch":
                results = []
                for op in arguments["operations"]:
                    op_stage = "create" if op["command"] == "gui.jobs.create" else op["arguments"]["state"]
                    if fail[0] and stage == op_stage:
                        if committed:
                            dispatch(op["command"], op["arguments"])
                        return {"results": results, "error": "INJECTED"}
                    results.append(dispatch(op["command"], op["arguments"]))
                return {"results": results, "error": None}
            return getattr(runtime, {"gui.jobs.create": "gui_jobs_create", "gui.jobs.transition": "gui_jobs_transition", "gui.jobs.list": "gui_jobs_list", "gui.jobs.view": "gui_jobs_view"}[command])(**arguments)
        self.window._task_queue_store = CoreTaskQueueStore(dispatch, async_write=True)
        with patch("src.ui.worker.transcribe_source", return_value=Transcript(text="成功识别并保存", segments=[])) as transcribe:
            group = self.group(2)
            self.window._start_task_group(group.id)
            self.wait_saves()
            transcribe.assert_not_called()
            self.assertTrue(self.window._task_queue_store_failed)
            fail[0] = False
            self.window._sync_shared_tasks()
            self.wait_saves()
            self.assertTrue(self.window._task_queue_store_failed)
            transcribe.assert_not_called()
            # A timer cannot replay a failed save. Explicit Continue reads the
            # actual outcome first, including a committed but lost response.
            self.window._start_task_group(group.id)
            self.wait_saves()
            self.assertFalse(self.window._task_queue_store_failed)
            if committed and stage == "running":
                self.assertTrue(any(t.state is TaskState.FAILED for t in self.window.tasks))
                self.window._retry_task_group(group.id)
                self.wait_saves()
            self.window._start_task_group(group.id)
            self.wait_saves()
            self.assertEqual(transcribe.call_count, 2)
        self.assertEqual(len(control.list()), 2)
        self.assertTrue(all(t.state is TaskState.DONE for t in self.window.tasks))

    def test_creation_failure_can_reconnect_without_duplicates(self):
        self._assert_reconnect_after_failure("create")

    def test_queue_failure_can_reconnect_before_any_worker_starts(self):
        self._assert_reconnect_after_failure("queued")

    def test_running_ack_failure_can_reconnect_before_any_worker_starts(self):
        self._assert_reconnect_after_failure("running")

    def test_committed_create_with_lost_reply_does_not_duplicate_or_start(self):
        self._assert_reconnect_after_failure("create", committed=True)

    def test_committed_queue_with_lost_reply_waits_for_explicit_continue(self):
        self._assert_reconnect_after_failure("queued", committed=True)

    def test_committed_running_with_lost_reply_settles_without_starting(self):
        self._assert_reconnect_after_failure("running", committed=True)

    def test_delete_failed_view_save_does_not_resurrect_group_on_reconnect(self):
        ledger = JobLedgerStore(self.root.resolve() / "jobs", FakeIntegrityAnchorStore())
        control = JobControlService(ledger)
        runtime = _runtime(control, [f"delete-{i}" for i in range(100)])
        fail_view = [False]
        def dispatch(command, arguments):
            if command == "gui.jobs.batch":
                return {"results": [dispatch(op["command"], op["arguments"]) for op in arguments["operations"]], "error": None}
            if command == "gui.jobs.view" and fail_view[0]:
                raise OSError("view save interrupted")
            return getattr(runtime, {"gui.jobs.create": "gui_jobs_create", "gui.jobs.transition": "gui_jobs_transition", "gui.jobs.list": "gui_jobs_list", "gui.jobs.view": "gui_jobs_view"}[command])(**arguments)
        self.window._task_queue_store = CoreTaskQueueStore(dispatch, async_write=True)
        group = self.group(2)
        self.wait_saves()
        fail_view[0] = True
        self.window._delete_task_group(group.id)
        self.wait_saves()
        self.assertTrue(self.window._task_queue_store_failed)
        fail_view[0] = False
        with patch("src.ui.worker.TranscribeWorker.start") as start:
            self.window._sync_shared_tasks()
            self.wait_saves()
            self.assertTrue(self.window._task_queue_store_failed)
            # The removal stays local until an explicit reconnection.
            self.window._task_queue_startup_pending = True
            self.window._start_task_queue_connection()
            self.wait_saves()
        start.assert_not_called()
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.task_groups, [])
        self.assertEqual(CoreTaskQueueStore(dispatch, async_write=True).load().tasks, ())
        self.assertEqual(len(control.list()), 2)
        self.assertTrue(all(job.state.value == "cancelled" for job in control.list()))

    def test_recovered_queue_waits_for_explicit_group_start(self):
        from src.core.models import Transcript
        ledger = JobLedgerStore(self.root.resolve() / "jobs", FakeIntegrityAnchorStore())
        control = JobControlService(ledger)
        runtime = _runtime(control, [f"queued-{i}" for i in range(100)])
        def dispatch(command, arguments):
            if command == "gui.jobs.batch":
                return {"results": [dispatch(op["command"], op["arguments"]) for op in arguments["operations"]], "error": None}
            return getattr(runtime, {"gui.jobs.create": "gui_jobs_create", "gui.jobs.transition": "gui_jobs_transition", "gui.jobs.list": "gui_jobs_list", "gui.jobs.view": "gui_jobs_view"}[command])(**arguments)
        store = CoreTaskQueueStore(dispatch, async_write=True)
        self.window._task_queue_store = store
        group = self.group(2)
        self.wait_saves()
        for task in self.window.tasks:
            task.state = TaskState.QUEUED
        store.save(self.window.tasks, self.window.task_groups)
        # Recreate the UI-side task objects, as on a fresh application launch.
        self.window.tasks = []
        self.window.task_groups = []
        reopened = CoreTaskQueueStore(dispatch, async_write=True)
        with patch("src.ui.worker.transcribe_source", return_value=Transcript(text="恢复后完整文稿", segments=[])) as transcribe:
            self.window._task_queue_connection_ready(reopened, reopened.load())
            self.window._pump()
            QTest.qWait(30)
            transcribe.assert_not_called()
            self.window._start_task_group(group.id)
            self.wait_saves()
            self.assertEqual(transcribe.call_count, 2)
        self.assertTrue(all(t.state is TaskState.DONE for t in self.window.tasks))
