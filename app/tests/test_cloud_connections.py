"""Beginner setup flows against isolated stores; never access a real credential."""
import json
import unittest
from unittest.mock import patch
from tests import test_agent_workspace as core
from src.core.agent_workspace import providers


class ConnectionCoreTests(unittest.TestCase):
    setUp=core.AgentWorkspaceTests.setUp

    def value(self, **changes):
        return dict(dict(provider='deepseek', mode='api', base='https://api.deepseek.com', protocol='openai',
            model='deepseek-flash', window=1048576, max_output=4096, threshold=.8,
            credential_id='workspace-key-fixture', label='DeepSeek V4.1 Flash'), **changes)

    def test_save_retry_is_idempotent_but_changed_connections_are_snapshots(self):
        first=providers.save_connection(self.value(), self.service.store)
        self.assertEqual(first, providers.save_connection(self.value(), self.service.store))
        chat=self.service.new_conversation(self.eid,'chat')
        self.service.select_connection(chat['id'],first['id'])
        second=providers.save_connection(self.value(credential_id='workspace-key-other'), self.service.store)
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(len(providers.connections(self.service.store)),2)
        self.assertEqual(self.service.store.get(chat['id'])['connection_id'],first['id'])

    def test_supported_plan_regions_and_unsupported_plan_policy_agree(self):
        data=providers.catalog()
        for p in data['providers']:
            for mode in p['plans']:
                if mode.get('available') is False:
                    with self.subTest(provider=p['id']), self.assertRaisesRegex(ValueError,'暂'):
                        providers.save_connection(self.value(provider=p['id'],mode=mode['id'],base=mode['base']),self.service.store)
        p=next(p for p in data['providers'] if p['id']=='minimax')
        mode=p['plans'][0]
        self.assertIn('https://api.minimax.io/anthropic',providers._bases(p,mode))
        for mode in ['token-personal','token-team']:
            c=providers.save_connection(self.value(provider='qwen',mode=mode,base='https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',model='qwen3.8-flash',window=1000000),self.service.store)
            self.assertEqual(c['max_input'],983616)


class ConnectionUiTests(unittest.TestCase):
    def test_v2_cloud_form_scopes_credentials_and_preserves_existing_connections(self):
        from tests.model_settings_frontend_probe import run_probe
        receipt=run_probe()
        self.assertEqual(receipt['checks'],'passed')
        self.assertEqual(receipt['model_list_height'],180)
        self.assertEqual(receipt['errors'],[])

if __name__=='__main__':unittest.main()
