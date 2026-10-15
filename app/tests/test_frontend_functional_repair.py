"""Regression cases from the September frontend investigations; isolated data only."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core import library
from src.core.collect.models import CollectedItem
from src.core.models import Segment, Transcript
from src.core.task import TaskGroupCandidate, TaskState
from src.ui.main_window import MainWindow


class FrontendFunctionalRepairTests(unittest.TestCase):
    def wait_library(self, page):
        from PySide6.QtCore import QEventLoop, QTimer
        for _ in range(500):
            if not (page._query_worker or page._batch_worker or page._pending_refresh):
                return
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        self.fail("library work did not finish")
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(patch.object(library, "LIBRARY_DIR", self.root / "library"))
        self.stack.enter_context(patch.object(library, "FOLDERS_FILE", self.root / "library/folders.json"))
        self.stack.enter_context(patch.object(AppConfig, "load", return_value=AppConfig()))
        self.stack.enter_context(patch.object(AppConfig, "save", return_value=True))
        self.stack.enter_context(patch("src.core.models_manager.app_data_dir", return_value=self.root / "data"))
        self.stack.enter_context(patch("src.core.models_manager.is_ready", return_value=False))
        for name in ("_prepare_library", "_sync_runtime_preferences", "_notify_task_completed", "_notify_task_failed"):
            self.stack.enter_context(patch.object(MainWindow, name))
        self.window = MainWindow(task_queue_store=None)
        self.window._switch_page = lambda _index: None
        self.window._toast = lambda _message: None

    def tearDown(self):
        if self.window.current:
            self.window.current[1].request_cancel()
            self.window.current[1].wait(3000)
        self.window._runtime_preferences_timer.stop()
        self.app.processEvents()
        self.window.extraction_workspace.shutdown()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.stack.close()

    def group(self, size):
        self.window._collect_transcribe_group([
            TaskGroupCandidate(f"douyin:{i}", f"合成作品 {i}", f"https://example.invalid/{i}")
            for i in range(size)
        ], "隔离批次")
        return self.window.task_groups[-1]

    def wait_idle(self):
        deadline = time.monotonic() + 15
        while self.window.current is not None and time.monotonic() < deadline:
            QTest.qWait(2)
        self.assertIsNone(self.window.current)
