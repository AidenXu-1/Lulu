"""Approved inline model information is read-only and supplied by public metadata."""
from unittest.mock import patch
from tests.model_settings_fixtures import ControllerCase, m

class ModelInformationTests(ControllerCase):
    def test_model_descriptions_do_not_create_installers_or_read_secrets(self):
        with self.public_state(),patch.object(m,'TTSRuntimeInstallWorker') as install,patch('src.core.secrets.get_config',side_effect=AssertionError('secret read')),patch('src.core.secrets.get_key',side_effect=AssertionError('secret read')):
            value=self.c._read_state()
        install.assert_not_called();self.assertEqual(self.c.owned_workers(),[])
        for model in value['localModels']['extract']+value['localModels']['voice']:
            self.assertTrue(model['description']);self.assertTrue(model['logo'])
            self.assertNotIn('最低配置',model['description'])
        self.assertEqual([f['id'] for f in value['voiceFeatures']],['clone','preset','design'])
