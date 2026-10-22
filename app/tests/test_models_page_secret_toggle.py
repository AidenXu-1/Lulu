"""V2 credentials, drafts, accessibility and layout in isolated Chromium."""
import unittest
from tests.model_settings_frontend_probe import run_probe

class ModelsPageFrontendTests(unittest.TestCase):

    def test_progress_refresh_keeps_both_model_card_clicks_and_download_controls(self):
        receipt=run_probe()['pointer_checks']
        self.assertEqual(receipt,dict(clicks=20,pages=2,native_navigation_requests=0,progress_and_actions=True))

    def test_repeated_new_key_starts_empty_and_discards_abandoned_reply(self):
        self.assertEqual(run_probe()['credential_checks'],dict(repeated_new_key=True,abandoned_reply=True))
