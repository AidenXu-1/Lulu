import unittest

from src.config import AppConfig
from src.core.runtime_preferences import remember_top_level_page, startup_page_index


class _Cfg:
    def __init__(self):
        self.startup_page_mode = "legacy-last"
        self.last_top_level_page = 7
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        return True


class RuntimePreferencesTests(unittest.TestCase):
    def test_app_config_contains_only_the_confirmed_global_defaults(self):
        cfg = AppConfig()
        self.assertTrue(cfg.notify_on_failure)
        self.assertTrue(cfg.notification_sound_enabled)
        self.assertTrue(cfg.confirm_exit_during_tasks)
        self.assertEqual(cfg.completion_action, "stay")
        self.assertFalse(hasattr(cfg, "startup_page_mode"))
        self.assertFalse(hasattr(cfg, "last_top_level_page"))

    def test_startup_is_always_transcription_even_with_legacy_values(self):
        cfg = _Cfg()
        self.assertEqual(startup_page_index(cfg), 0)
        self.assertEqual(startup_page_index(None), 0)

    def test_navigation_compatibility_hook_is_a_noop(self):
        cfg = _Cfg()
        self.assertTrue(remember_top_level_page(cfg, 3))
        self.assertTrue(remember_top_level_page(cfg, 6))
        self.assertEqual(cfg.last_top_level_page, 7)
        self.assertEqual(cfg.save_calls, 0)


if __name__ == "__main__":
    unittest.main()
