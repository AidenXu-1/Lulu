"""Current extraction queue keeps batch state without native widget copies."""
import unittest
from PySide6.QtWidgets import QWidget
from tests.test_task_groups import TaskGroupRuntimeTests, _candidate


class TaskGroupPerformanceTests(unittest.TestCase):
    setUpClass = TaskGroupRuntimeTests.__dict__["setUpClass"]
    setUp = TaskGroupRuntimeTests.setUp
    tearDown = TaskGroupRuntimeTests.tearDown

    def test_fifty_two_tasks_do_not_create_hidden_widget_copies(self):
        before = len(self.window.findChildren(QWidget))
        self.window._collect_transcribe_group([_candidate(i) for i in range(52)], "批量样本")
        self.assertEqual(len(self.window.tasks), 52)
        self.assertEqual(len(self.window.task_groups[0].members), 52)
        self.assertEqual(len(self.window.extraction_workspace.rows()), 52)
        self.assertEqual(len(self.window.findChildren(QWidget)), before)
        self.assertFalse(hasattr(self.window, "cards"))
        self.assertFalse(hasattr(self.window, "task_group_cards"))
        for name in ("link_input", "task_list", "queue_scroll", "mic_btn", "rec_source_hint"):
            self.assertFalse(hasattr(self.window, name), name)
        self.window._delete_task_group(self.window.task_groups[0].id)
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.extraction_workspace.rows(), [])

    def test_repeated_recovery_keeps_one_task_and_group_identity(self):
        from types import SimpleNamespace
        from src.core.task_queue_store import TaskQueueSnapshot
        self.window._collect_transcribe_group([_candidate(i) for i in range(3)], "恢复样本")
        tasks, groups = tuple(self.window.tasks), tuple(self.window.task_groups)
        self.window.tasks.clear()
        self.window.task_groups.clear()
        self.window._task_queue_store = SimpleNamespace(authoritative=False, save=lambda *_: True)
        snapshot = TaskQueueSnapshot(tasks, groups)
        self.window._restore_task_queue(snapshot)
        first_ids = [row["id"] for row in self.window.extraction_workspace.rows()]
        self.window._restore_task_queue(snapshot)
        self.assertEqual(len(self.window.task_groups), 1)
        self.assertEqual(tuple(self.window.tasks), tasks)
        self.assertEqual([row["id"] for row in self.window.extraction_workspace.rows()], first_ids)
