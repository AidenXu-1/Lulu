import unittest
from types import SimpleNamespace

from src.ui.main_window import MainWindow


class StartupPreferenceIntegrationTests(unittest.TestCase):
    def test_main_window_always_starts_on_transcription(self):
        shell = SimpleNamespace(
            cfg=SimpleNamespace(startup_page_mode="legacy-last", last_top_level_page=7)
        )
        self.assertEqual(MainWindow._startup_target_page(shell), 0)

    def test_navigation_does_not_persist_last_page(self):
        calls = []
        cfg = SimpleNamespace(last_top_level_page=0, save=lambda: calls.append("save") or True)
        shell = SimpleNamespace(cfg=cfg)
        self.assertTrue(MainWindow._remember_top_level_page(shell, 3))
        self.assertTrue(MainWindow._remember_top_level_page(shell, 6))
        self.assertEqual(cfg.last_top_level_page, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
