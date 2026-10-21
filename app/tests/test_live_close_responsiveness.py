"""Actual MainWindow close path with blocked temporary-volume preparations."""
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from src.core.job_ledger.store import JobLedgerStore
from src.core.job_ledger.service import JobControlService
from src.core.live_recording import service as service_module
from src.core.live_recording.contracts import Observation
from tests import test_extraction_main_window as window_fixtures
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class LiveCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        window_fixtures.ExtractionMainWindowTests.setUpClass()

    def test_real_window_closes_before_slow_start_or_directory_returns(self):
        for operation in ('start', 'directory', 'automatic-disk', 'automatic-mkdir'):
            with self.subTest(operation=operation):
                fixture = window_fixtures.ExtractionMainWindowTests()
                fixture.setUp()
                main = fixture.main
                service = service_module.LiveService(fixture.root / 'live',
                    JobControlService(JobLedgerStore(fixture.root / 'ledger', FakeIntegrityAnchorStore())),
                    'fixture', provider=SimpleNamespace(check=lambda *_: Observation('offline')),
                    secret_read=lambda: self.fail('credentials accessed'), secret_write=lambda _: None,
                    disk_free=lambda _: 100 * 1024**3)
                output = fixture.root / 'recordings'
                output.mkdir(exist_ok=True)
                service.index.set_setting('directory', str(output))
                main.live_controller.dispatcher = service.handle
                entered, release = threading.Event(), threading.Event()
                original_directory = service_module.directory
                original_mkdir = Path.mkdir
                target = fixture.root / 'slow-volume'
                target.mkdir()
                def directory(path, **kwargs):
                    if str(path) == str(target):
                        entered.set()
                        release.wait(8)
                    return original_directory(path, **kwargs)
                def disk(_):
                    entered.set()
                    release.wait(8)
                    return 100 * 1024**3
                def mkdir(path, *args, **kwargs):
                    if operation == 'automatic-mkdir' and path.parent == output and path.name.startswith('recording-'):
                        entered.set()
                        release.wait(8)
                    return original_mkdir(path, *args, **kwargs)
                try:
                    main.live_controller.start()
                    fixture.until(lambda: main.live_controller.connected)
                    main._switch_page(9)
                    page = main.live_recording_page
                    fixture.until(lambda: bool(page.data))
                    service.stop_event.set()
                    service.scheduler.join(2)
                    hid = service.handle('add', dict(session=main.live_controller.session,
                        source='https://live.douyin.com/778899'))['host_id']
                    fixture.until(lambda: not service.checks)
                    page.refresh()
                    fixture.until(lambda: page.table.rowCount() == 1)
                    if operation.startswith('automatic'):
                        service.handle('start', dict(session=main.live_controller.session, host_id=hid))
                        service.provider.check = lambda *_: Observation('live')
                    with patch.object(service, 'disk_free', disk if operation in {'start', 'automatic-disk'} else service.disk_free), \
                            patch.object(service_module, 'directory', directory), \
                            patch.object(Path, 'mkdir', mkdir), \
                            patch('src.ui.main_window.dialogs.confirm', return_value=True), \
                            patch('src.ui.live_recording_page.QFileDialog.getExistingDirectory', return_value=str(target)):
                        if operation == 'start':
                            QTest.mouseClick(page.table.cellWidget(0, 6).findChild(QPushButton), Qt.LeftButton)
                        elif operation == 'directory':
                            page._directory()
                        else:
                            service._submit_check(hid, main.live_controller.session)
                        fixture.until(entered.is_set)
                        main.close()  # Deliver the native window close event, including safe-stop retries.
                        fixture.until(lambda: not main.isVisible(), seconds=2)
                        self.assertTrue(main.live_controller.settled)
                        self.assertTrue(service.closing)
                        self.assertFalse(service.busy())
                        if operation.startswith('automatic'):
                            # Normal exit retains the pre-existing monitor preference
                            # for next launch, while all current work is cancelled.
                            self.assertTrue(service._host(hid)['enabled'])
                            self.assertTrue(all(m['cancel'].is_set() for m in service.monitors.values()))
                        else:
                            self.assertFalse(service._host(hid)['enabled'])
                        self.assertEqual(service.control.list(), ())
                        self.assertFalse(release.is_set())
                        self.assertEqual(main._registered_qthreads(), [])
                finally:
                    release.set()
                    fixture.until(lambda: not service.prepare_reads)
                    main.live_controller.request_stop()
                    fixture.until(lambda: main.live_controller.settled)
                    service.shutdown()
                    fixture.tearDown()
