import unittest
from unittest.mock import patch
import httpx
from tests import test_agent_workspace
from src.core.agent_workspace import providers


class ProviderTests(unittest.TestCase):
    setUp=test_agent_workspace.AgentWorkspaceTests.setUp
    def value(self,**kwargs):
        return dict(dict(provider='kimi',mode='api',base='https://api.moonshot.cn/v1',protocol='openai',model='kimi-k3',
                         window=1000000,max_output=4096,threshold=.8,credential_id='workspace-key-test',label='Kimi'),**kwargs)

    def test_discovered_capacity_is_bound_to_exact_endpoint_and_key(self):
        client=httpx.Client
        response={'data':[{'id':'kimi-k3','context_length':1200000}]}
        with patch.object(providers,'key_for',return_value='fixture'),patch.object(httpx,'Client',side_effect=lambda **kw:client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=response)),**kw)):
            rows=providers.discover(value={k:v for k,v in self.value().items() if k in {'provider','mode','base','protocol','credential_id'}},store=self.service.store)
        self.assertEqual(rows[0]['window'],1200000)
        c=providers.save_connection(self.value(window=1200000),self.service.store)
        self.assertEqual(c['window'],1200000)
        with self.assertRaises(ValueError):providers.save_connection(self.value(window=1200000,credential_id='workspace-key-other'),self.service.store)
        with self.assertRaises(ValueError):providers.save_connection(self.value(window=1200000,base='https://api.moonshot.ai/v1'),self.service.store)

    def test_official_input_limit_and_future_chats_use_explicit_default(self):
        c=providers.save_connection(self.value(provider='qwen',base='https://dashscope.aliyuncs.com/compatible-mode/v1',model='qwen3.8-max'),self.service.store)
        self.assertEqual(c['max_input'],983616)
        first=self.service.new_conversation(self.eid,'c1')
        self.service.select_connection(first['id'],c['id'])
        second=self.service.new_conversation(self.eid,'c2')
        self.assertEqual(second['connection_id'],c['id'])
        newer=providers.save_connection(self.value(),self.service.store)
        self.service.select_connection(second['id'],newer['id'])
        self.assertEqual(self.service.store.get(first['id'])['connection_id'],c['id'])
