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


class TaskGroupModelTests(unittest.TestCase):
    def test_collection_item_runtime_uses_stable_key_and_never_claims_persisted_success(self):
        tasks = [
            Task(1, "排队", "fixture://1", "link", state=TaskState.QUEUED, stable_key="work:1"),
            Task(
                2,
                "下载",
                "fixture://2",
                "link",
                state=TaskState.RUNNING,
                message="正在下载音频",
                stable_key="work:2",
            ),
            Task(
                3,
                "转录",
                "fixture://3",
                "link",
                state=TaskState.RUNNING,
                message="正在转录",
                stable_key="work:3",
            ),
            Task(4, "失败", "fixture://4", "link", state=TaskState.FAILED, stable_key="work:4"),
            Task(5, "完成", "fixture://5", "link", state=TaskState.DONE, stable_key="work:5"),
            Task(6, "普通任务", "fixture://6", "link", state=TaskState.RUNNING),
        ]
        snapshots = derive_collection_item_runtime(tasks)
        self.assertEqual(
            (snapshots["work:1"].transcript, snapshots["work:1"].media),
            ("queued", "queued"),
        )
        self.assertEqual(
            (snapshots["work:2"].transcript, snapshots["work:2"].media),
            ("queued", "downloading"),
        )
        self.assertEqual(
            (snapshots["work:3"].transcript, snapshots["work:3"].media),
            ("transcribing", ""),
        )
        self.assertEqual(
            (snapshots["work:4"].transcript, snapshots["work:4"].media),
            ("failed", "failed"),
        )
        self.assertNotIn("work:5", snapshots)
        self.assertNotIn("", snapshots)

    def test_zero_one_two_and_hundreds_are_purely_derived(self):
        empty = TaskGroup(1, "空组", [])
        self.assertEqual(derive_task_group(empty, []).total, 0)

        one_task = Task(1, "一条", "fixture://1", "link")
        one = TaskGroup(
            2,
            "一条",
            [TaskGroupMember("k1", "一条", task_id=1)],
        )
        one_snapshot = derive_task_group(one, [one_task])
        self.assertEqual(one_snapshot.state, "pending")
        self.assertEqual(one_snapshot.completed, 0)
        self.assertEqual(one_snapshot.current_title, "")

        two_tasks = [
            Task(1, "成功", "fixture://1", "link", state=TaskState.DONE),
            Task(2, "失败", "fixture://2", "link", state=TaskState.FAILED),
        ]
        two = TaskGroup(
            3,
            "两条",
            [
                TaskGroupMember("k1", "成功", task_id=1),
                TaskGroupMember("k2", "失败", task_id=2),
            ],
        )
        two_snapshot = derive_task_group(two, two_tasks)
        self.assertEqual(two_snapshot.state, "partial")
        self.assertEqual((two_snapshot.succeeded, two_snapshot.failed), (1, 1))

        many_tasks = [
            Task(
                index,
                f"作品 {index}",
                f"fixture://{index}",
                "link",
                state=TaskState.DONE,
            )
            for index in range(1, 301)
        ]
        many = TaskGroup(
            4,
            "三百条",
            [
                TaskGroupMember(
                    f"k{index}",
                    f"作品 {index}",
                    task_id=index,
                )
                for index in range(1, 301)
            ],
        )
        snapshot = derive_task_group(many, many_tasks)
        self.assertEqual(snapshot.total, 300)
        self.assertEqual(snapshot.completed, 300)
        self.assertEqual(snapshot.state, "done")

    def test_skipped_and_current_item_come_from_member_and_real_task(self):
        tasks = [
            Task(1, "排队作品", "fixture://1", "link", state=TaskState.QUEUED),
            Task(2, "正在转录", "fixture://2", "link", state=TaskState.RUNNING),
        ]
        group = TaskGroup(
            1,
            "混合组",
            [
                TaskGroupMember("a", "排队作品", task_id=1),
                TaskGroupMember("b", "正在转录", task_id=2),
                TaskGroupMember("c", "图文", skipped_reason="图文作品无音频"),
            ],
        )
        snapshot = derive_task_group(group, tasks)
        self.assertEqual(snapshot.current_title, "正在转录")
        self.assertEqual(snapshot.skipped, 1)
        self.assertEqual(snapshot.pending, 1)
        self.assertEqual(snapshot.queued, 1)
        self.assertEqual(snapshot.running, 1)
        for task in tasks:
            task.state = TaskState.DONE
        self.assertEqual(derive_task_group(group, tasks).state, "partial")


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

    def test_one_item_preserves_single_task_and_two_items_create_one_group(self):
        self.window._collect_transcribe_group([_candidate(1)], None)
        self.assertEqual(len(self.window.tasks), 1)
        self.assertEqual(self.window.task_groups, [])
        self.assertEqual(self.window.tasks[0].stable_key, "douyin:1")

        self.window._collect_transcribe_group([_candidate(2), _candidate(3)], "两条")
        self.assertEqual(len(self.window.task_groups), 1)
        group = self.window.task_groups[0]
        self.assertEqual(len(group.members), 2)
        self.assertEqual(len({member.task_id for member in group.members}), 2)

    def test_unstarted_group_and_members_never_claim_to_be_running(self):
        group = self._make_group(3)
        snapshot = derive_task_group(group, self.window.tasks)
        self.assertEqual(snapshot.state, "pending")
        self.assertEqual(snapshot.running, 0)
        self.assertEqual(snapshot.current_title, "")
        rows = self.window.extraction_workspace.rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["state"] == "waiting" for row in rows))
        self.assertTrue(all(row["group"] == str(group.id) for row in rows))

    def test_group_policy_is_frozen_and_retry_keeps_original_snapshot(self):
        # 本用例验证页面偏好变化不会改变批量任务的纯转录策略，
        # 也不应依赖开发机当前持久化的真实用户偏好。
        self.window.cfg.link_media_mode = "audio"
        self.window.cfg.keep_media = False
        self.window.cfg.collect_cover = False
        group = self._make_group(2)
        original_tasks = [
            self.window._task(member.task_id) for member in group.members
        ]
        original_policies = [task.execution_policy for task in original_tasks]
        self.assertTrue(all(policy.mode == "transcription_only" for policy in original_policies))
        self.assertTrue(all(policy.material_snapshot.download_mode == "audio" for policy in original_policies))
        self.assertTrue(all(not policy.material_snapshot.save_media for policy in original_policies))

        self.window.cfg.link_media_mode = "video"
        self.window.cfg.keep_media = True
        self.window.cfg.collect_cover = True
        original_tasks[0].state = TaskState.FAILED
        self.window._retry_task_group(group.id)
        self.assertIs(original_tasks[0].execution_policy, original_policies[0])

        new_group = self._make_group(2)
        for member in new_group.members:
            policy = self.window._task(member.task_id).execution_policy
            self.assertEqual(policy.material_snapshot.download_mode, "audio")
            self.assertFalse(policy.material_snapshot.save_media)
            self.assertFalse(policy.material_snapshot.save_cover)


    def test_batch_transcript_cas_changes_only_transcript_column(self):
        with tempfile.TemporaryDirectory() as raw:
            repository = CollectionRepository(Path(raw) / "records.json")
            original = CollectedItem(
                platform="douyin",
                work_id="1",
                aweme_id="1",
                title="kept",
                media_local_path="douyin/kept.m4a",
                media_status="downloaded",
                cover_local_path="douyin/kept.jpg",
                cover_status="downloaded",
                transcript_status="not_started",
            )
            repository.upsert([original])
            self.window.collection_page = SimpleNamespace(
                _records_repo=repository
            )
            task = Task(
                999,
                "作品",
                "https://example.invalid/1",
                "link",
                stable_key=original.stable_key,
                execution_policy=TaskExecutionPolicy.transcription_only(
                    origin="homepage_batch_transcribe",
                    stable_key=original.stable_key,
                    snapshot=MaterialPreferenceSnapshot("video", True, True),
                ),
            )
            self.window._persist_collection_transcript_status(task, "failed")
            saved = repository.load().items[0]
            self.assertEqual(saved.transcript_status, "failed")
            self.assertEqual(saved.media_local_path, original.media_local_path)
            self.assertEqual(saved.media_status, original.media_status)
            self.assertEqual(saved.cover_local_path, original.cover_local_path)
            self.assertEqual(saved.cover_status, original.cover_status)
            self.assertEqual(saved.title, "kept")


    def test_paused_last_running_member_shows_continue_and_does_not_revive_it(self):
        group = self._make_group(2)
        completed, last_running = [
            self.window._task(member.task_id) for member in group.members
        ]
        completed.state = TaskState.DONE

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_task_group(group.id)
            worker = _FakeWorker.instances[0]
            self.assertIs(self.window.current[0], last_running)

            self.window._toggle_task_group_pause(group.id)

            snapshot = derive_task_group(group, self.window.tasks)
            self.assertEqual(snapshot.pending, 0)
            self.assertEqual(snapshot.running, 1)
            self.assertIn(group.id, self.window._paused_task_group_ids)

            self.window._toggle_task_group_pause(group.id)

            self.assertNotIn(group.id, self.window._paused_task_group_ids)
            self.assertIs(self.window.current[1], worker)
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(last_running.state, TaskState.RUNNING)
            self.assertEqual(last_running.message, "正在取消…")

            worker.finish_cancelled()

        self.assertIsNone(self.window.current)
        self.assertEqual(len(_FakeWorker.instances), 1)
        self.assertEqual(_FakeWorker.max_active, 1)
        self.assertEqual(completed.state, TaskState.DONE)
        self.assertEqual(last_running.state, TaskState.CANCELLED)

    def test_paused_mixed_cancelled_and_pending_continues_from_pending_member(self):
        group = self._make_group(3)
        cancelled, pending, completed = [
            self.window._task(member.task_id) for member in group.members
        ]
        cancelled.state = TaskState.CANCELLED
        pending.state = TaskState.PENDING
        completed.state = TaskState.DONE
        self.window._paused_task_group_ids.add(group.id)
        self.window._publish_extraction_state()

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._toggle_task_group_pause(group.id)

            self.assertNotIn(group.id, self.window._paused_task_group_ids)
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            self.assertEqual(pending.state, TaskState.RUNNING)
            self.assertEqual(completed.state, TaskState.DONE)
            self.assertIs(self.window.current[0], pending)
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_start_group_queues_only_its_members_and_starts_serially(self):
        untouched_group = self._make_group(2)
        started_group = self._make_group(3, start=100)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_task_group(started_group.id)

            untouched_tasks = [
                self.window._task(member.task_id)
                for member in untouched_group.members
            ]
            started_tasks = [
                self.window._task(member.task_id)
                for member in started_group.members
            ]
            self.assertTrue(
                all(task.state == TaskState.PENDING for task in untouched_tasks)
            )
            self.assertEqual(
                [task.state for task in started_tasks],
                [
                    TaskState.RUNNING,
                    TaskState.QUEUED,
                    TaskState.QUEUED,
                ],
            )
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(_FakeWorker.max_active, 1)
            self.assertIs(self.window.current[0], started_tasks[0])

            first_worker = _FakeWorker.instances[0]
            first_worker.failed.emit("fixture failure")
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertIs(self.window.current[1], first_worker)
            first_worker.finish_thread()

            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertIs(self.window.current[0], started_tasks[1])
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_multiple_groups_keep_global_task_order_and_serial_worker(self):
        first = self._make_group(2)
        second = self._make_group(2, start=100)
        self.assertEqual([group.id for group in self.window.task_groups], [1, 2])
        expected_sources = [
            member.source for group in (first, second) for member in group.members
        ]
        self.assertEqual([task.source for task in self.window.tasks], expected_sources)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(_FakeWorker.max_active, 1)
            _FakeWorker.instances[0].finish_failed()
            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertEqual(_FakeWorker.max_active, 1)
            _FakeWorker.instances[1].finish_failed()
            self.assertEqual(len(_FakeWorker.instances), 3)
            self.assertEqual(_FakeWorker.max_active, 1)
        self.assertEqual(self.window.tasks[0].state, TaskState.FAILED)
        self.assertEqual(self.window.tasks[1].state, TaskState.FAILED)
        self.assertEqual(self.window.tasks[2].state, TaskState.RUNNING)

    def test_result_signal_does_not_advance_until_worker_thread_finishes(self):
        group = self._make_group(2)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            first = _FakeWorker.instances[0]
            first.failed.emit("fixture failure")

            self.assertEqual(
                self.window._task(group.members[0].task_id).state,
                TaskState.FAILED,
            )
            self.assertIs(self.window.current[1], first)
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(_FakeWorker.active, 1)

            first.finish_thread()

            self.assertTrue(first.deleted_later)
            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertIs(self.window.current[1], _FakeWorker.instances[1])
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_cancel_immediately_shows_cancelling_and_waits_for_thread_finish(self):
        task = self.window._add_task("fixture://single", "link", "单条任务")

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]

            self.window._cancel_task(task.id)

            self.assertEqual(task.state, TaskState.RUNNING)
            self.assertEqual(task.message, "正在取消…")
            self.assertTrue(worker.cancel_requested)
            self.assertIs(self.window.current[1], worker)

            self.window._on_progress(task.id, "迟到的模型进度", 0.8)
            self.assertEqual(task.message, "正在取消…")

            worker.cancelled.emit()
            self.assertEqual(task.state, TaskState.RUNNING)
            self.assertEqual(task.message, "正在取消…")
            self.assertIs(self.window.current[1], worker)

            worker.finish_thread()
            self.assertIsNone(self.window.current)
            self.assertEqual(task.state, TaskState.CANCELLED)
            self.assertTrue(worker.deleted_later)

    def test_cancel_member_pauses_group_until_user_resumes(self):
        group = self._make_group(3)
        first, second, third = [
            self.window._task(member.task_id) for member in group.members
        ]

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]

            self.window._cancel_task(first.id)

            self.assertIn(group.id, self.window._paused_task_group_ids)
            self.assertEqual(first.message, "正在取消…")
            self.assertTrue(worker.cancel_requested)

            worker.finish_cancelled()
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertEqual(first.state, TaskState.CANCELLED)
            self.assertEqual(second.state, TaskState.QUEUED)
            self.assertEqual(third.state, TaskState.QUEUED)
            self.assertIsNone(self.window.current)

            self.window._toggle_task_group_pause(group.id)

            self.assertNotIn(group.id, self.window._paused_task_group_ids)
            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertIs(self.window.current[0], second)
            self.assertEqual(first.state, TaskState.CANCELLED)
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_paused_group_does_not_block_unrelated_group(self):
        first_group = self._make_group(2)
        second_group = self._make_group(2, start=100)
        first_task = self.window._task(first_group.members[0].task_id)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            first_worker = _FakeWorker.instances[0]
            self.window._cancel_task(first_task.id)
            first_worker.finish_cancelled()

            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertEqual(
                self.window.current[0].id,
                second_group.members[0].task_id,
            )
            self.assertIn(first_group.id, self.window._paused_task_group_ids)
            self.assertEqual(
                self.window._task(first_group.members[1].task_id).state,
                TaskState.QUEUED,
            )
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_group_pause_discards_interrupted_member_and_resumes_at_next(self):
        group = self._make_group(3)
        first, second, third = [
            self.window._task(member.task_id) for member in group.members
        ]
        first.state = TaskState.DONE

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]
            self.assertIs(self.window.current[0], second)

            self.window._toggle_task_group_pause(group.id)
            self.assertTrue(worker.cancel_requested)
            worker.finish_cancelled()

            self.assertIsNone(self.window.current)
            self.assertEqual(second.state, TaskState.CANCELLED)
            self.assertEqual(third.state, TaskState.QUEUED)

            self.window._toggle_task_group_pause(group.id)

            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertIs(self.window.current[0], third)
            self.assertEqual(second.state, TaskState.CANCELLED)
            self.assertEqual(third.state, TaskState.RUNNING)
            self.assertEqual(first.state, TaskState.DONE)
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_group_resume_during_cancellation_waits_for_old_thread_to_finish(self):
        group = self._make_group(2)
        first = self.window._task(group.members[0].task_id)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]
            self.window._toggle_task_group_pause(group.id)
            self.window._toggle_task_group_pause(group.id)

            self.assertEqual(len(_FakeWorker.instances), 1)
            self.assertIs(self.window.current[1], worker)

            worker.finish_cancelled()

            self.assertEqual(len(_FakeWorker.instances), 2)
            self.assertEqual(first.state, TaskState.CANCELLED)
            self.assertEqual(
                self.window.current[0].id,
                group.members[1].task_id,
            )
            self.assertEqual(_FakeWorker.max_active, 1)

    def test_fifty_two_batch_items_never_overlap_worker_lifetimes(self):
        group = self._make_group(52)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_task_group(group.id)
            self.assertEqual(
                [
                    self.window._task(member.task_id).state
                    for member in group.members
                ],
                [TaskState.RUNNING] + [TaskState.QUEUED] * 51,
            )
            for index in range(52):
                worker = _FakeWorker.instances[index]
                worker.failed.emit(f"fixture failure {index}")
                self.assertEqual(len(_FakeWorker.instances), index + 1)
                self.assertIs(self.window.current[1], worker)
                self.assertEqual(_FakeWorker.active, 1)
                worker.finish_thread()

        self.assertEqual(len(_FakeWorker.instances), 52)
        self.assertEqual(_FakeWorker.active, 0)
        self.assertEqual(_FakeWorker.max_active, 1)
        self.assertTrue(all(worker.deleted_later for worker in _FakeWorker.instances))
        self.assertIsNone(self.window.current)
        self.assertTrue(
            all(
                self.window._task(member.task_id).state == TaskState.FAILED
                for member in group.members
            )
        )

    def test_cancel_group_keeps_done_stops_waiting_and_requests_current(self):
        group = self._make_group(4)
        first, second, third, fourth = [
            self.window._task(member.task_id) for member in group.members
        ]
        first.state = TaskState.DONE
        second.state = TaskState.RUNNING
        third.state = TaskState.QUEUED
        fourth.state = TaskState.PENDING
        worker = _FakeWorker(second.source)
        worker.cancelled.connect(lambda: self.window._on_cancelled(second.id))
        worker.finished.connect(
            lambda w=worker: self.window._on_transcribe_worker_finished(second.id, w)
        )
        worker.start()
        self.window.current = (second, worker)

        self.window._cancel_task_group(group.id)
        self.assertEqual(first.state, TaskState.DONE)
        self.assertEqual(second.state, TaskState.RUNNING)
        self.assertTrue(worker.cancel_requested)
        self.assertEqual(third.state, TaskState.CANCELLED)
        self.assertEqual(fourth.state, TaskState.CANCELLED)

        worker.finish_cancelled()
        self.assertEqual(second.state, TaskState.CANCELLED)
        self.assertIsNone(self.window.current)

    def test_retry_reuses_failed_or_cancelled_tasks_without_duplicate_cards(self):
        group = self._make_group(4)
        old_ids = [member.task_id for member in group.members]
        old_cards = {
            task_id: self.window._task(task_id)
            for task_id in old_ids
        }
        states = (
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.PENDING,
        )
        for member, state in zip(group.members, states):
            self.window._task(member.task_id).state = state

        self.window._retry_task_group(group.id)
        new_ids = [member.task_id for member in group.members]
        self.assertEqual(new_ids, old_ids)
        self.assertEqual(
            [self.window._task(new_ids[index]).state for index in (1, 2)],
            [TaskState.PENDING, TaskState.PENDING],
        )
        self.assertEqual(
            [self.window._task(new_ids[index]).folder_id for index in (1, 2)],
            [group.folder_id, group.folder_id],
        )
        self.assertEqual(len(self.window.tasks), 4)
        self.assertEqual({task.id for task in self.window.tasks}, set(old_ids))
        self.assertTrue(
            all(self.window._task(task_id) is old_cards[task_id] for task_id in old_ids)
        )

    def test_cancel_group_retry_immediate_and_delayed_keeps_one_current_card(self):
        group = self._make_group(3)
        first, second, third = [
            self.window._task(member.task_id) for member in group.members
        ]
        original_cards = {task.id: task for task in self.window.tasks}

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]
            self.window._cancel_task_group(group.id)

            self.assertEqual(first.state, TaskState.RUNNING)
            self.assertEqual(second.state, TaskState.CANCELLED)
            self.assertEqual(third.state, TaskState.CANCELLED)
            self.window._retry_task_group(group.id)
            self.assertEqual(first.state, TaskState.RUNNING)
            self.assertEqual(second.state, TaskState.PENDING)
            self.assertEqual(third.state, TaskState.PENDING)
            self.assertEqual(len(self.window.tasks), 3)
            self.assertEqual({task.id for task in self.window.tasks}, set(original_cards))

            worker.finish_cancelled()
            self.assertEqual(first.state, TaskState.CANCELLED)
            self.assertEqual(len(_FakeWorker.instances), 1)
            self.window._retry_task_group(group.id)

        self.assertTrue(
            all(
                self.window._task(member.task_id).state == TaskState.PENDING
                for member in group.members
            )
        )
        self.assertEqual(len(self.window.tasks), 3)
        self.assertEqual({task.id for task in self.window.tasks}, set(original_cards))
        self.assertTrue(
            all(
                self.window._task(task_id) is original_cards[task_id]
                for task_id in original_cards
            )
        )

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

    def test_delete_after_result_signal_still_waits_for_thread_finished(self):
        group = self._make_group(2)

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            self.window._start_all()
            worker = _FakeWorker.instances[0]
            worker.failed.emit("结果信号先到")
            self.assertEqual(
                self.window._task(group.members[0].task_id).state,
                TaskState.FAILED,
            )
            self.assertIs(self.window.current[1], worker)

            self.window._delete_task_group(group.id)
            self.assertIn(group.id, self.window._deleting_task_group_ids)
            self.assertIn(group, self.window.task_groups)
            self.assertFalse(worker.deleted_later)
            self.assertEqual(len(_FakeWorker.instances), 1)

            worker.finish_thread()

        self.assertIsNone(self.window.current)
        self.assertTrue(worker.deleted_later)
        self.assertNotIn(group, self.window.task_groups)
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.extraction_workspace.rows(), [])
        self.assertEqual(len(_FakeWorker.instances), 1)

    def test_single_task_retry_preserves_stable_key_and_folder_id(self):
        original = self.window._add_task(
            "fixture://single",
            "link",
            "单条作品",
            folder_id="folder-single",
            stable_key="douyin:single",
        )
        original.state = TaskState.FAILED

        self.window._rerun_task(original.id)

        retried = self.window.tasks[-1]
        self.assertNotEqual(retried.id, original.id)
        self.assertEqual(retried.stable_key, original.stable_key)
        self.assertEqual(retried.folder_id, original.folder_id)

    def test_collection_page_marks_image_and_unsupported_entries_skipped(self):
        page = self.window._ensure_page_loaded(5)
        items = [
            CollectedItem(
                aweme_id="11",
                platform="douyin",
                work_id="11",
                title="抖音视频",
            ),
            CollectedItem(
                aweme_id="",
                platform="xiaohongshu",
                work_id="x1",
                work_url="https://www.xiaohongshu.com/explore/x1",
                title="小红书视频",
            ),
            CollectedItem(
                aweme_id="",
                platform="xiaohongshu",
                work_id="x2",
                work_url="https://www.xiaohongshu.com/explore/x2",
                title="小红书图文",
                content_kind="image",
            ),
            CollectedItem(
                aweme_id="",
                platform="bilibili",
                work_id="b1",
                work_url="https://www.bilibili.com/video/b1",
                title="尚未接入",
            ),
        ]
        page._set_items(items)
        for row in range(page.table.rowCount()):
            page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)

        page._on_transcribe_selected()
        group = self.window.task_groups[-1]
        self.assertEqual(len(group.members), 4)
        self.assertEqual(
            [member.skipped_reason for member in group.members],
            ["", "", "图文作品无音频", "当前平台尚未接入转录"],
        )
        snapshot = derive_task_group(group, self.window.tasks)
        self.assertEqual(snapshot.skipped, 2)
        self.assertEqual(len(self.window.tasks), 2)


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

    def test_transcribe_worker_turns_late_success_into_cancelled(self):
        worker = TranscribeWorker(
            "fixture://slow",
            "local_sensevoice",
            True,
        )
        events = []
        worker.finished_ok.connect(lambda _transcript: events.append("done"))
        worker.cancelled.connect(lambda: events.append("cancelled"))

        with (
            patch.object(worker, "requestInterruption") as request_interruption,
            patch(
                "src.ui.worker.transcribe_source",
                return_value=Transcript(text="安全检查点之后才返回"),
            ),
        ):
            worker.request_cancel()
            worker.run()

        request_interruption.assert_called_once_with()
        self.assertEqual(events, ["cancelled"])

    def test_real_qthreads_finish_serially_without_lifetime_overlap(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_transcribe(source, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return Transcript(text=f"完成：{source}")
            finally:
                with lock:
                    active -= 1

        first = self.window._add_task("fixture://first", "link", "第一条")
        second = self.window._add_task("fixture://second", "link", "第二条")
        saved_ids = iter(("saved-first", "saved-second"))
        with (
            patch("src.ui.worker.transcribe_source", side_effect=fake_transcribe),
            patch("src.core.extraction.acquire_source", side_effect=fake_transcribe),
            patch("src.core.library.save", side_effect=lambda *_a, **_k: next(saved_ids)),
            patch("src.core.library.load", return_value=object()),
            patch.object(MainWindow, "_notify_task_completed", autospec=True),
        ):
            self.window._start_all()
            deadline = time.monotonic() + 3.0
            while self.window.current is not None and time.monotonic() < deadline:
                QTest.qWait(10)
                self.app.processEvents()

        self.assertIsNone(self.window.current)
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)
        self.assertEqual(first.state, TaskState.DONE)
        self.assertEqual(second.state, TaskState.DONE)

    def test_real_qthread_group_start_preserves_finished_gate_and_member_order(self):
        active = 0
        max_active = 0
        call_order = []
        lock = threading.Lock()

        def fake_transcribe(source, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                call_order.append(source)
            try:
                time.sleep(0.03)
                return Transcript(text=f"完成：{source}")
            finally:
                with lock:
                    active -= 1

        group = self._make_group(3)
        saved_ids = iter(("saved-1", "saved-2", "saved-3"))
        with (
            patch("src.ui.worker.transcribe_source", side_effect=fake_transcribe),
            patch("src.core.extraction.acquire_source", side_effect=fake_transcribe),
            patch("src.core.library.save", side_effect=lambda *_a, **_k: next(saved_ids)),
            patch("src.core.library.load", return_value=object()),
            patch.object(MainWindow, "_notify_task_completed", autospec=True),
        ):
            self.window._start_task_group(group.id)
            deadline = time.monotonic() + 3.0
            while self.window.current is not None and time.monotonic() < deadline:
                QTest.qWait(10)
                self.app.processEvents()

        self.assertIsNone(self.window.current)
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)
        self.assertEqual(call_order, [member.source for member in group.members])
        self.assertTrue(
            all(
                self.window._task(member.task_id).state == TaskState.DONE
                for member in group.members
            )
        )

    def test_real_qthread_resume_waits_for_cancelled_thread_before_next_member(self):
        active = 0
        max_active = 0
        call_order = []
        lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        def fake_transcribe(source, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                call_order.append(source)
                is_first = len(call_order) == 1
            try:
                if is_first:
                    first_started.set()
                    release_first.wait(timeout=2.0)
                return Transcript(text=f"完成：{source}")
            finally:
                with lock:
                    active -= 1

        group = self._make_group(3)
        first, second, third = [
            self.window._task(member.task_id) for member in group.members
        ]
        saved_ids = iter(("saved-2", "saved-3"))
        try:
            with (
                patch("src.ui.worker.transcribe_source", side_effect=fake_transcribe),
            patch("src.core.extraction.acquire_source", side_effect=fake_transcribe),
                patch(
                    "src.core.library.save",
                    side_effect=lambda *_a, **_k: next(saved_ids),
                ),
                patch("src.core.library.load", return_value=object()),
                patch.object(MainWindow, "_notify_task_completed", autospec=True),
            ):
                self.window._start_task_group(group.id)
                deadline = time.monotonic() + 2.0
                while not first_started.is_set() and time.monotonic() < deadline:
                    QTest.qWait(10)
                    self.app.processEvents()
                self.assertTrue(first_started.is_set())
                first_worker = self.window.current[1]

                self.window._toggle_task_group_pause(group.id)
                self.window._toggle_task_group_pause(group.id)

                self.assertIs(self.window.current[1], first_worker)
                self.assertEqual(call_order, [first.source])
                self.assertEqual(first.state, TaskState.RUNNING)
                self.assertEqual(first.message, "正在取消…")
                self.assertEqual(second.state, TaskState.QUEUED)
                self.assertEqual(third.state, TaskState.QUEUED)

                release_first.set()
                deadline = time.monotonic() + 3.0
                while self.window.current is not None and time.monotonic() < deadline:
                    QTest.qWait(10)
                    self.app.processEvents()
        finally:
            release_first.set()
            deadline = time.monotonic() + 3.0
            while self.window.current is not None and time.monotonic() < deadline:
                QTest.qWait(10)
                self.app.processEvents()

        self.assertIsNone(self.window.current)
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)
        self.assertEqual(first.state, TaskState.CANCELLED)
        self.assertEqual(second.state, TaskState.DONE)
        self.assertEqual(third.state, TaskState.DONE)
        self.assertEqual(call_order, [member.source for member in group.members])

    def test_real_qthread_last_member_resume_waits_and_never_revives_cancelled_item(self):
        active = 0
        max_active = 0
        call_order = []
        lock = threading.Lock()
        worker_started = threading.Event()
        release_worker = threading.Event()

        def fake_transcribe(source, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                call_order.append(source)
            try:
                worker_started.set()
                release_worker.wait(timeout=2.0)
                return Transcript(text=f"完成：{source}")
            finally:
                with lock:
                    active -= 1

        group = self._make_group(2)
        completed, last_running = [
            self.window._task(member.task_id) for member in group.members
        ]
        completed.state = TaskState.DONE
        try:
            with (
                patch("src.ui.worker.transcribe_source", side_effect=fake_transcribe),
            patch("src.core.extraction.acquire_source", side_effect=fake_transcribe),
                patch.object(MainWindow, "_notify_task_completed", autospec=True),
            ):
                self.window._start_task_group(group.id)
                deadline = time.monotonic() + 2.0
                while not worker_started.is_set() and time.monotonic() < deadline:
                    QTest.qWait(10)
                    self.app.processEvents()
                self.assertTrue(worker_started.is_set())
                worker = self.window.current[1]

                self.window._toggle_task_group_pause(group.id)
                self.window._toggle_task_group_pause(group.id)

                self.assertNotIn(group.id, self.window._paused_task_group_ids)
                self.assertIs(self.window.current[1], worker)
                self.assertEqual(call_order, [last_running.source])
                self.assertEqual(last_running.state, TaskState.RUNNING)
                self.assertEqual(last_running.message, "正在取消…")

                release_worker.set()
                deadline = time.monotonic() + 3.0
                while self.window.current is not None and time.monotonic() < deadline:
                    QTest.qWait(10)
                    self.app.processEvents()
        finally:
            release_worker.set()
            deadline = time.monotonic() + 3.0
            while self.window.current is not None and time.monotonic() < deadline:
                QTest.qWait(10)
                self.app.processEvents()

        self.assertIsNone(self.window.current)
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)
        self.assertEqual(call_order, [last_running.source])
        self.assertEqual(completed.state, TaskState.DONE)
        self.assertEqual(last_running.state, TaskState.CANCELLED)

    def test_groups_are_runtime_only_without_an_explicit_recovery_store(self):
        self._make_group(2)
        self.assertEqual(len(self.window.task_groups), 1)
        other = MainWindow()
        try:
            self.assertEqual(other.task_groups, [])
            self.assertEqual(other.tasks, [])
        finally:
            timer = getattr(other, "_runtime_preferences_timer", None)
            if timer is not None:
                timer.stop()
            other.deleteLater()


if __name__ == "__main__":
    unittest.main()
