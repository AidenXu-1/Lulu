"""Offscreen real buttons and worker signals with isolated installation state."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication

from src.core.updater.models import UpdateSnapshot
from src.core.updater.state import StateStore
from src.core.updater.constants import MANUAL_DOWNLOAD_URL
from src.ui.update_ui import UpdateController, UpdatePanel


class UpdateInstallUiTests(unittest.TestCase):
    def setUp(self):
        self.app=QApplication.instance() or QApplication([])
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=StateStore(Path(temp.name).resolve()/'updates')
        self.store.save(UpdateSnapshot(state='ready_to_install',ui_state='ready'))
        self.service=SimpleNamespace(store=self.store,snapshot=self.store.load())
        self.controller=UpdateController(service=self.service)
        self.controller._result_timer.stop()
        cfg=SimpleNamespace(auto_update_enabled=False,save=lambda:None)
        self.panel=UpdatePanel(cfg,self.controller)
        self.panel.install_requested.connect(self.controller.launch_install)
        self.addCleanup(self.panel.close)
        self.addCleanup(self.controller._install_timer.stop)

    def settle_until(self, condition):
        end=time.monotonic()+3
        while not condition() and time.monotonic()<end:
            self.app.processEvents();time.sleep(.01)
        self.assertTrue(condition())

    def test_real_button_stays_responsive_and_only_ack_requests_exit(self):
        entered,release=threading.Event(),threading.Event()
        session=SimpleNamespace(poll=Mock(return_value='waiting'),plan={'txn':'a'*64},child=Mock())
        session.child.poll.return_value=None
        def prepare(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('fixture timeout')
            self.store.save(UpdateSnapshot(state='installing',ui_state='preparing',txn='a'*64))
            return session
        requested=Mock();self.controller.install_accepted.connect(requested)
        with patch('src.core.updater.launch.prepare_launch',side_effect=prepare) as prepare_mock:
            try:
                self.panel.primary.click()
                self.assertEqual(self.controller.snapshot.ui_state,'preparing')
                self.assertTrue(entered.wait(1))
                self.panel.primary.click()
                self.app.processEvents();requested.assert_not_called()
                release.set()
                self.settle_until(lambda:self.controller._install_launch is session)
                self.controller._poll_install();requested.assert_not_called()
                session.poll.return_value='accepted'
                self.settle_until(lambda: requested.call_count == 1)
                self.controller._poll_install();requested.assert_called_once()
                self.assertEqual(prepare_mock.call_count,1)
            finally:
                release.set();self.controller.shutdown(3000);self.app.processEvents()

    def test_cached_manual_link_goes_to_latest_release(self):
        self.controller.snapshot=UpdateSnapshot(state='failed',ui_state='rollback_failed',manual_url='https://github.com/AidenXu-1/Lulu/releases/tag/v4.1.5')
        self.controller.publish()
        opened=Mock();self.panel.manual_download.connect(opened)
        self.panel.primary.click()
        opened.assert_called_once_with(MANUAL_DOWNLOAD_URL)

    def test_unknown_install_result_is_retryable_not_rollback_failure(self):
        self.controller.snapshot=UpdateSnapshot(state='failed',ui_state='install_interrupted')
        self.controller.check=Mock();self.controller.publish()
        self.panel.primary.click()
        self.controller.check.assert_called_once_with(manual=True)

    def test_real_receipt_poll_keeps_ui_responsive_during_slow_process_query(self):
        import subprocess
        from PySide6.QtCore import QTimer
        from src.core.updater import launch, helper_protocol, process_identity
        from src.core.updater.install import InstallTransaction
        from src.core.updater.archive import app_file_manifest
        from tests import test_auto_update as fixtures
        root = self.store.root.parent
        _, target, staged = fixtures.AutoUpdateTests._apps(self, root)
        tx = InstallTransaction(self.store.root, allowed_targets={target}, verify_system=False)
        executable = str(target/'Contents/MacOS/Lulu')
        path, plan = tx.create_plan(target=target, staged_app=staged, version='0.2.4',
            build='20260712.1', old_pid=123, old_pid_start_time='start',
            old_executable_path=executable, staged_sha=app_file_manifest(staged)[1])
        with patch.object(helper_protocol, 'current_identity', return_value=('Mon Oct 12 01:41:41 2026', executable)):
            helper_protocol.accept(path, plan, tx)
        child = Mock(pid=os.getpid()); child.poll.return_value = None
        session = launch.InstallLaunch(tx, path, plan, child, time.monotonic()+30)
        self.controller._install_launch = session
        entered, release = threading.Event(), threading.Event()
        requested = Mock(); self.controller.install_accepted.connect(requested)
        ticks = []
        def slow_query(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError('fixture timeout')
            return subprocess.CompletedProcess([], 0, f'S Mon Oct 12 01:41:41 2026 {executable}\n', '')
        with patch.object(process_identity.subprocess, 'run', side_effect=slow_query):
            try:
                self.controller._poll_install()
                self.assertTrue(entered.wait(1))
                QTimer.singleShot(0, lambda: ticks.append(1))
                self.app.processEvents()
                self.assertEqual(ticks, [1]); requested.assert_not_called()
                release.set()
                self.settle_until(lambda: requested.call_count == 1)
            finally:
                release.set(); self.controller.shutdown(3000); self.app.processEvents()

    def test_startup_corrupt_recovery_has_visible_repair_exit_before_core(self):
        from src import main
        from src.core.updater.models import UpdateError
        with patch.object(main.sys, 'frozen', True, create=True), \
             patch.object(main.sys, 'argv', ['Lulu']), \
             patch('src.core.updater.restart_recovery.launch_pending_recovery', side_effect=UpdateError('plan','bad plan')), \
             patch('src.core.diagnostics.record_failure'), \
             patch.object(main, '_show_update_recovery_error', return_value=5) as visible, \
             patch('src.core.agent_entry.standalone_bootstrap.start_preparation') as core:
            self.assertEqual(main.main(), 5)
        visible.assert_called_once(); core.assert_not_called()

    def test_delayed_acceptance_from_exited_helper_never_closes_gui(self):
        session = SimpleNamespace(child=Mock(), plan={'txn':'a'*64})
        session.child.poll.return_value = 4
        worker = SimpleNamespace(launch=session, result='accepted', deleteLater=Mock())
        self.controller._install_launch = session
        self.controller._install_poll_worker = worker
        self.store.save(UpdateSnapshot(state='failed',ui_state='preparation_error',error_code='old_process_timeout'))
        requested = Mock(); self.controller.install_accepted.connect(requested)
        self.controller._install_polled(worker)
        requested.assert_not_called()
        self.assertEqual(self.controller.snapshot.error_code,'old_process_timeout')

    def test_recovery_child_failure_shows_repair_exit_but_live_helper_yields(self):
        from src import main
        for result in (0, 3, 4, 5):
            with self.subTest(result=result), \
                 patch.object(main.sys, 'argv', ['Lulu','--lulu-update-recover','plan','1','start','exe']), \
                 patch('src.core.updater.restart_recovery.run_recovery', return_value=result), \
                 patch.object(main, '_show_update_recovery_error', return_value=5) as visible:
                self.assertEqual(main.main(), result)
                self.assertEqual(visible.call_count, int(result == 5))
