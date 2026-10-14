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

    def test_recovery_buttons_execute_the_visible_action_in_both_panels(self):
        for about in (False, True):
            controller = UpdateController()
            controller.check = Mock()
            controller.download = Mock()
            panel = (AboutUpdatePanel(Config(), controller, repository_url='repo', author_url='author',
                        product_boundary='boundary', open_url=lambda _: None, show_contact=lambda: None)
                     if about else UpdatePanel(Config(), controller))
            manual = Mock()
            panel.manual_download.connect(manual)
            for state, action in (('install_error_rollback', controller.download), ('location_error', controller.check)):
                controller.snapshot = UpdateSnapshot(ui_state=state)
                controller.publish()
                panel.primary.click()
                self.assertEqual(action.call_count, 1)
            controller.snapshot = UpdateSnapshot(ui_state='rollback_failed', manual_url='https://example.test/download')
            controller.publish()
            (panel.primary if about else panel.tertiary).click()
            from src.core.updater.constants import MANUAL_DOWNLOAD_URL
            manual.assert_called_once_with(MANUAL_DOWNLOAD_URL)
            controller.snapshot = UpdateSnapshot(ui_state='installed')
            controller.publish()
            panel.primary.click()
            self.assertEqual(controller.snapshot.ui_state, 'up_to_date')
            panel.close()

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

    def test_panel_omits_unverified_content(self):
        controller=UpdateController(); panel=UpdatePanel(Config(),controller)
        controller.snapshot=UpdateSnapshot(ui_state="manifest_verify_error",latest_version="9.9.9",notes="UNVERIFIED",verified_manifest=False)
        controller.publish(); self.app.processEvents()
        self.assertFalse(panel.notes.isVisible()); self.assertNotIn("9.9.9",panel.state_title.text())

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

    def test_about_panel_is_compact_and_uses_same_controller(self):
        cfg=Config(); controller=UpdateController(); panel=AboutUpdatePanel(
            cfg,controller,
            repository_url="https://github.com/AidenXu-1/lulu-releases",
            author_url="https://github.com/AidenXu-1",
            product_boundary="本地优先",
            open_url=lambda _url: None,
            show_contact=lambda: None,
        )
        controller.snapshot=UpdateSnapshot(ui_state="up_to_date",latest_version="0.2.4",verified_manifest=True); controller.publish(); self.app.processEvents()
        self.assertEqual(panel.state_title.text(),"已是最新版本")
        self.assertEqual(panel.message.text(), "")
        self.assertEqual(panel.primary.text(), "检查更新")
        self.assertEqual(panel.status_dot._color, T.GREEN)
        self.assertFalse(hasattr(panel,"notes"))
        self.assertEqual(panel.auto.isChecked(),cfg.auto_update_enabled)

        cfg.save_result=False; panel.auto.click(); self.app.processEvents()
        self.assertTrue(cfg.auto_update_enabled)
        self.assertTrue(panel.auto.isChecked())
        self.assertTrue(panel.inline_error.isVisible() or bool(panel.inline_error.text()))

    def test_about_latest_collapses_empty_detail_slot_and_reclaims_height(self):
        controller=UpdateController(); panel=AboutUpdatePanel(
            Config(),controller,repository_url="repo",author_url="author",
            product_boundary="boundary",open_url=lambda _url:None,show_contact=lambda:None,
        )
        panel.resize(760,720); panel.show()
        panel.render(UpdateSnapshot(ui_state="check_error",error_code="network")); self.app.processEvents()
        expanded_height=panel.status_hero.height()
        self.assertTrue(panel.detail_slot.isVisible())

        panel.render(UpdateSnapshot(ui_state="up_to_date",latest_version="0.2.4",verified_manifest=True)); self.app.processEvents()
        self.assertFalse(panel.detail_slot.isVisible())
        self.assertEqual(panel.detail_slot.height(),0)
        for _ in range(6): self.app.processEvents()
        self.assertLess(panel.status_hero.height(),expanded_height)
        panel.close()

    def test_about_save_failure_runs_visible_reverse_animation_and_keeps_buddy(self):
        cfg=Config(); cfg.save_result=False
        panel=AboutUpdatePanel(
            cfg,UpdateController(),repository_url="repo",author_url="author",
            product_boundary="boundary",open_url=lambda _url:None,show_contact=lambda:None,
        )
        panel.resize(760,720); panel.show(); self.app.processEvents()
        self.assertTrue(panel.auto.isChecked())
        self.assertIs(panel.auto_title.buddy(),panel.auto)
        QTest.mouseClick(panel.auto,Qt.LeftButton)
        self.assertTrue(panel.auto.isChecked())
        QTest.qWait(55)
        self.assertGreater(panel.auto.position,0.0)
        self.assertLess(panel.auto.position,1.0)
        QTest.qWait(180)
        self.assertAlmostEqual(panel.auto.position,1.0,places=3)
        panel.close()

    def test_about_state_groups_keep_action_aligned_and_expand_only_real_details(self):
        controller=UpdateController()
        panel=AboutUpdatePanel(Config(), controller, repository_url="repo", author_url="author",
            product_boundary="本地优先", open_url=lambda _: None, show_contact=lambda: None)
        panel.resize(760, 720); panel.show()
        cases=(
            ("idle", "", "检查更新", False),
            ("checking_manual", "", "正在检查…", True),
            ("up_to_date", "", "检查更新", False),
            ("installed", "", "知道了", False),
            ("update_available", "0.2.5", "立即更新", True),
            ("downloading", "0.2.5", "暂停下载", True),
            ("verifying", "0.2.5", "正在校验…", True),
            ("ready", "0.2.5", "立即重启安装", True),
        )
        baseline_right=None
        compact_height=None
        for state, latest, button_text, has_detail in cases:
            with self.subTest(state=state):
                panel.render(UpdateSnapshot(ui_state=state, latest_version=latest, verified_manifest=True))
                for _ in range(6): self.app.processEvents()
                right=panel.primary.mapTo(panel.software_group, panel.primary.rect().topRight()).x()
                if baseline_right is None: baseline_right=right
                self.assertEqual(right, baseline_right)
                self.assertEqual(panel.primary.text(), button_text)
                self.assertEqual(panel.primary.accessibleName(), button_text)
                self.assertEqual(panel.detail_slot.isVisible(), has_detail)
                if state == "idle": compact_height=panel.status_hero.height()
                elif has_detail: self.assertGreater(panel.status_hero.height(), compact_height)
                else: self.assertEqual(panel.status_hero.height(), compact_height)
                self.assertTrue(panel.software_group.isAncestorOf(panel.auto))
                self.assertFalse(panel.info_group.isAncestorOf(panel.auto))
        panel.render(UpdateSnapshot(ui_state="update_available",latest_version="0.2.5",verified_manifest=True))
        self.assertEqual(panel.state_title.text(), "最新版本 v0.2.5")
        self.assertEqual(panel.status_dot._color, T.DANGER)
        panel.close(); controller.shutdown(0)

    def test_about_update_available_primary_reuses_download_controller_action(self):
        class Controller(UpdateController):
            def __init__(self):
                super().__init__(); self.download_calls=0
            def download(self): self.download_calls += 1
        c=Controller(); panel=AboutUpdatePanel(
            Config(),c,repository_url="repo",author_url="author",product_boundary="boundary",
            open_url=lambda _url: None,show_contact=lambda: None,
        )
        c.snapshot=UpdateSnapshot(ui_state="update_available",latest_version="0.2.5",verified_manifest=True)
        c.publish(); panel.primary.click()
        self.assertEqual(c.download_calls,1)

    def test_restart_without_manifest_first_click_rechecks_before_download(self):
        class Controller(UpdateController):
            def __init__(self):
                super().__init__()
                self.check_calls = 0
                self.download_calls = 0

            def check(self, *, manual):
                self.check_calls += int(manual)

            def download(self):
                self.download_calls += 1

        controller = Controller()
        controller.snapshot = UpdateSnapshot(
            state="idle",
            ui_state="check_error",
            error_code="manifest_recheck_required",
            verified_manifest=False,
            update_available=False,
        )
        panel = AboutUpdatePanel(
            Config(),
            controller,
            repository_url="repo",
            author_url="author",
            product_boundary="boundary",
            open_url=lambda _url: None,
            show_contact=lambda: None,
        )
        full_panel = UpdatePanel(Config(), controller)
        controller.publish()
        self.assertEqual(panel.primary.text(), "重新检查更新")
        self.assertEqual(full_panel.primary.text(), "重新检查更新")
        panel.primary.click()
        self.assertEqual(controller.check_calls, 1)
        self.assertEqual(controller.download_calls, 0)

        controller.snapshot = UpdateSnapshot(
            ui_state="update_available",
            latest_version="0.2.5",
            verified_manifest=True,
            update_available=True,
        )
        controller.publish()
        panel.primary.click()
        self.assertEqual(controller.download_calls, 1)

        controller.snapshot = UpdateSnapshot(
            ui_state="check_error",
            error_code="network",
        )
        controller.publish()
        self.assertEqual(panel.primary.text(), "重试")
        self.assertEqual(full_panel.primary.text(), "重试")

    def test_auto_update_preference_persists_and_rolls_back_visibly(self):
        cfg=Config(); panel=UpdatePanel(cfg,UpdateController())
        panel.auto.click(); self.app.processEvents()
        self.assertFalse(cfg.auto_update_enabled); self.assertEqual(cfg.save_calls,1)

        cfg.save_result=False
        panel.auto.click(); self.app.processEvents()
        self.assertFalse(cfg.auto_update_enabled); self.assertFalse(panel.auto.isChecked())
        self.assertIn("暂未保存",panel.message.text())

    def test_ready_active_gate_and_unsaved_gate(self):
        active=[True]; unsaved=[False]; c=UpdateController(active_work=lambda:active[0],unsaved_work=lambda:unsaved[0]); c.snapshot.ui_state="ready"; c.publish(); self.assertEqual(c.snapshot.ui_state,"ready_blocked"); self.assertEqual(c.install_gate(),"active")
        active[0]=False; c.publish(); self.assertEqual(c.snapshot.ui_state,"ready"); unsaved[0]=True; self.assertEqual(c.install_gate(),"unsaved")

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

    def test_downloading_known_and_unknown_total(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c)
        self.assertFalse(panel.progress.isTextVisible())
        c.snapshot=UpdateSnapshot(ui_state="downloading",downloaded=25,total=100); c.publish(); self.assertEqual(panel.progress.maximum(),100); self.assertEqual(panel.progress.value(),22); self.assertIn("25%",panel.progress_text.text())
        c.snapshot=UpdateSnapshot(ui_state="downloading",downloaded=10*1024*1024,total=0); c.publish(); self.assertEqual(panel.progress.maximum(),100); self.assertTrue(panel.progress.property("luluProgressUnknown")); self.assertEqual(panel.progress_text.text(),"正在下载… · 已完成 10.0 MB")
        c.snapshot=UpdateSnapshot(ui_state="downloading",downloaded=0,total=0); c.publish(); self.assertEqual(panel.progress.maximum(),100); self.assertTrue(panel.progress.property("luluProgressUnknown")); self.assertEqual(panel.progress_text.text(),"正在下载… · 已完成 0 B")
        c.snapshot=UpdateSnapshot(ui_state="verifying"); c.publish(); self.assertEqual(panel.progress.value(),94)
        c.snapshot=UpdateSnapshot(ui_state="preparing"); c.publish(); self.assertEqual(panel.progress.value(),97)
        c.snapshot=UpdateSnapshot(ui_state="ready"); c.publish(); self.assertEqual(panel.progress.value(),0)
        about=AboutUpdatePanel(Config(),c,repository_url="repo",author_url="author",product_boundary="boundary",open_url=lambda _url:None,show_contact=lambda:None); self.assertFalse(about.progress.isTextVisible())

    def test_binary_size_boundaries_include_zero(self):
        self.assertEqual((_size(0),_size(1),_size(1024),_size(1024*1024)),("0 B","1 B","1.0 KB","1.0 MB"))

    def test_nested_activity_gate_covers_microphone_and_runtime_card(self):
        class Running:
            def isRunning(self): return True
        class Recording:
            def is_recording(self): return True
        def window():
            return SimpleNamespace(tasks=[],_recorder_obj=None,_mixed_rec_active=False,_mixed_pending_stop=False,_system_rec_session=None,models_page=SimpleNamespace(has_active_work=lambda:False),library_page=None,voice_page=None,collection_page=None,live_clips_page=None,gsv_finetune_running=lambda:False)
        mic=window(); mic._recorder_obj=Recording(); self.assertTrue(MainWindow.has_active_work(mic))
        runtime=window(); runtime.models_page.has_active_work=lambda:True; self.assertTrue(MainWindow.has_active_work(runtime))
        storage=window(); storage.settings_page=SimpleNamespace(_storage_workers={"models":Running()}); self.assertTrue(MainWindow.has_active_work(storage))
        idle=window(); self.assertFalse(MainWindow.has_active_work(idle))

    def test_compact_actions_stay_in_one_row(self):
        panel=UpdatePanel(Config(),UpdateController()); panel.resize(390,700); panel.show(); self.app.processEvents(); self.assertEqual(panel.actions.direction(),QBoxLayout.LeftToRight)
        from src.version import __version__
        self.assertEqual(panel.badge.text(), "当前 " + __version__)
        panel.resize(700,700); self.app.processEvents(); self.assertEqual(panel.actions.direction(),QBoxLayout.LeftToRight)

    def test_check_error_copy_preserves_safe_reason(self):
        self.assertIn("安全校验",_check_error_message("unsafe_url"))
        self.assertIn("网络连接",_check_error_message("network"))
        c=UpdateController(); panel=UpdatePanel(Config(),c); c.snapshot=UpdateSnapshot(ui_state="check_error",error_code="unsafe_url"); c.publish()
        self.assertIn("安全校验",panel.message.text())

    def test_up_to_date_uses_exact_current_version_copy(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c); c.snapshot=UpdateSnapshot(ui_state="up_to_date",latest_version="0.2.4",latest_build="20260712.1",verified_manifest=True); c.publish()
        self.assertEqual(panel.state_title.text(),"当前已是最新版本")

    def test_dynamic_action_tiers_and_geometry(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c); panel.show(); self.app.processEvents()
        cases=(
            ("idle","secondary",True),
            ("up_to_date","secondary",True),
            ("checking_manual","secondary",False),
            ("check_error","secondary",True),
            ("update_available","primary",True),
            ("download_paused","primary",True),
            ("download_error","primary",True),
            ("downloading","secondary",True),
            ("ready","primary",True),
            ("ready_blocked","secondary",False),
        )
        baseline=None
        for state,tier,enabled in cases:
            with self.subTest(state=state):
                c.snapshot=UpdateSnapshot(ui_state=state,latest_version="0.2.5",verified_manifest=True); panel.render(c.snapshot); self.app.processEvents()
                self.assertEqual(panel.primary.property("tier"),tier)
                self.assertEqual(panel.primary.isEnabled(),enabled)
                self.assertEqual(panel.primary.height(),34)
                self.assertGreaterEqual(panel.primary.minimumWidth(),112)
                geometry=(panel.auto.size().toTuple(),panel.badge.height(),panel.primary.height(),panel.secondary.height(),panel.tertiary.height())
                baseline=baseline or geometry
                self.assertEqual(geometry,baseline)
        panel.close()

    def test_switch_keyboard_states_and_fixed_geometry(self):
        switch=UpdateSwitch(); switch.show(); self.app.processEvents()
        self.assertEqual(switch.size().toTuple(),(38,22))
        switch.setFocus(); before=switch.geometry(); QTest.keyClick(switch,Qt.Key_Space); self.app.processEvents()
        self.assertTrue(switch.isChecked()); self.assertEqual(switch.geometry(),before)
        QTest.mousePress(switch,Qt.LeftButton); self.assertEqual(switch.geometry(),before)
        QTest.mouseRelease(switch,Qt.LeftButton); self.assertFalse(switch.isChecked()); self.assertEqual(switch.geometry(),before)
        switch.setEnabled(False); self.assertFalse(switch.isChecked()); self.assertEqual(switch.geometry(),before)
        self.assertFalse(switch.grab().isNull()); switch.close()

    def test_button_hover_pressed_focus_disabled_geometry_stable(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c); panel.resize(760,240); panel.show(); panel.render(UpdateSnapshot(ui_state="up_to_date")); self.app.processEvents()
        button=panel.primary; button.blockSignals(True); baseline=button.geometry()
        QTest.mouseMove(button,button.rect().center()); self.app.processEvents(); self.assertEqual(button.geometry(),baseline)
        QTest.mousePress(button,Qt.LeftButton); self.app.processEvents(); self.assertEqual(button.geometry(),baseline)
        QTest.mouseRelease(button,Qt.LeftButton); button.setFocus(); self.app.processEvents(); self.assertEqual(button.geometry(),baseline)
        button.setEnabled(False); self.app.processEvents(); self.assertEqual(button.geometry(),baseline); panel.close()

    def test_checking_spinner_replaces_six_pixel_status_dot(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c); panel.show(); self.app.processEvents()
        c.snapshot=UpdateSnapshot(ui_state="checking_manual"); c.publish(); self.app.processEvents()
        self.assertTrue(panel.spinner.isVisible()); self.assertTrue(panel.spinner._timer.isActive()); self.assertFalse(panel.status_dot.isVisible()); self.assertEqual(panel.spinner.size().toTuple(),(14,14))
        c.snapshot=UpdateSnapshot(ui_state="up_to_date"); c.publish(); self.app.processEvents()
        self.assertFalse(panel.spinner.isVisible()); self.assertFalse(panel.spinner._timer.isActive()); self.assertTrue(panel.status_dot.isVisible()); self.assertEqual(panel.status_dot.size().toTuple(),(10,14)); panel.close()

    def test_switch_accessible_name_relation_click_tab_and_space(self):
        c=UpdateController(); panel=UpdatePanel(Config(),c); panel.show(); panel.activateWindow(); self.app.processEvents()
        geometry=panel.auto.geometry(); interface=QAccessible.queryAccessibleInterface(panel.auto); label_interface=QAccessible.queryAccessibleInterface(panel.auto_title)
        self.assertIsNotNone(interface); self.assertEqual(interface.role(),QAccessible.Role.CheckBox)
        self.assertEqual(interface.text(QAccessible.Text.Name),"自动检查更新")
        self.assertIn("每 24 小时",interface.text(QAccessible.Text.Description))
        state=interface.state(); self.assertTrue(state.focusable); self.assertTrue(state.checkable); self.assertEqual(bool(state.checked),panel.auto.isChecked())
        self.assertIs(panel.auto_title.buddy(),panel.auto)
        self.assertTrue(any(flag==QAccessible.RelationFlag.Label for _target,flag in interface.relations()))
        self.assertTrue(any(flag==QAccessible.RelationFlag.Labelled for _target,flag in label_interface.relations()))
        panel.auto.clearFocus(); self.assertTrue(panel.focusNextChild()); self.assertTrue(panel.auto.hasFocus())
        panel.auto.clearFocus(); QTest.mouseClick(panel.auto_title,Qt.LeftButton); self.app.processEvents(); self.assertTrue(panel.auto.hasFocus())
        checked=panel.auto.isChecked(); QTest.keyClick(panel.auto,Qt.Key_Space); self.app.processEvents(); self.assertEqual(panel.auto.isChecked(),not checked); self.assertEqual(panel.auto.geometry(),geometry)
        updated=QAccessible.queryAccessibleInterface(panel.auto).state(); self.assertEqual(bool(updated.checked),panel.auto.isChecked()); panel.close()

    def test_paper_qss_and_gatekeeper_scope(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme); c=UpdateController(); panel=UpdatePanel(Config(),c); c.snapshot.ui_state="ready"; c.publish(); self.assertTrue(panel.gatekeeper.isVisible() or not panel.isVisible()); self.assertIn(T.CARD,panel.styleSheet())


if __name__=="__main__": unittest.main()
