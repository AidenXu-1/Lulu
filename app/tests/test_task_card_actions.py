import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout

from src.core.models import Transcript
from src.core.task import Task, TaskState
from src.ui.main_window import MainWindow


class TaskCardActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _done_link_task(self, source: str) -> Task:
        return Task(
            id=17,
            title="测试作品",
            source=source,
            kind="link",
            state=TaskState.DONE,
            transcript=Transcript(text="测试逐字稿", source=source),
        )








    def test_original_link_extracts_url_from_share_text(self):
        task = self._done_link_task(
            "3.20 复制打开抖音，看看【测试作品】 "
            "https://v.douyin.com/abc123/ 12/30 abc:/"
        )
        notices: list[str] = []

        class _Window:
            def _task(self, tid):
                return task if tid == task.id else None

            def _toast(self, message):
                notices.append(message)

        opened: list[QUrl] = []
        with patch(
            "PySide6.QtGui.QDesktopServices.openUrl",
            side_effect=lambda url: opened.append(url) or True,
        ):
            MainWindow._open_task_link(_Window(), task.id)

        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].toString(), "https://v.douyin.com/abc123/")
        self.assertEqual(notices, [])

    def test_original_link_failure_is_visible(self):
        task = self._done_link_task("https://v.douyin.com/abc123/")
        notices: list[str] = []

        class _Window:
            def _task(self, tid):
                return task if tid == task.id else None

            def _toast(self, message):
                notices.append(message)

        with patch("PySide6.QtGui.QDesktopServices.openUrl", return_value=False):
            MainWindow._open_task_link(_Window(), task.id)

        self.assertEqual(notices, ["原链接暂时无法打开，请稍后重试"])


if __name__ == "__main__":
    unittest.main()
