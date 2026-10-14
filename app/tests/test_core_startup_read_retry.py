import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from src.core.agent_entry.native_transport import XpcTransportError
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.ui.worker import CoreTaskStartupWorker


class CoreStartupReadRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_worker(self, worker):
        ready, failed = [], []
        worker.ready.connect(lambda *value: ready.append(value))
        worker.failed.connect(failed.append)
        with patch('src.core.diagnostics.record_failure'), patch.object(worker, 'msleep'):
            worker.run()
        return ready, failed

    def test_timeout_reopens_verified_factory_and_only_reads_snapshot(self):
        snapshot = object()
        first = SimpleNamespace(load=Mock(side_effect=XpcTransportError(21)))
        second = SimpleNamespace(load=Mock(return_value=snapshot))
        with patch.object(CoreTaskQueueStore, 'production', side_effect=[first, second]) as factory:
            ready, failed = self.run_worker(CoreTaskStartupWorker())
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(ready, [(second, snapshot)])
        self.assertEqual(failed, [])
        first.load.assert_called_once_with()
        second.load.assert_called_once_with()

    def test_retries_are_bounded_when_service_remains_unavailable(self):
        with patch.object(CoreTaskQueueStore, 'production', side_effect=XpcTransportError(20)) as factory:
            ready, failed = self.run_worker(CoreTaskStartupWorker())
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(ready, [])
        self.assertEqual(failed, ['CORE_TASK_SERVICE_UNAVAILABLE:connect:20'])

    def test_security_and_handler_failures_are_never_automatically_retried(self):
        for error in (XpcTransportError(11), XpcTransportError(30), RuntimeError('COMPONENT_EVIDENCE_REQUIRED')):
            with self.subTest(error=type(error).__name__, code=getattr(error, 'native_code', None)):
                with patch.object(CoreTaskQueueStore, 'production', side_effect=error) as factory:
                    ready, failed = self.run_worker(CoreTaskStartupWorker())
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(len(failed), 1)
                self.assertEqual(ready, [])

    def test_quit_during_timeout_does_not_reconnect_or_emit_ready(self):
        worker = CoreTaskStartupWorker()
        def interrupted():
            worker.requestInterruption()
            raise XpcTransportError(21)
        # QThread interruption is meaningful only while running; model the quit
        # flag directly while running this worker synchronously in the test.
        with patch.object(worker, 'isInterruptionRequested', side_effect=[False, True]), patch.object(CoreTaskQueueStore, 'production', side_effect=interrupted) as factory:
            ready, failed = self.run_worker(worker)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual((ready, failed), ([], []))
