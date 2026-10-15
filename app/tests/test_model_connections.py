from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from tests import test_agent_workspace
from src.core import model_connections as mc, secrets
from src.core.agent_workspace import providers


class ModelConnectionTests(unittest.TestCase):
    def setUp(self):
        test_agent_workspace.AgentWorkspaceTests.setUp(self)
        self.workspace = self.service
        self.cfg = SimpleNamespace(engine_name='local_whisper_mlx', save=Mock(return_value=True))
        self.service = mc.Service(self.cfg, store=self.workspace.store)
        self.keys = {}
        def config(pid, **_):
            raw = self.keys.get(pid, '')
            try:
                value = json.loads(raw)
                return value if isinstance(value, dict) else {'api_key': raw}
            except ValueError:
                return {'api_key': raw} if raw else {}
        for p in (patch.object(secrets, 'set_key_verified', side_effect=lambda pid, key: self.keys.__setitem__(pid, key)),
                  patch.object(secrets, 'get_key', side_effect=lambda pid, **_: self.keys.get(pid)),
                  patch.object(secrets, 'get_config', side_effect=config)):
            p.start()
            self.addCleanup(p.stop)

    def args(self, **kwargs):
        return dict(dict(purpose='assistant', provider='deepseek', mode='api',
                         base='https://api.deepseek.com', protocol='openai', region=0,
                         credentialId='', key='new-fixture-secret',
                         modelIds=['deepseek-flash', 'deepseek-v4-pro'], defaultModel='deepseek-flash'), **kwargs)

    def save(self, **kwargs):
        return self.service.execute('save_connection', self.args(**kwargs))

    def remove(self, credential_id, **changes):
        args = {k: v for k, v in self.args(key='', credentialId=credential_id, **changes).items()
                if k in {'purpose', 'provider', 'mode', 'base', 'protocol', 'region', 'credentialId'}}
        return self.service.execute('remove_credential', args)


    def transport(self, callback):
        constructor = httpx.Client
        def client(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            return constructor(transport=httpx.MockTransport(callback), **kwargs)
        return patch.object(httpx, 'Client', side_effect=client)


    def test_legacy_custom_asr_import_is_explicit_public_only_and_does_not_select(self):
        raw = json.dumps({'api_key': 'legacy-secret', 'base_url': 'https://legacy.invalid/v1', 'model': 'legacy-transcribe'})
        self.keys['custom_openai'] = raw
        with patch.object(secrets, '_cache', {}), patch.object(secrets, 'get_config', side_effect=AssertionError('no passive key read')):
            public = self.service.snapshot()['cloudConfigs']['custom_asr']
        self.assertTrue(public['requiresImport'])
        self.assertIsNone(public['configured'])
        with patch('httpx.Client', side_effect=AssertionError('no network')):
            imported = self.service.execute('load_saved_configuration', dict(purpose='extract', provider='custom_asr'))
        public = imported['cloudConfigs']['custom_asr']
        self.assertEqual(public['base'], 'https://legacy.invalid/v1')
        self.assertEqual(public['modelId'], 'legacy-transcribe')
        self.assertTrue(public['configured'])
        self.assertFalse(public['requiresImport'])
        self.assertNotIn('legacy-secret', json.dumps(imported))
        self.assertEqual(self.keys['custom_openai'], raw)
        self.assertEqual(self.cfg.engine_name, 'local_whisper_mlx')
        self.cfg.save.assert_not_called()
        restarted = mc.Service(self.cfg, store=self.workspace.store)
        with patch.object(secrets, 'get_config', side_effect=AssertionError('public copy after restart')):
            self.assertEqual(restarted.snapshot()['cloudConfigs']['custom_asr'], public)


    def test_multiselect_save_is_idempotent_and_preserves_old_conversation_snapshot(self):
        result = self.save()
        cid = result['credentialId']
        old = deepcopy(providers.connections(self.workspace.store))
        conversation = self.workspace.new_conversation(self.eid, 'old-chat')
        self.assertEqual(conversation['connection_id'], result['defaultConnectionId'])
        repeated = self.save(key='', credentialId=cid)
        self.assertEqual(result['connections'], repeated['connections'])
        self.save(key='', credentialId=cid, modelIds=['deepseek-v4-pro'], defaultModel='deepseek-v4-pro')
        self.assertEqual(len(self.workspace.connections()), 1)
        self.assertEqual(providers.connections(self.workspace.store), old)
        self.assertEqual(self.workspace.store.get('old-chat')['connection_id'], result['defaultConnectionId'])
        self.assertEqual(providers.connection(result['defaultConnectionId'], self.workspace.store), old[0])


    def test_connection_failure_never_echoes_server_body_or_secret_and_does_not_save(self):
        with self.transport(lambda _: httpx.Response(401, text='new-fixture-secret-private-server-body')):
            with self.assertRaises(RuntimeError) as raised:
                self.service.execute('test_connection', self.args())
        self.assertIn('401', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))
        self.assertEqual(self.keys, {})


if __name__ == '__main__':
    unittest.main()
