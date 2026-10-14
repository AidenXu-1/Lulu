"""GUI and CLI projections over one authoritative Core job ledger."""

from __future__ import annotations

import os
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.agent_cli.main import main as cli_main
from src.config import AppConfig
from src.core.auth_guard import UnifiedLicenseAuthGuard
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.task import Task, TaskState, derive_collection_item_runtime
from src.ui.main_window import MainWindow
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments


class _FreshCachedLicense:
    def cached_status_projection(self):
        return SimpleNamespace(allowed=True, code="cached_allowed")


def _runtime(control: JobControlService, ids=()):
    from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
    from src.core.transcription_job_application import LocalTranscriptionJobApplication

    iterator = iter(ids)
    application = LocalTranscriptionJobApplication(
        runtime_verification={
            "profile": "trusted_test",
            "verified": True,
            "signature_kind": "developer_id",
            "installation_id": "installation-001",
        },
        authorization_guard=UnifiedLicenseAuthGuard(_FreshCachedLicense()),
        model_readiness_probe=lambda _engine: True,
    )
    return LocalTranscriptionCliRuntime(
        application,
        job_control=control,
        now=lambda: "2026-09-05T02:00:01Z",
        id_factory=(lambda _kind: next(iterator)) if ids else None,
    )


def _dispatcher(runtime):
    def dispatch(command: str, arguments: dict[str, object]):
        if command == "gui.jobs.list":
            return runtime.gui_jobs_list(**arguments)
        if command == "gui.jobs.create":
            return runtime.gui_jobs_create(**arguments)
        if command == "gui.jobs.transition":
            return runtime.gui_jobs_transition(**arguments)
        raise AssertionError(command)

    return dispatch


class SharedCoreGuiTaskLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from src.core.gui_task_recovery import GuiTaskRecoveryStore
        self.recovery_directory = TemporaryDirectory(prefix="lulu-ledger-recovery-test-")
        self.addCleanup(self.recovery_directory.cleanup)
        self.recovery_patch = patch.object(
            GuiTaskRecoveryStore, "production",
            return_value=GuiTaskRecoveryStore(Path(self.recovery_directory.name) / "unsynced.json"),
        )
        self.recovery_patch.start()
        self.addCleanup(self.recovery_patch.stop)

    def wait_for_connection(self, window) -> None:
        from PySide6.QtTest import QTest
        import time
        deadline = time.monotonic() + 2
        while window._task_queue_startup_pending and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(window._task_queue_startup_pending)
        worker = getattr(window, "_task_queue_startup_worker", None)
        if worker is not None: self.assertTrue(worker.wait(1000))

    def test_core_job_is_visible_in_real_gui_from_the_same_ledger(self) -> None:
        with TemporaryDirectory(prefix="lulu-shared-core-gui-") as directory:
            root = Path(directory).resolve()
            ledger = JobLedgerStore(root / "Lulu" / "jobs", FakeIntegrityAnchorStore())
            ledger.create_job(**_create_arguments())
            dispatch = _dispatcher(_runtime(JobControlService(ledger)))

            with (
                patch.object(AppConfig, "load", return_value=AppConfig()),
                patch.object(AppConfig, "save", return_value=True),
                patch.object(sys, "frozen", True, create=True),
                patch(
                    "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
                    return_value=dispatch,
                ),
            ):
                window = MainWindow()
                self.wait_for_connection(window)
            try:
                self.app.processEvents()
                self.assertEqual([task.id for task in window.tasks], ["job-001"])
                self.assertEqual(window.tasks[0].title, "转写任务")
                self.assertIsInstance(window._task_queue_store, CoreTaskQueueStore)
                self.assertTrue(window.tasks[0]._core_view_only)
                self.assertTrue(window._persist_task_queue())
                self.assertEqual(ledger.get_job("job-001").job_revision, 1)
                with patch.object(window, "_pump") as pump:
                    window._start_all()
                self.assertEqual(window.tasks[0].state.value, "pending")
                pump.assert_called_once_with()
            finally:
                window.deleteLater()
                self.app.processEvents()

    def test_gui_created_task_is_queryable_through_the_real_cli(self) -> None:
        with TemporaryDirectory(prefix="lulu-gui-core-cli-") as directory:
            root = Path(directory).resolve()
            ledger = JobLedgerStore(root / "Lulu" / "jobs", FakeIntegrityAnchorStore())
            control = JobControlService(ledger)
            runtime = _runtime(
                control,
                ("gui-job-001", "gui-attempt-001", "gui-lineage-001"),
            )
            queue = CoreTaskQueueStore(_dispatcher(runtime))
            with (
                patch.object(AppConfig, "load", return_value=AppConfig()),
                patch.object(AppConfig, "save", return_value=True),
            ):
                window = MainWindow(task_queue_store=queue)
            try:
                task = window._add_task("/tmp/gui.wav", "file", "GUI 新任务")
                self.assertEqual(task.id, "gui-job-001")
                with patch.object(window, "_pump"):
                    window._start_task(task.id)

                stdout = StringIO()
                stderr = StringIO()
                code = cli_main(
                    ["jobs", "list", "--output", "json"],
                    stdout=stdout,
                    stderr=stderr,
                    product_identity={"adapter_id": "agent_cli"},
                    local_transcription_runtime=runtime,
                )

                self.assertEqual((code, stderr.getvalue()), (0, ""))
                jobs = json.loads(stdout.getvalue())["result"]["jobs"]
                self.assertEqual(
                    [(item["job_id"], item["state"]) for item in jobs],
                    [("gui-job-001", "queued")],
                )
            finally:
                window.deleteLater()
                self.app.processEvents()

    def test_unreachable_core_is_visible_and_never_falls_back_to_legacy_queue(self) -> None:
        with TemporaryDirectory(prefix="lulu-gui-core-unreachable-") as directory:
            home = Path(directory).resolve()
            messages: list[str] = []
            with (
                patch.dict(os.environ, {"HOME": str(home)}, clear=False),
                patch.object(AppConfig, "load", return_value=AppConfig()),
                patch.object(AppConfig, "save", return_value=True),
                patch.object(sys, "frozen", True, create=True),
                patch(
                    "src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher",
                    side_effect=RuntimeError("CORE_UNREACHABLE"),
                ),
            ):
                window = MainWindow()
                window._toast = messages.append
                self.wait_for_connection(window)
            try:
                window._toast = messages.append
                self.app.processEvents()
                self.assertIn("任务服务暂时不可用，原记录已保留", messages)
                self.assertEqual(window.tasks, [])
                self.assertFalse(
                    (home / "Library/Application Support/Lulu/task-queue/pending.json").exists()
                )
            finally:
                window.deleteLater()
                self.app.processEvents()

    def test_two_clients_enter_one_core_without_a_stale_ledger_writer(self) -> None:
        with TemporaryDirectory(prefix="lulu-one-core-concurrency-") as directory:
            root = Path(directory).resolve()
            ledger = JobLedgerStore(root / "Lulu" / "jobs", FakeIntegrityAnchorStore())
            control = JobControlService(ledger)
            runtime = _runtime(
                control,
                (
                    "gui-job-a", "gui-attempt-a", "gui-lineage-a",
                    "gui-job-b", "gui-attempt-b", "gui-lineage-b",
                ),
            )
            dispatch = _dispatcher(runtime)
            barrier = Barrier(3)
            failures: list[Exception] = []

            def create(title: str) -> None:
                try:
                    barrier.wait()
                    CoreTaskQueueStore(dispatch).register(
                        Task(1, title, f"/tmp/{title}.wav", "file")
                    )
                except Exception as exc:  # captured for the test thread
                    failures.append(exc)

            threads = [Thread(target=create, args=(name,)) for name in ("a", "b")]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            jobs = control.list()
            self.assertEqual(len(jobs), 2)
            self.assertEqual({job.created_by for job in jobs}, {"gui"})

    def test_malformed_core_request_is_rejected_before_runtime_construction(self) -> None:
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher

        calls: list[str] = []
        dispatcher = CoreCommandDispatcher(
            product_identity={"adapter_id": "agent_core"},
            resource_root=Path("/Applications/Lulu.app/Contents/Resources"),
            runtime_factory=lambda: calls.append("runtime") or object(),
        )

        invalid_requests = (
            {"command": "gui.jobs.unknown", "arguments": {}},
            {"command": "gui.jobs.list", "arguments": {"extra": True}},
            {"command": "gui.jobs.create", "arguments": {"task": "not-a-map"}},
            {
                "command": "gui.jobs.transition",
                "arguments": {
                    "job_id": "job-1",
                    "expected_revision": 0,
                    "state": "queued",
                    "result_ref": None,
                },
            },
            {"command": "argv", "arguments": {"argv": ["doctor"], "extra": True}},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED"):
                    dispatcher(request)
        self.assertEqual(calls, [])

    def test_core_string_ids_remain_valid_in_collection_runtime_projection(self) -> None:
        snapshots = derive_collection_item_runtime(
            [
                Task(
                    "gui-job-001",
                    "采集任务",
                    "fixture://one",
                    "link",
                    state=TaskState.QUEUED,
                    stable_key="work:one",
                )
            ]
        )
        self.assertEqual(snapshots["work:one"].transcript, "queued")


if __name__ == "__main__":
    unittest.main()
