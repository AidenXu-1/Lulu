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


if __name__ == "__main__":
    unittest.main()
