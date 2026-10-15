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
