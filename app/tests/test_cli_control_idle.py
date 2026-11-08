"""Idle management reads retain prompt commands and authoritative busy state."""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from src.ui import cli_control
from tests.test_cli_expansion_ui import ControlWindowTests


class PendingPoll(QObject):
    result = Signal(object, str)
    finished = Signal()

    def __init__(self, nonce, reports, snapshot):
        super().__init__()
        self.reports, self.snapshot = reports, snapshot

    def start(self): pass
    def isRunning(self): return False

    def settle(self, *, busy=False, error=''):
        self.result.emit(dict(actions=[], core_busy=busy, busy_models=['fixture'] if busy else []), error)
        self.finished.emit()


class ControlIdleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ControlWindowTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        (root/'library').mkdir(mode=0o700)
        self.clock = 100.0
        for target, value in (
            ('src.core.library._dir', lambda: root/'library'),
            ('src.core.models_manager.app_data_dir', lambda: root/'product'),
            ('src.ui.cli_control._PollWorker', PendingPoll),
            ('src.ui.cli_control.time', SimpleNamespace(monotonic=lambda: self.clock)),
        ):
            context = patch(target, value); context.start(); self.addCleanup(context.stop)
        self.addCleanup(self.control.shutdown)
        # Use the owning storage implementations so a duplicated guess about
        # their paths cannot make both the watcher and its fixture look valid.
        from src.core.local_integrity import product_local_store, TASK_DIRECTORY
        from src.core.job_ledger.storage_binding import prepare_core_task_store
        from src.core.agent_workspace.store import WorkspaceStore
        product = root/'product'; product.mkdir(mode=0o700)
        keys = product_local_store(product)
        task_root = product/TASK_DIRECTORY; task_root.mkdir(mode=0o700)
        self.jobs = prepare_core_task_store(task_root, 'idle-fixture', keys, legacy_storage=False)
        self.ledger = self.jobs.directory
        self.store = WorkspaceStore()
        self.database = self.store.path
        self.workspace = self.database.parent

    def new_job(self):
        from tests.test_phase5_job_ledger import _create_arguments
        self.jobs.create_job(**dict(_create_arguments(), installation_id='idle-fixture'))

    def poll(self, seconds=1.5):
        self.clock += seconds
        self.control.poll()
        return self.control.worker

    def settle_first(self):
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()

    def wait_dirty(self):
        deadline = time.monotonic()+2
        while not self.control._poll_dirty and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.01)
        self.assertTrue(self.control._poll_dirty)

    def test_idle_reads_are_bounded_and_renew_before_core_lease_expires(self):
        self.settle_first()
        for _ in range(4): self.assertIsNone(self.poll())
        renewal = self.poll(); self.assertIsNotNone(renewal)
        self.assertLess(self.clock-101.5, 15)
        renewal.settle()

    def test_database_command_and_new_job_events_restore_next_tick_read(self):
        self.settle_first()
        for change in (lambda: self.store.preference('control:fixture', {'state': 'pending'}), self.new_job):
            change()
            self.wait_dirty()
            worker = self.poll(); self.assertIsNotNone(worker); worker.settle()

    def test_event_during_pending_reply_is_not_consumed_by_old_reply(self):
        worker = self.poll(); self.assertIsNotNone(worker)
        self.new_job()
        self.wait_dirty()
        worker.settle()
        following = self.poll(); self.assertIsNotNone(following); following.settle()

    def test_active_work_changed_settings_reports_and_errors_keep_fast_reads(self):
        worker = self.poll(); worker.settle(busy=True)
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        self.window.cfg.prefer_subtitles = not self.window.cfg.prefer_subtitles
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        self.control._report('fixture', 'succeeded', {})
        worker = self.poll(); self.assertIsNotNone(worker)
        self.assertEqual(worker.reports[0]['request_id'], 'fixture')
        worker.settle(error='temporary failure')
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        self.assertFalse(self.control.reports)

    def test_missing_or_failed_watches_fall_back_and_replaced_database_reconnects(self):
        self.settle_first()
        self.database.rename(self.workspace/'old.sqlite3')
        self.wait_dirty()
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        (self.workspace/'old.sqlite3').rename(self.database)
        self.app.processEvents()
        worker = self.poll(); self.assertIsNotNone(worker); worker.settle()
        self.assertIn(str(self.database), self.control._poll_watch.files())
        self.control._poll_watch.removePaths(self.control._poll_watch.files())
        with patch.object(self.control._poll_watch, 'addPaths', return_value=[str(self.database)]):
            for _ in range(2):
                worker = self.poll(); self.assertIsNotNone(worker); worker.settle()


if __name__ == '__main__': unittest.main()
