from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.task import (
    Task,
    TaskExecutionPolicy,
    TaskGroup,
    TaskGroupMember,
    TaskState,
)
from src.core.task_queue_store import (
    MAX_TASKS,
    TaskQueueStore,
    TaskQueueStoreError,
)


def _policy(key: str = "") -> TaskExecutionPolicy:
    if key:
        return TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key=key,
            snapshot=MaterialPreferenceSnapshot(),
        )
    return TaskExecutionPolicy(
        origin="single_link",
        mode="materialize",
        stable_key="",
        material_snapshot=MaterialPreferenceSnapshot("audio", True, True),
        media_destination="user_directory",
        media_directory="/tmp/frozen-media",
        prefer_subtitles=True,
    )


class TaskQueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "task-queue" / "pending.json"
        self.store = TaskQueueStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_keeps_only_recoverable_tasks_and_group_members(self):
        pending = Task(1, "待办", "/tmp/a.wav", "file", execution_policy=_policy())
        running = Task(
            2,
            "运行中",
            "https://example.invalid/2",
            "link",
            state=TaskState.RUNNING,
            stable_key="douyin:2",
            execution_policy=_policy("douyin:2"),
        )
        done = Task(3, "已完成", "/tmp/done.wav", "file", state=TaskState.DONE)
        group = TaskGroup(
            8,
            "作者批次",
            [
                TaskGroupMember(
                    "douyin:2",
                    "运行中",
                    running.source,
                    "link",
                    task_id=2,
                    attempt_task_ids=[2],
                ),
                TaskGroupMember(
                    "douyin:3",
                    "已完成",
                    done.source,
                    "file",
                    task_id=3,
                    attempt_task_ids=[3],
                ),
            ],
            folder_id="folder-1",
            expanded=True,
        )

        self.assertTrue(self.store.save([pending, running, done], [group]))
        restored = TaskQueueStore(self.path).load()

        self.assertEqual([task.id for task in restored.tasks], [1, 2])
        self.assertEqual(restored.tasks[1].state, TaskState.RUNNING)
        self.assertEqual(restored.tasks[1].execution_policy, running.execution_policy)
        self.assertEqual(
            restored.tasks[0].execution_policy.media_directory,
            "/tmp/frozen-media",
        )
        self.assertTrue(restored.tasks[0].execution_policy.prefer_subtitles)
        self.assertEqual(len(restored.groups), 1)
        self.assertEqual([m.task_id for m in restored.groups[0].members], [2])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_completed_removed_and_failed_retained_for_next_launch(self):
        tasks = [
            Task(1, "完成", "/tmp/a.wav", "file", state=TaskState.DONE),
            Task(2, "失败", "/tmp/b.wav", "file", state=TaskState.FAILED, error="无法读取", message="转录失败"),
            Task(3, "取消", "/tmp/c.wav", "file", state=TaskState.CANCELLED),
        ]
        self.store.save(tasks, [])
        restored = TaskQueueStore(self.path).load()
        self.assertEqual([t.id for t in restored.tasks], [2])
        self.assertEqual(restored.tasks[0].error, "无法读取")
        self.assertEqual(restored.tasks[0].message, "转录失败")
        self.assertEqual(restored.groups, ())

    def test_corrupt_file_is_preserved_and_blocks_overwrite(self):
        self.path.parent.mkdir(parents=True)
        original = b"{broken-json\n"
        self.path.write_bytes(original)
        self.path.chmod(0o600)

        with self.assertRaises(TaskQueueStoreError) as caught:
            self.store.load()
        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertFalse(self.store.save([Task(1, "新任务", "/tmp/a", "file")], []))
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_fields_fail_closed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"schema": 1, "tasks": [], "groups": [], "surprise": True}),
            encoding="utf-8",
        )
        self.path.chmod(0o600)
        with self.assertRaises(TaskQueueStoreError) as caught:
            self.store.load()
        self.assertEqual(caught.exception.code, "invalid_schema")

    def test_schema_one_policy_loads_and_next_save_migrates_to_schema_three(self):
        legacy = {
            "schema": 1,
            "tasks": [{
                "id": 1,
                "title": "旧任务",
                "source": "https://example.invalid/legacy",
                "kind": "link",
                "state": "queued",
                "folder_id": None,
                "stable_key": "",
                "execution_policy": {
                    "origin": "single_link",
                    "mode": "materialize",
                    "stable_key": "",
                    "media_destination": "user_directory",
                    "material_snapshot": {
                        "download_mode": "video",
                        "save_media": True,
                        "save_cover": True,
                        "version": 1,
                    },
                },
            }],
            "groups": [],
        }
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        self.path.chmod(0o600)

        snapshot = self.store.load()
        policy = snapshot.tasks[0].execution_policy
        self.assertIsNone(policy.media_directory)
        self.assertIsNone(policy.prefer_subtitles)
        self.assertTrue(self.store.save(snapshot.tasks, snapshot.groups))
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema"], 3)
        migrated_policy = migrated["tasks"][0]["execution_policy"]
        self.assertIn("media_directory", migrated_policy)
        self.assertIn("prefer_subtitles", migrated_policy)

    def test_symlink_target_is_never_read_or_replaced(self):
        target = Path(self.temp.name) / "outside.json"
        target.write_text("outside", encoding="utf-8")
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(target)

        with self.assertRaises(TaskQueueStoreError) as caught:
            self.store.load()
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_task_count_limit_is_enforced_before_write(self):
        tasks = [
            Task(index + 1, f"任务 {index}", f"/tmp/{index}", "file")
            for index in range(MAX_TASKS + 1)
        ]
        with self.assertRaises(TaskQueueStoreError) as caught:
            self.store.save(tasks, [])
        self.assertEqual(caught.exception.code, "limit_exceeded")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
