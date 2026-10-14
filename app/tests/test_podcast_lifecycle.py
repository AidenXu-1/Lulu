"""RSS parsing owns one real QThread until its finished signal is handled."""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.ui.main_window import MainWindow


class PodcastLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                self.app.processEvents()
                return
            time.sleep(0.002)
        self.fail("RSS worker did not settle")

    def test_duplicate_rss_trigger_keeps_one_worker_and_one_result(self):
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_fetch(url: str):
            calls.append(url)
            started.set()
            release.wait(1.5)
            return None

        window = MainWindow()
        try:
            window.link_input.setText("https://feeds.example.test/show.xml")
            with (
                patch("src.core.podcast.looks_like_feed", return_value=True),
                patch("src.core.podcast.fetch_feed", side_effect=fake_fetch),
            ):
                window._grab_link()
                self.assertTrue(started.wait(1.0))
                first = window._feed_worker
                self.assertIsNotNone(first)
                window._grab_link()
                self.assertIs(window._feed_worker, first)
                self.assertEqual(len(calls), 1)
                release.set()
                self._wait(lambda: window._feed_worker is None)

            self.assertEqual(len(calls), 1)
            self.assertEqual(len(window.tasks), 1)
            self.assertEqual(window.tasks[0].source, "https://feeds.example.test/show.xml")
        finally:
            release.set()
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_exit_settlement_keeps_reference_until_real_thread_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def fake_fetch(_url: str):
            started.set()
            release.wait(1.5)
            return None

        window = MainWindow()
        try:
            window.link_input.setText("https://feeds.example.test/slow.xml")
            with (
                patch("src.core.podcast.looks_like_feed", return_value=True),
                patch("src.core.podcast.fetch_feed", side_effect=fake_fetch),
            ):
                window._grab_link()
                self.assertTrue(started.wait(1.0))
                worker = window._feed_worker
                self.assertIsNotNone(worker)
                self.assertFalse(window._settle_registered_qthreads(10))
                self.assertIs(window._feed_worker, worker)
                self.assertTrue(worker.isInterruptionRequested())
                release.set()
                self._wait(lambda: window._feed_worker is None)

            self.assertTrue(window._settle_registered_qthreads(20))
            self.assertEqual(window.tasks, [])
        finally:
            release.set()
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
