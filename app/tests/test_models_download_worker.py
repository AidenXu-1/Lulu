from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core import models_manager as mm, model_transfer as transfer
from PySide6.QtWidgets import QApplication
from src.ui.model_settings.workers import DownloadWorker


class ModelsDownloadWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.partial = self.root / 'weights.incomplete'
        def prepare(session, _model_id):
            session.check()
            self.session = session
            session.snapshots.append(transfer.Snapshot('repo', 'revision', self.root, [
                transfer.File(self.root/'weights', 2_000_000, 'unused', partials=(self.partial,)),
            ]))
        for p in (patch.object(transfer, 'prepare_asr', side_effect=prepare),
                  patch.object(mm, 'is_downloaded', return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_worker_forwards_true_download_progress_and_finishes(self):
        seen, finished = [], []
        def download(_model_id, *, progress, is_cancelled):
            self.assertFalse(is_cancelled())
            self.partial.write_bytes(b'x' * 1_000_000)
            with self.session.downloading():
                time.sleep(.12)
        worker = DownloadWorker('local_whisper_mlx', observation_interval=.05)
        worker.progress_detail.connect(lambda _message, fraction: seen.append(fraction))
        worker.finished_ok.connect(lambda: finished.append(True))
        with patch.object(mm, 'download_blocking', side_effect=download):
            worker.run()
            self.app.processEvents()
        self.assertIn(.5, seen)
        self.assertNotIn(1.0, seen)  # completion is not a fabricated byte measurement
        self.assertEqual(finished, [True])

    def test_worker_cancellation_reaches_download_operation(self):
        cancelled = []
        worker = DownloadWorker('local_whisper_mlx')
        worker.cancelled.connect(lambda: cancelled.append(True))
        worker.request_cancel()
        with patch.object(mm, 'download_blocking') as download:
            worker.run()
        self.assertEqual(cancelled, [True])
        download.assert_not_called()

    def test_worker_reports_real_retained_bytes_and_speed_while_backend_is_quiet(self):
        details = []
        self.partial.write_bytes(b'x' * 500_000)
        def download(_model_id, *, progress, is_cancelled):
            with self.session.downloading():
                time.sleep(.08)
                self.partial.write_bytes(b'x' * 1_000_000)
                time.sleep(.08)
        worker = DownloadWorker('local_whisper_mlx', observation_interval=.05)
        worker.progress_detail.connect(lambda message, fraction: details.append((message, fraction)))
        with patch.object(mm, 'download_blocking', side_effect=download):
            worker.run()
            self.app.processEvents()
        self.assertTrue(any('MB' in m and 'MB/s' in m for m, _ in details))
        self.assertTrue(any(fraction == .5 for _, fraction in details))
        self.assertTrue(all('约' not in m for m, _ in details))


if __name__ == '__main__':
    unittest.main()
