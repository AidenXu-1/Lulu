from __future__ import annotations

import os
import unittest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget
from src.core.task import Task, TaskGroup, TaskGroupMember, TaskState
from src.ui.library_page import FolderSection
from tests import test_frontend_functional_repair as window_fixture
from tests.test_extraction_workspace import ExtractionWorkspaceTests


class QueueGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class FullWindowQueueGeometryTests(unittest.TestCase):
    tick = ExtractionWorkspaceTests.tick
    until = ExtractionWorkspaceTests.until
    js = ExtractionWorkspaceTests.js
    setUpClass = window_fixture.FrontendFunctionalRepairTests.__dict__['setUpClass']
    setUp = window_fixture.FrontendFunctionalRepairTests.setUp
    tearDown = window_fixture.FrontendFunctionalRepairTests.tearDown
    group = window_fixture.FrontendFunctionalRepairTests.group

    def test_restored_expanded_group_keeps_normal_intake_visible(self):
        from types import SimpleNamespace
        from src.core.task_queue_store import TaskQueueSnapshot
        window = self.window
        window.resize(1210, 768)
        window.show()
        tasks = tuple(Task(id=i, title=f'作品 {i}', source=f'https://example.invalid/{i}',
                           kind='link', state=TaskState.PENDING) for i in range(1, 9))
        group = TaskGroup(1, '恢复的批次', [TaskGroupMember(str(t.id), t.title, task_id=t.id) for t in tasks], expanded=True)
        window._task_queue_store = SimpleNamespace(authoritative=True)
        window._restore_task_queue(TaskQueueSnapshot(tasks, (group,)))
        group = window.task_groups[0]
        self.widget = window.extraction_workspace
        self.until(lambda: self.widget.ready)
        self.widget.publish(force=True)
        self.until(lambda: self.js('tasks.length') == 8)
        self.assertTrue(self.widget.isVisible())
        self.assertTrue(self.js('document.querySelector("#intake").getBoundingClientRect().height > 0'))
        self.assertTrue(self.js('document.querySelector("#url").getBoundingClientRect().bottom < innerHeight'))
        self.assertTrue(self.js('expanded.has("1")'))
        self.assertTrue(group.expanded)
        self.js('toggleGroup(tasks[0].id)')
        self.until(lambda: not group.expanded)
        window._restore_task_queue(TaskQueueSnapshot((), (group,)))
        self.widget.publish(force=True)
        self.until(lambda: not self.js('expanded.has("1")'))
        self.js('toggleGroup(tasks[0].id)')
        self.until(lambda: group.expanded)
        self.assertTrue(self.js('document.querySelector("#intake").getBoundingClientRect().height > 0'))


if __name__ == '__main__':
    unittest.main()
