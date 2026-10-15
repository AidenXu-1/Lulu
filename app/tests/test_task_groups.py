from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from src.core.collect.models import CollectedItem
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.collect.repository import CollectionRepository
from src.core.models import Transcript
from src.core.task import (
    Task,
    TaskExecutionPolicy,
    TaskGroup,
    TaskGroupCandidate,
    TaskGroupMember,
    TaskState,
    derive_collection_item_runtime,
    derive_task_group,
)
from src.ui import theme as T
from src.ui.control_foundation import PaperTextureFrame
from src.ui.main_window import MainWindow
from src.ui.worker import TranscribeWorker


class _Signal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self._callbacks):
            callback(*args)


class _FakeWorker:
    instances = []
    active = 0
    max_active = 0

    def __init__(self, source, *_args, **_kwargs):
        self.source = source
        self.progress = _Signal()
        self.finished_ok = _Signal()
        self.failed = _Signal()
        self.cancelled = _Signal()
        self.finished = _Signal()
        self.cancel_requested = False
        self.started = False
        self.deleted_later = False
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.active = 0
        cls.max_active = 0

    def start(self):
        self.started = True
        self.__class__.active += 1
        self.__class__.max_active = max(
            self.__class__.max_active,
            self.__class__.active,
        )

    def request_cancel(self):
        self.cancel_requested = True

    def deleteLater(self):  # noqa: N802
        self.deleted_later = True

    def finish_failed(self, message="fixture failure"):
        self.failed.emit(message)
        self.finish_thread()

    def finish_cancelled(self):
        self.cancelled.emit()
        self.finish_thread()

    def finish_thread(self):
        self.__class__.active -= 1
        self.finished.emit()


