"""Restore completed cards from saved results without rewriting task inputs."""

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core import library
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.models import Transcript
from src.core.task import TaskState


class ResultDisplayRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = patch.object(library, "LIBRARY_DIR", self.root)
        override.start()
        self.addCleanup(override.stop)

    def job(self, entry_id, *, state="succeeded"):
        return {
            "job_id": "job-" + entry_id, "job_revision": 3, "state": state,
            "job_kind": "transcription", "input_ref": "https://example.com/video",
            "gui_task": {"title": "链接转写", "source": "https://example.com/video",
                         "kind": "link", "stable_key": "video-" + entry_id},
            "result_ref": "lulu-library:" + entry_id, "created_by": "gui",
        }

    def store(self, jobs, *, hidden=(), groups=()):
        def dispatch(command, arguments):
            self.assertEqual(command, "gui.jobs.list")
            self.assertEqual(arguments, {"include_view": True})
            return {"jobs": copy.deepcopy(jobs), "view": {
                "revision": 1, "groups": list(groups), "hidden_ids": list(hidden)}}
        return CoreTaskQueueStore(dispatch, async_write=True)

    def saved(self, entry_id="saved"):
        library.save(Transcript(text="已保存的完整文稿", engine="SenseVoiceSmall",
                                source="https://example.com/video",
                                meta={"author": {"name": "作品作者"}, "view_count": 123}),
                     "已保存的真实作品标题", entry_id=entry_id)
        return self.job(entry_id)

    def test_restart_restores_card_and_group_from_result_without_mutating_input(self):
        job = self.saved()
        original = copy.deepcopy(job)
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        group = {"id": "batch", "name": "主页批量", "folder_id": None,
                 "expanded": True, "members": [{"task_id": job["job_id"]}]}
        snapshot = self.store([job], groups=[group]).load()
        task = snapshot.tasks[0]
        self.assertEqual(task.title, "已保存的真实作品标题")
        self.assertEqual(task.transcript.text, "已保存的完整文稿")
        self.assertEqual(task.transcript.meta["author"]["name"], "作品作者")
        self.assertEqual(task.source, job["gui_task"]["source"])
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(snapshot.groups[0].members[0].title, task.title)
        self.assertEqual(job, original)
        self.assertEqual(files, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_missing_or_corrupt_result_does_not_block_other_completed_tasks(self):
        good = self.saved()
        corrupt = self.root / "corrupt.json"
        corrupt.write_text("{broken", encoding="utf-8")
        snapshot = self.store([self.job("missing"), self.job("corrupt"), good]).load()
        self.assertEqual(len(snapshot.tasks), 3)
        for task in snapshot.tasks[:2]:
            self.assertEqual(task.state, TaskState.DONE)
            self.assertIsNone(task.transcript)
            self.assertEqual(task.title, "链接转写")
        self.assertEqual(snapshot.tasks[2].title, "已保存的真实作品标题")
        self.assertEqual(corrupt.read_text(), "{broken")
        self.assertFalse((self.root / "missing.json").exists())

    def test_hidden_and_unfinished_tasks_do_not_load_results(self):
        hidden = self.job("hidden")
        running = self.job("running", state="running")
        with patch.object(library, "load", side_effect=AssertionError("unexpected read")):
            snapshot = self.store([hidden, running], hidden=[hidden["job_id"]]).load()
        self.assertEqual(len(snapshot.tasks), 1)
        self.assertEqual(snapshot.tasks[0].state, TaskState.RUNNING)

    def test_read_failure_is_isolated_and_blank_title_keeps_original(self):
        result = Transcript(text="仍可查看", title="  ")
        with patch.object(library, "load", side_effect=[OSError("unavailable"), result]):
            snapshot = self.store([self.job("unreadable"), self.job("blank")]).load()
        self.assertIsNone(snapshot.tasks[0].transcript)
        self.assertEqual(snapshot.tasks[1].title, "链接转写")
        self.assertEqual(snapshot.tasks[1].transcript.text, "仍可查看")
