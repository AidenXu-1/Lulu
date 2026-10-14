import concurrent.futures
import unittest
from unittest.mock import Mock, patch

from src.core import secrets


class SecretSessionCacheTests(unittest.TestCase):
    def setUp(self):
        secrets._cache.clear()
        secrets._memory.clear()
        self.addCleanup(secrets._cache.clear)
        self.addCleanup(secrets._memory.clear)

    def test_concurrent_feature_reads_request_the_same_credential_only_once(self):
        backend = Mock()
        backend.get_password.return_value = '{"api_key":"test-only"}'
        with patch.object(secrets, '_backend', return_value=backend):
            self.assertFalse(secrets.configuration_loaded('summary_llm'))
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(secrets.get_config, ['summary_llm'] * 8))
            self.assertEqual(results, [{'api_key': 'test-only'}] * 8)
            backend.get_password.assert_called_once_with('Lulu', 'summary_llm')

    def test_save_and_delete_immediately_replace_cached_values(self):
        backend = Mock()
        backend.get_password.return_value = 'old-test-value'
        with patch.object(secrets, '_backend', return_value=backend):
            self.assertEqual(secrets.get_key('test'), 'old-test-value')
            secrets.set_key('test', 'new-test-value')
            self.assertEqual(secrets.get_key('test'), 'new-test-value')
            secrets.delete_key('test')
            self.assertIsNone(secrets.get_key('test'))
            self.assertEqual(backend.get_password.call_count, 1)
            backend.delete_password.assert_called_once_with('Lulu', 'test')

    def test_denied_read_is_not_cached_as_missing_configuration(self):
        backend = Mock()
        backend.get_password.side_effect = [RuntimeError('denied'), 'test-only']
        with patch.object(secrets, '_backend', return_value=backend):
            self.assertIsNone(secrets.get_key('test'))
            self.assertFalse(secrets.configuration_loaded('test'))
            self.assertEqual(secrets.get_key('test'), 'test-only')

    def test_editing_configuration_distinguishes_denied_access_from_missing_key(self):
        backend = Mock()
        backend.get_password.side_effect = RuntimeError('private error details')
        with patch.object(secrets, '_backend', return_value=backend):
            with self.assertRaisesRegex(RuntimeError, '^CREDENTIAL_READ_UNAVAILABLE$'):
                secrets.get_config('test', require_access=True)
            self.assertFalse(secrets.configuration_loaded('test'))
