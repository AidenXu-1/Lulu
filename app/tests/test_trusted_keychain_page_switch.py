import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.config import AppConfig
from src.core import library, secrets
from src.core.synthesis import glossary, style_presets
from src.ui import voice_page as voice_page_module
from src.ui.main_window import MainWindow
from src.ui.voice_page import VoicePage
from tests.test_voice_page_model_shell import _FakeAudioOutput, _FakeMediaPlayer


class TrustedKeychainPageSwitchTests(unittest.TestCase):
    def setUp(self):
        loaded = patch("src.core.secrets.configuration_loaded", return_value=True)
        loaded.start()
        self.addCleanup(loaded.stop)
        secrets._cache.clear()
        self.addCleanup(secrets._cache.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = QApplication.instance() or QApplication([])

        self.old_config_load = AppConfig.__dict__["load"]
        self.old_config_save = AppConfig.save
        AppConfig.load = classmethod(lambda _cls: AppConfig())
        AppConfig.save = lambda _self: True  # type: ignore[assignment]

        self.old_library_dir = library.LIBRARY_DIR
        library.LIBRARY_DIR = self.root / "library"
        self.old_secret_backend = secrets._backend
        secrets._backend = self._real_secret_forbidden  # type: ignore[assignment]

        self.old_style_path = style_presets.style_presets_path
        self.old_glossary_path = glossary.glossary_path
        style_presets.style_presets_path = (  # type: ignore[assignment]
            lambda: self.root / "synthesis_style_presets.json"
        )
        glossary.glossary_path = (  # type: ignore[assignment]
            lambda: self.root / "synthesis_glossary.json"
        )
        self.old_voice_refresh = VoicePage._refresh_indextts2_status
        VoicePage._refresh_indextts2_status = (  # type: ignore[assignment]
            lambda _self, force=False: None
        )
        self.old_media_player = voice_page_module.QMediaPlayer
        self.old_audio_output = voice_page_module.QAudioOutput
        voice_page_module.QMediaPlayer = _FakeMediaPlayer  # type: ignore[assignment]
        voice_page_module.QAudioOutput = _FakeAudioOutput  # type: ignore[assignment]

    def tearDown(self):
        voice_page_module.QAudioOutput = self.old_audio_output  # type: ignore[assignment]
        voice_page_module.QMediaPlayer = self.old_media_player  # type: ignore[assignment]
        VoicePage._refresh_indextts2_status = self.old_voice_refresh  # type: ignore[assignment]
        style_presets.style_presets_path = self.old_style_path  # type: ignore[assignment]
        glossary.glossary_path = self.old_glossary_path  # type: ignore[assignment]
        secrets._backend = self.old_secret_backend  # type: ignore[assignment]
        library.LIBRARY_DIR = self.old_library_dir
        AppConfig.save = self.old_config_save  # type: ignore[assignment]
        AppConfig.load = self.old_config_load  # type: ignore[assignment]
        self.tmp.cleanup()

    @staticmethod
    def _real_secret_forbidden():
        raise AssertionError("trusted page-switch tests must not reach real Keychain")

    def _wait_until(self, predicate, *, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("timed out waiting for Qt background result")

    def test_opening_local_and_text_pages_never_reads_credentials_for_hidden_services(self):
        with patch("src.core.secrets._backend", return_value=None) as backend, \
             patch("src.core.secrets.configuration_loaded", return_value=False):
            window = MainWindow()
            try:
                for index in (0, 3, 7, 8, 4, 0, 4, 3):
                    window._switch_page(index)
                    QTest.qWait(20)
                backend.assert_not_called()
                self.assertFalse(hasattr(window.voice_page, "_minimax_config_check_thread"))
            finally:
                window.close()


    def test_voice_page_minimax_keychain_check_is_background_and_coalesced(self):
        entered = threading.Event()
        release = threading.Event()
        calls: list[threading.Thread] = []

        class BlockingBackend:
            def get_password(self, _service, _provider):
                calls.append(threading.current_thread())
                entered.set()
                release.wait(2.0)
                return None

        secrets._backend = lambda: BlockingBackend()  # type: ignore[assignment]
        window = MainWindow()
        started = time.perf_counter()
        window._switch_page(4)
        page = window.voice_page
        self.assertIsNone(page._minimax_config_check_thread)
        page._switch_panel(next(i for i in range(page.panel_stack.count())
                                if page.panel_stack.widget(i).objectName() == "minimaxModelPanel"))
        elapsed = time.perf_counter() - started

        self.assertLessEqual(elapsed, 0.250)
        self.assertTrue(entered.wait(1.0))
        page = window.voice_page
        self.assertIsNotNone(page)
        self.assertEqual(page._minimax_config_state, "checking")
        self.assertEqual(page.minimax_ready_chip.text(), "● 检查中")
        self.assertFalse(page.btn_minimax_generate.isEnabled())
        self.assertFalse(window.has_active_work())
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0], threading.main_thread())

        worker = page._minimax_config_check_thread
        page.refresh()
        page._reload_minimax_voice_options()
        self.app.processEvents()
        self.assertIs(page._minimax_config_check_thread, worker)
        self.assertEqual(len(calls), 1)

        release.set()
        self._wait_until(
            lambda: (
                page._minimax_config_state == "unconfigured"
                and page._minimax_config_check_thread is None
            )
        )
        self.assertEqual(page.minimax_ready_chip.text(), "● 未配置")
        window.close()
        self.assertIsNone(page._minimax_config_check_thread)

    def test_close_waits_for_blocked_minimax_config_worker(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingBackend:
            def get_password(self, _service, _provider):
                entered.set()
                release.wait(2.0)
                return None

        secrets._backend = lambda: BlockingBackend()  # type: ignore[assignment]
        window = MainWindow()
        window._switch_page(4)
        page = window.voice_page
        page._switch_panel(next(i for i in range(page.panel_stack.count())
                                if page.panel_stack.widget(i).objectName() == "minimaxModelPanel"))
        self.assertTrue(entered.wait(1.0))
        page = window.voice_page
        self.assertIsNotNone(page)
        self.assertIsNotNone(page._minimax_config_check_thread)

        timer = threading.Timer(0.05, release.set)
        timer.start()
        started = time.perf_counter()
        window.close()
        timer.join()

        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertIsNone(page._minimax_config_check_thread)


if __name__ == "__main__":
    unittest.main()
