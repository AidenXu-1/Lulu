"""V2 credentials, drafts, accessibility and layout in isolated Chromium."""
import unittest
from tests.model_settings_frontend_probe import run_probe

class ModelsPageFrontendTests(unittest.TestCase):
    def test_real_frontend_keeps_keys_scoped_and_uses_only_explicit_bridge_actions(self):
        receipt=run_probe()
        self.assertEqual(receipt['checks'],'passed');self.assertEqual(receipt['model_list_height'],180)
        self.assertEqual(receipt['errors'],[]);self.assertGreaterEqual(receipt['requests'],10)