def _candidate(index: int, *, skipped_reason: str = "") -> TaskGroupCandidate:
    return TaskGroupCandidate(
        stable_key=f"douyin:{index}",
        title=f"作品 {index}",
        source="" if skipped_reason else f"https://example.invalid/{index}",
        skipped_reason=skipped_reason,
    )


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [
            int(color[index:index + 2], 16) / 255.0
            for index in (1, 3, 5)
        ]
        linear = [
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted(
        (luminance(foreground), luminance(background)),
        reverse=True,
    )
    return (first + 0.05) / (second + 0.05)


class TaskGroupRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "HOME": self.home.name,
                "XDG_CONFIG_HOME": os.path.join(self.home.name, "config"),
                "XDG_CACHE_HOME": os.path.join(self.home.name, "cache"),
                "TMPDIR": os.path.join(self.home.name, "tmp"),
            },
        )
        self.env.start()
        os.makedirs(os.environ["TMPDIR"], exist_ok=True)
        self.folder_patch = patch(
            "src.core.library.ensure_folder",
            return_value="fixture-folder",
        )
        self.folder_patch.start()
        self.notify_patch = patch.object(
            MainWindow,
            "_notify_task_failed",
            autospec=True,
        )
        self.notify_patch.start()
        self.runtime_patch = patch.object(
            MainWindow,
            "_sync_runtime_preferences",
            autospec=True,
        )
        self.runtime_patch.start()
        self.network_patches = [
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
            patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")),
        ]
        for guard in self.network_patches:
            guard.start()
        self.isolation = ExitStack()
        from src.config import AppConfig
        from src.core import library
        root = Path(self.home.name)
        cfg = AppConfig()
        cfg.media_dir = str(root / "media")
        cfg.recording_dir = str(root / "recordings")
        for guard in (patch.object(AppConfig, "load", return_value=cfg),
                      patch.object(AppConfig, "save", return_value=True),
                      patch("src.core.models_manager.app_data_dir", return_value=root / "data"),
                      patch("src.core.models_manager.is_ready", return_value=False),
                      patch.object(library, "LIBRARY_DIR", root / "library"),
                      patch.object(library, "FOLDERS_FILE", root / "library/folders.json"),
                      patch.object(MainWindow, "_prepare_library")):
            self.isolation.enter_context(guard)
        self.window = MainWindow(task_queue_store=None)
        self.window._switch_page = lambda _index: None
        self.window._toast = lambda _message: None
        _FakeWorker.reset()

    def tearDown(self):
        timer = getattr(self.window, "_runtime_preferences_timer", None)
        if timer is not None:
            timer.stop()
        self.window.extraction_workspace.shutdown()
        self.window.deleteLater()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        for guard in reversed(self.network_patches):
            guard.stop()
        self.runtime_patch.stop()
        self.notify_patch.stop()
        self.folder_patch.stop()
        self.isolation.close()
        self.env.stop()
        self.home.cleanup()

    def _make_group(self, count=3, *, skipped=(), start=0):
        candidates = [
            _candidate(
                start + index,
                skipped_reason=("图文作品无音频" if index in skipped else ""),
            )
            for index in range(count)
        ]
        self.window._collect_transcribe_group(candidates, "20260726 测试作者")
        return self.window.task_groups[-1]


    def test_delete_idle_group_cleans_group_tasks_cards_and_pause_state(self):
        group = self._make_group(3)
        member_ids = {member.task_id for member in group.members}
        self.window._paused_task_group_ids.add(group.id)

        self.window._delete_task_group(group.id)

        self.assertNotIn(group, self.window.task_groups)
        self.assertNotIn(group, self.window.task_groups)
        self.assertNotIn(group.id, self.window._paused_task_group_ids)
        self.assertNotIn(group.id, self.window._deleting_task_group_ids)
        self.assertTrue(member_ids.isdisjoint(task.id for task in self.window.tasks))
        self.assertTrue(
            all(task.id not in member_ids for task in self.window.tasks)
        )

    def test_delete_running_group_waits_for_finished_before_cleanup(self):
        group = self._make_group(3)
        member_ids = {member.task_id for member in group.members}

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]
            self.window._delete_task_group(group.id)

            self.assertIn(group, self.window.task_groups)
            self.assertIn(group.id, self.window._deleting_task_group_ids)
            self.assertIs(self.window.current[1], worker)
            self.assertTrue(worker.cancel_requested)
            self.assertFalse(worker.deleted_later)
            self.assertEqual(len(_FakeWorker.instances), 1)

            worker.cancelled.emit()
            self.assertIn(group, self.window.task_groups)
            self.assertIs(self.window.current[1], worker)
            self.assertFalse(worker.deleted_later)

            worker.finish_thread()

        self.assertIsNone(self.window.current)
        self.assertTrue(worker.deleted_later)
        self.assertNotIn(group, self.window.task_groups)
        self.assertNotIn(group, self.window.task_groups)
        self.assertTrue(member_ids.isdisjoint(task.id for task in self.window.tasks))
        self.assertTrue(
            all(task.id not in member_ids for task in self.window.tasks)
        )
        self.assertEqual(len(_FakeWorker.instances), 1)
        self.assertEqual(_FakeWorker.max_active, 1)


    def test_real_qthread_group_delete_waits_until_worker_has_finished(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_transcribe(source, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.06)
                return Transcript(text=f"迟到结果：{source}")
            finally:
                with lock:
                    active -= 1

        group = self._make_group(2)
        with patch("src.ui.worker.transcribe_source", side_effect=fake_transcribe):
            self.window._start_all()
            worker = self.window.current[1]
            finished_events = []
            worker.finished.connect(lambda: finished_events.append("finished"))
            self.window._delete_task_group(group.id)
            self.assertTrue(worker.isRunning())
            self.assertIn(group.id, self.window._deleting_task_group_ids)
            self.assertIn(group, self.window.task_groups)

            deadline = time.monotonic() + 3.0
            while self.window.current is not None and time.monotonic() < deadline:
                QTest.qWait(10)
                self.app.processEvents()

        self.assertIsNone(self.window.current)
        self.assertEqual(finished_events, ["finished"])
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)
        self.assertNotIn(group, self.window.task_groups)
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.extraction_workspace.rows(), [])


if __name__ == "__main__":
    unittest.main()
