"""Repeated native shell lifecycle with temporary tasks and manuscript data."""
import time
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer, qInstallMessageHandler

from src.core import library
from tests import test_task_queue_ui_recovery as fixture


class BasicStabilityTests(fixture.TaskQueueUiRecoveryTests):
    def setUp(self):
        super().setUp()
        self.qt_messages=[]
        self.previous_handler=qInstallMessageHandler(lambda kind,context,message:self.qt_messages.append(message))

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            qInstallMessageHandler(self.previous_handler)
        self.assertEqual([m for m in self.qt_messages if 'WebEnginePage still not deleted' in m],[])

    def until(self, predicate, timeout=8):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            loop=QEventLoop();QTimer.singleShot(10,loop.quit);loop.exec()
        self.assertTrue(predicate())

    def test_repeated_navigation_close_and_reopen_preserve_tasks_and_manuscript(self):
        cfg=fixture.AppConfig.load()
        cfg.auto_update_enabled=False
        cfg.confirm_exit_during_tasks=False
        text='本轮稳定性检查🙂\n\n保存后重开，正文应逐字保留。'
        with patch.object(library,'FOLDERS_FILE',self.root/'library/folders.json'):
            document=library.save_document(text,'临时稳定性检查')
            for iteration in range(3):
                with self.subTest(iteration=iteration):
                    window=self._window();window.show()
                    self.assertEqual(len(window.tasks),iteration)
                    self.assertIsNone(window.current)
                    window._switch_page(1)
                    self.until(lambda:window.models_page.ready and bool(window.models_page.controller.state))
                    for purpose in (0,1,2,0,1,0):
                        window.models_page.show_purpose(purpose)
                        self.app.processEvents()
                    window._switch_page(2)
                    window._switch_page(6)
                    window._switch_page(0)
                    self.assertEqual(library.load_markdown(document),text)
                    path=self.root/f'pending-{iteration}.wav'
                    path.write_bytes(b'pending test input; never transcribed')
                    window._add_task(str(path),'file',f'待处理 {iteration}')
                    self.assertIsNone(window.current)
                    window.close()
                    self.until(lambda:not window.isVisible())
                    self.assertFalse(any(worker.isRunning() for worker in window.models_page.owned_workers()))
            self.assertEqual(library.load_markdown(document),text)
            self.assertEqual(len(fixture.TaskQueueStore(self.path).load().tasks),3)
