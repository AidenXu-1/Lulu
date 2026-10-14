"""Listing configuration never opens Keychain; explicit connection actions may."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from src.core.model_connections import Service
from src.core import model_connections as c

class ModelCredentialsOnDemandTests(unittest.TestCase):
    def test_opening_settings_never_reads_saved_keys(self):
        store=Mock();store.preference.return_value=None
        service=Service(SimpleNamespace(engine_name='local_sensevoice'),store=store)
        with patch.object(c.workspace,'available_connections',return_value=[]),patch.object(c.secrets,'get_key',side_effect=AssertionError('unselected key')),patch.object(c.secrets,'get_config',side_effect=AssertionError('unselected config')):
            value=service.snapshot()
        self.assertEqual(value['connections'],[])
        self.assertTrue(value['providers']);self.assertTrue(value['asrProviders'])
        self.assertTrue(all(x.get('configured') is None for x in value['cloudConfigs'].values()))

    def test_existing_connection_snapshot_contains_only_opaque_handle(self):
        store=Mock();store.preference.return_value=None
        safe={'id':'connection-fixture','credential_id':'workspace-key-fixture','provider':'deepseek'}
        with patch.object(c.workspace,'available_connections',return_value=[safe]),patch.object(c.secrets,'get_key',side_effect=AssertionError('unselected key')):
            value=Service(SimpleNamespace(engine_name='local_sensevoice'),store=store).snapshot()
        self.assertEqual(value['connections'],[safe]);self.assertFalse({'api_key','key','secret'} & set(value['connections'][0]))
