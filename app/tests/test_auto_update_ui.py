import os
import subprocess
import sys
import threading
import textwrap
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QBoxLayout
from PySide6.QtTest import QTest

from src.core.updater.models import UpdateSnapshot
from src.ui import theme as T
from src.ui.update_ui import AboutUpdatePanel, UpdateController, UpdatePanel, UpdateSwitch, _check_error_message, _size
from src.ui.main_window import MainWindow


class Config:
    def __init__(self):
        self.auto_update_enabled = True
        self.save_result = True
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        return self.save_result


class UpdateUiTests(unittest.TestCase):
    def setUp(self): self.app=QApplication.instance() or QApplication([]); T.apply_theme("warm")

    def test_click_publishes_checking_before_worker_enters_service(self):
        from dataclasses import replace
        from src.core.updater.models import UpdateError
        from PySide6.QtCore import QEventLoop, QTimer
        for about, failure in ((False, ''), (True, ''), (True, 'check_error'), (True, 'manifest_verify_error')):
            gate, entered = threading.Event(), threading.Event()
            # A pending download is real durable state, not UI feedback storage.
            saved = UpdateSnapshot(state='download_paused', ui_state='up_to_date', downloaded=2, total=6)
            service = SimpleNamespace(snapshot=saved, public_keys={'test': 'key'}, store=Mock())
            def check(*, manual):
                entered.set()
                if not gate.wait(3): raise AssertionError('test gate timeout')
                if failure:
                    service.snapshot = replace(saved, state='failed', ui_state=failure)
                    raise UpdateError('network' if failure == 'check_error' else 'signature', '检查失败')
                service.snapshot = replace(saved, ui_state='up_to_date', verified_manifest=True)
                return service.snapshot
            service.check = Mock(side_effect=check)
            c = UpdateController(service=service)
            c._result_timer.stop()
            p = (AboutUpdatePanel(Config(), c, repository_url='repo', author_url='author',
                 product_boundary='boundary', open_url=lambda _: None, show_contact=lambda: None)
                 if about else UpdatePanel(Config(), c))
            try:
                p.primary.click()
                self.assertEqual(c.snapshot.ui_state, 'checking_manual')
                self.assertIn('正在检查', p.primary.text())
                self.assertTrue(p.spinner._timer.isActive())
                self.assertFalse(p.primary.isEnabled())
                self.assertEqual(saved.ui_state, 'up_to_date')
                self.assertEqual((saved.state, saved.downloaded), ('download_paused', 2))
                p.primary.click()
                self.assertTrue(entered.wait(1))
            finally:
                gate.set()
                loop = QEventLoop()
                c.changed.connect(loop.quit)
                QTimer.singleShot(3000, loop.quit)
                loop.exec()
                c.shutdown(3000)
                self.app.processEvents()
                p.close()
            self.assertEqual(service.check.call_count, 1)
            self.assertEqual(c.snapshot.ui_state, failure or 'up_to_date')
            self.assertTrue(p.primary.isEnabled())

    def test_about_button_hover_and_press_change_rendered_background(self):
        from PySide6.QtCore import QPoint
        c = UpdateController()
        c.check = Mock()
        p = AboutUpdatePanel(Config(), c, repository_url='repo', author_url='author',
            product_boundary='boundary', open_url=lambda _: None, show_contact=lambda: None)
        p.resize(760, 720); p.show(); self.app.processEvents()
        b = p.primary
        try:
            QTest.mouseMove(p, QPoint(2, 2)); self.app.processEvents()
            normal = b.grab().toImage().pixelColor(12, b.height() // 2)
            QTest.mouseMove(b, b.rect().center()); self.app.processEvents()
            hover = b.grab().toImage().pixelColor(12, b.height() // 2)
            QTest.mousePress(b, Qt.LeftButton); self.app.processEvents()
            pressed = b.grab().toImage().pixelColor(12, b.height() // 2)
            QTest.mouseRelease(b, Qt.LeftButton)
            self.assertNotEqual(normal, hover)
            self.assertNotEqual(hover, pressed)
            self.assertEqual(b.cursor().shape(), Qt.PointingHandCursor)
        finally:
            p.close(); c._result_timer.stop()


    def test_interrupted_download_retains_continue_action_in_both_panels(self):
        for about in (False, True):
            snapshot=UpdateSnapshot(state='download_paused',ui_state='download_paused',downloaded=2*1024*1024,total=6*1024*1024,error_code='network')
            service=SimpleNamespace(snapshot=snapshot,store=Mock())
            controller=UpdateController(service=service);controller.download=Mock()
            panel=(AboutUpdatePanel(Config(),controller,repository_url='repo',author_url='author',product_boundary='boundary',open_url=lambda _:None,show_contact=lambda:None) if about else UpdatePanel(Config(),controller))
            controller._failed('network','下载连接中断')
            self.assertEqual(controller.snapshot.ui_state,'download_paused')
            self.assertIn('已保留',panel.message.text());self.assertIn('2.0 MB',panel.message.text())
            self.assertEqual(panel.primary.text(),'继续下载')
            panel.primary.click();controller.download.assert_called_once_with()
            panel.close();controller._result_timer.stop()

    def test_cancel_without_progress_does_not_offer_continue(self):
        for about in (False,True):
            snapshot=UpdateSnapshot(state='failed',ui_state='download_error',downloaded=0,total=6,error_code='cancelled')
            service=SimpleNamespace(snapshot=snapshot,store=Mock())
            controller=UpdateController(service=service);controller.download=Mock()
            panel=(AboutUpdatePanel(Config(),controller,repository_url='repo',author_url='author',product_boundary='boundary',open_url=lambda _:None,show_contact=lambda:None) if about else UpdatePanel(Config(),controller))
            controller._failed('cancelled','下载已暂停')
            self.assertEqual(controller.snapshot.ui_state,'download_error')
            self.assertEqual(panel.primary.text(),'重新下载')
            self.assertNotIn('下次可以继续',panel.message.text())
            panel.primary.click();controller.download.assert_called_once_with()
            panel.close();controller._result_timer.stop()


    def test_staging_failure_has_honest_message_and_working_retry_in_both_panels(self):
        for about in (False, True):
            controller = UpdateController()
            controller.check = Mock()
            panel = (AboutUpdatePanel(Config(), controller, repository_url='repo', author_url='author',
                        product_boundary='boundary', open_url=lambda _: None, show_contact=lambda: None)
                     if about else UpdatePanel(Config(), controller))
            for state, code, words in (('zip_structure_error', 'staged_exists', '临时目录'),
                                       ('preparation_error', 'staged_exists', '临时目录'),
                                       ('preparation_error', 'storage_full', '空间'),
                                       ('preparation_error', 'staging_io', '权限')):
                with self.subTest(about=about, state=state, code=code):
                    controller.snapshot = UpdateSnapshot(ui_state=state, error_code=code)
                    controller.publish()
                    self.assertNotIn('安装包结构异常', panel.state_title.text())
                    self.assertIn(words, panel.message.text())
                    panel.primary.click()
                    controller.check.assert_called_with(manual=True)
                    controller.check.reset_mock()
            panel.close()


    def test_shutdown_keeps_running_update_worker_until_it_naturally_settles(self):
        release = threading.Event()

        class BlockingUpdateWorker(QThread):
            def __init__(self):
                super().__init__()
                self.cancel_requested = False

            def run(self):
                release.wait(2)

        controller = UpdateController()
        worker = BlockingUpdateWorker()
        controller._worker = worker
        worker.start()
        self.assertTrue(worker.isRunning())

        self.assertFalse(controller.shutdown(1))
        self.assertTrue(worker.cancel_requested)
        self.assertIs(controller._worker, worker)

        release.set()
        self.assertTrue(worker.wait(1000))
        self.assertTrue(controller.shutdown(0))
        self.assertIsNone(controller._worker)

    def test_shutdown_contract_exits_controlled_child_without_qthread_fatal(self):
        script = textwrap.dedent(
            """
            import os
            import threading
            from types import SimpleNamespace
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtCore import QCoreApplication, QThread
            from src.ui.main_window import MainWindow
            from src.ui.model_settings.controller import ModelController
            from src.ui.update_ui import UpdateController

            app = QCoreApplication.instance() or QCoreApplication([])

            class BlockingWorker(QThread):
                def __init__(self, release):
                    super().__init__()
                    self.release = release
                    self.cancel_requested = False
                def request_cancel(self):
                    self.cancel_requested = True
                    self.requestInterruption()
                def run(self):
                    self.release.wait(2)

            update_release = threading.Event()
            update_worker = BlockingWorker(update_release)
            update = UpdateController()
            update._worker = update_worker
            update_worker.start()
            assert update.shutdown(1) is False
            assert update._worker is update_worker

            model_release = threading.Event()
            model_worker = BlockingWorker(model_release)
            model = SimpleNamespace(workers=[model_worker], closing=False)
            model.owned_workers = lambda: list(model.workers)
            model.shutdown_workers = lambda timeout=0: ModelController.shutdown_workers(model, timeout)
            model_worker.start()

            page_release = threading.Event()
            page_worker = BlockingWorker(page_release)
            page_worker.start()
            collection_shutdown_calls = []
            collection = SimpleNamespace(
                _worker=page_worker,
                shutdown_workers=lambda: collection_shutdown_calls.append(True),
            )

            class Event:
                ignored = False
                def ignore(self):
                    self.ignored = True

            notices = []
            shell = SimpleNamespace(
                cfg=SimpleNamespace(confirm_exit_during_tasks=False, save=lambda: True),
                _close_settling=False,
                _close_retry_scheduled=False,
                _close_xhs_action="keep",
                has_active_work=lambda: False,
                _choose_xhs_exit_action=lambda: "keep",
                _settle_xhs_exit=lambda _action: True,
                collection_page=collection,
                models_page=model,
                library_page=None,
                live_controller=SimpleNamespace(request_stop=lambda: True),
                voice_page=None,
                live_clips_page=None,
                settings_page=None,
                update_controller=update,
                current=None,
                _transcript_cover_retry_worker=None,
                _cover_preference_unsubscribe=None,
                gsv_finetune_running=lambda: False,
                power_manager=SimpleNamespace(shutdown=lambda: None),
                _toast=notices.append,
                close=lambda: None,
                findChildren=lambda _type: [],
            )
            shell._registered_qthreads = lambda: MainWindow._registered_qthreads(shell)
            shell._settle_registered_qthreads = (
                lambda timeout=0: MainWindow._settle_registered_qthreads(shell, timeout)
            )
            shell._defer_close_until_workers_settle = (
                lambda event: MainWindow._defer_close_until_workers_settle(shell, event)
            )
            event = Event()
            MainWindow.closeEvent(shell, event)
            assert event.ignored is True
            assert notices == ["正在安全停止后台任务"]
            assert update._worker is update_worker
            assert model.workers == [model_worker]
            assert collection_shutdown_calls == []
            assert page_worker.isRunning()

            update_release.set()
            model_release.set()
            page_release.set()
            assert update_worker.wait(1000)
            assert model_worker.wait(1000)
            assert page_worker.wait(1000)
            assert update.shutdown(0) is True
            assert ModelController.shutdown_workers(model, 0) is True
            app.processEvents()
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("QThread: Destroyed while thread is still running", result.stderr)
        self.assertNotIn("Abort trap", result.stderr)


if __name__=="__main__": unittest.main()
