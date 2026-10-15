"""V2 state reads and installs remain owned until the actual thread exits."""
import threading
from PySide6.QtCore import QThread
from tests.model_settings_fixtures import ControllerCase, m

class ModelsPageOwnedWorkerTests(ControllerCase):
    def test_existing_tts_with_missing_environment_offers_repair(self):
        with self.public_state(status='runtime_only', is_installed=True):
            state = self.c._read_state()
        for model in state['localModels']['voice']:
            self.assertTrue(model['repair'])
            self.assertFalse(model['ready'])
        for feature in state['voiceFeatures']:
            self.assertTrue(feature['installed'])
            self.assertFalse(feature['ready'])
