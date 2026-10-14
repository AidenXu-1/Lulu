from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent

from src.config import AppConfig
from src.core import library
from src.core.task import Task, TaskGroup, TaskGroupMember, TaskState
from src.core.task_queue_store import TaskQueueStore
from src.ui.main_window import MainWindow


class TaskQueueUiRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "TMPDIR": str(self.root / "tmp"),
            },
        )
        self.env.start()
        (self.root / "tmp").mkdir()
        self.config_load = patch.object(AppConfig, "load", return_value=AppConfig())
        self.config_save = patch.object(AppConfig, "save", return_value=True)
        self.config_load.start()
        self.config_save.start()
        self.old_library_dir = library.LIBRARY_DIR
        library.LIBRARY_DIR = self.root / "library"
        self.windows: list[MainWindow] = []
        self.path = self.root / "app-data" / "task-queue" / "pending.json"

    def tearDown(self):
        for window in self.windows:
            timer = getattr(window, "_runtime_preferences_timer", None)
            if timer is not None:
                timer.stop()
            if window.library_page is not None:
                self.assertTrue(window.library_page.shutdown())
            window.extraction_workspace.shutdown()
            window.hide()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        library.LIBRARY_DIR = self.old_library_dir
        self.config_save.stop()
        self.config_load.stop()
        self.env.stop()
        self.temp.cleanup()

    def _window(self) -> MainWindow:
        window = MainWindow(task_queue_store=TaskQueueStore(self.path))
        window._toast = lambda _message: None
        self.windows.append(window)
        return window

    def test_pending_and_interrupted_tasks_restore_without_starting_worker(self):
        pending = Task(1, "待处理", "/tmp/pending.wav", "file")
        queued = Task(2, "排队任务", "/tmp/queued.wav", "file", state=TaskState.QUEUED)
        running = Task(
            3,
            "组内运行任务",
            "https://example.invalid/work",
            "link",
            state=TaskState.RUNNING,
            stable_key="douyin:3",
        )
        group = TaskGroup(
            7,
            "批量转录",
            [
                TaskGroupMember(
                    "douyin:3",
                    running.title,
                    running.source,
                    "link",
                    task_id=3,
                    attempt_task_ids=[3],
                )
            ],
        )
        TaskQueueStore(self.path).save([pending, queued, running], [group])

        window = self._window()

        self.assertIsNone(window.current)
        self.assertEqual([task.state for task in window.tasks], [TaskState.PENDING] * 3)
        self.assertEqual(window.tasks[0].message, "待转录")
        self.assertEqual(window.tasks[1].message, "上次未完成，待继续")
        self.assertEqual(window.tasks[2].message, "上次未完成，待继续")
        self.assertIn(7, {group.id for group in window.task_groups})
        self.assertEqual(window.task_groups[0].members[0].task_id, 3)
        self.assertEqual(window._next_id, 4)
        self.assertEqual(window._next_group_id, 8)

        normalized = TaskQueueStore(self.path).load()
        self.assertEqual([task.state for task in normalized.tasks], [TaskState.PENDING] * 3)

    def test_done_task_remains_in_session_but_not_next_launch(self):
        window = self._window()
        task = window._add_task("/tmp/finished.wav", "file", "已完成任务")
        task.state = TaskState.DONE
        task.message = "已完成"
        window._persist_task_queue()

        self.assertEqual(len(window.tasks), 1)
        self.assertEqual(TaskQueueStore(self.path).load().tasks, ())

        reopened = self._window()
        self.assertEqual(reopened.tasks, [])
        self.assertEqual(reopened.extraction_workspace.rows(), [])

    def test_source_default_does_not_create_real_recovery_store(self):
        window = MainWindow()
        self.windows.append(window)
        self.assertIsNone(window._task_queue_store)
        window._add_task("/tmp/source-test.wav", "file", "源码测试")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
