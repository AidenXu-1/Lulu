from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from tests import test_agent_workspace
from src.core import model_connections as mc, secrets
from src.core.agent_workspace import providers


class VerifiedCredentialRemovalTests(unittest.TestCase):
    def test_verified_writer_deletes_the_record_and_clears_cached_copies(self):
        cid = 'workspace-key-verified-fixture'
        records = {cid: 'fixture-only'}
        backend = Mock()
        backend.get_password.side_effect = lambda service, account: records.get(account)
        backend.delete_password.side_effect = lambda service, account: records.pop(account, None)
        cache, memory = {cid: 'cached-fixture'}, {cid: 'memory-fixture'}
        with patch.object(secrets, '_backend', return_value=backend), \
                patch.object(secrets, '_cache', cache), patch.object(secrets, '_memory', memory):
            secrets.set_key_verified(cid, '')
            secrets.set_key_verified(cid, '')  # Retry is safe after actual removal.
        backend.delete_password.assert_called_once_with(secrets._SERVICE, cid)
        backend.set_password.assert_not_called()
        self.assertNotIn(cid, records)
        self.assertNotIn(cid, memory)
        self.assertIsNone(cache[cid])

    def test_verified_writer_rejects_a_delete_that_leaves_the_record_present(self):
        backend = Mock()
        backend.get_password.return_value = 'fixture-still-present'
        with patch.object(secrets, '_backend', return_value=backend), \
                patch.object(secrets, '_cache', {}), patch.object(secrets, '_memory', {}):
            with self.assertRaisesRegex(RuntimeError, '未能确认'):
                secrets.set_key_verified('workspace-key-verified-fixture', '')


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

    def test_remove_credential_deletes_the_key_but_preserves_historical_snapshots(self):
        first = self.save()
        old = deepcopy(providers.connections(self.workspace.store))
        chat = self.workspace.new_conversation(self.eid, 'preserved-chat')
        second = self.save(key='second-fixture-secret')
        self.assertEqual(len(self.service.snapshot()['storedConnections']), 4)
        with patch('httpx.Client', side_effect=AssertionError('deletion must not call the service')):
            removed = self.remove(second['credentialId'])
        self.assertFalse(self.keys.get(second['credentialId']))
        self.assertTrue(self.keys.get(first['credentialId']))
        self.assertEqual(providers.connections(self.workspace.store)[:2], old)
        self.assertEqual(self.workspace.store.get(chat['id'])['connection_id'], first['defaultConnectionId'])
        self.assertEqual(removed['connections'], [])
        self.assertEqual(len(removed['storedConnections']), 2)
        self.assertEqual(removed['defaultConnectionId'], '')
        self.assertNotIn('fixture-secret', json.dumps(removed))
        self.assertEqual(self.remove(second['credentialId'])['connections'], [])

    def test_remove_inactive_credential_keeps_the_active_models_and_default(self):
        old = self.save()
        current = self.save(key='replacement-fixture')
        removed = self.remove(old['credentialId'])
        self.assertEqual(removed['connections'], current['connections'])
        self.assertEqual(removed['defaultConnectionId'], current['defaultConnectionId'])
        self.assertEqual(len(removed['storedConnections']), 2)

    def test_remove_credential_rejects_other_provider_before_touching_secrets(self):
        saved = self.save()
        with patch.object(secrets, 'set_key_verified', side_effect=AssertionError('wrong scope')):
            with self.assertRaises(ValueError):
                self.remove(saved['credentialId'], provider='kimi', base='https://api.moonshot.cn/v1')
        self.assertEqual(self.workspace.connections(), saved['connections'])

    def test_failed_key_removal_never_reports_success_or_hides_the_connection(self):
        saved = self.save()
        with patch.object(secrets, 'set_key_verified', side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, '删除'):
                self.remove(saved['credentialId'])
        self.assertTrue(self.keys[saved['credentialId']])
        self.assertEqual(self.workspace.connections(), saved['connections'])

    def test_removed_credential_cannot_be_selected_or_saved_again(self):
        saved = self.save()
        self.remove(saved['credentialId'])
        with patch.object(secrets, 'get_key', side_effect=AssertionError('revoked handle')):
            with self.assertRaisesRegex(ValueError, '删除'):
                self.save(key='', credentialId=saved['credentialId'])
        # The old conversation still resolves its immutable model description.
        self.assertEqual(providers.connection(saved['defaultConnectionId'], self.workspace.store)['credential_id'], saved['credentialId'])

    def test_remove_selected_asr_credential_returns_to_local_and_keeps_other_keys(self):
        self.keys.update(openai='asr-fixture', other='untouched')
        self.cfg.engine_name = 'openai'
        removed = self.service.execute('remove_credential', dict(purpose='extract', provider='openai', credentialId='openai'))
        from src.config import DEFAULT_ENGINE
        self.assertEqual(self.cfg.engine_name, DEFAULT_ENGINE)
        self.assertFalse(self.keys.get('openai'))
        self.assertEqual(self.keys['other'], 'untouched')
        self.assertFalse(removed['cloudConfigs']['openai']['configured'])

    def test_asr_removal_aborts_before_deletion_when_local_preference_cannot_save(self):
        self.keys['openai'] = 'asr-fixture'
        self.cfg.engine_name = 'openai'
        self.cfg.save.return_value = False
        with patch.object(secrets, 'set_key_verified', side_effect=AssertionError('preserve original key')):
            with self.assertRaises(RuntimeError):
                self.service.execute('remove_credential', dict(purpose='extract', provider='openai', credentialId='openai'))
        self.assertEqual(self.cfg.engine_name, 'openai')

    def test_deleted_custom_asr_does_not_reopen_the_legacy_import_prompt(self):
        self.keys['custom_openai'] = json.dumps({'api_key': 'legacy-fixture', 'base_url': 'https://legacy.invalid/v1'})
        with patch.object(secrets, 'cached_public_config', side_effect=AssertionError('deleted legacy record')):
            removed = self.service.execute('remove_credential', dict(purpose='extract', provider='custom_asr', credentialId='custom_openai'))
            self.assertFalse(self.service.snapshot()['cloudConfigs']['custom_asr']['configured'])
        self.assertFalse(self.keys.get('custom_openai'))
        self.assertFalse(removed['cloudConfigs']['custom_asr']['hasCredential'])
        self.assertFalse(removed['cloudConfigs']['custom_asr']['requiresImport'])

    def test_asr_removal_restores_selection_when_preference_write_raises(self):
        self.keys['openai'] = 'asr-fixture'
        self.cfg.engine_name = 'openai'
        self.cfg.save.side_effect = OSError('disk unavailable')
        with patch.object(secrets, 'set_key_verified', side_effect=AssertionError('preserve original key')):
            with self.assertRaisesRegex(RuntimeError, '密钥未删除'):
                self.service.execute('remove_credential', dict(purpose='extract', provider='openai', credentialId='openai'))
        self.assertEqual(self.cfg.engine_name, 'openai')

    def test_metadata_failure_reports_actual_key_removal_and_can_retry(self):
        saved = self.save()
        original_connect = self.workspace.store.connect
        with patch.object(self.workspace.store, 'connect', side_effect=[original_connect(), OSError('disk unavailable')]):
            with self.assertRaisesRegex(RuntimeError, '密钥已从本机移除'):
                self.remove(saved['credentialId'])
        self.assertFalse(self.keys.get(saved['credentialId']))
        self.assertEqual(self.workspace.connections(), saved['connections'])
        self.assertEqual(self.remove(saved['credentialId'])['connections'], [])

    def test_removal_rejects_invalid_region_before_accessing_secrets(self):
        saved = self.save()
        with patch.object(secrets, 'set_key_verified', side_effect=AssertionError('invalid region')):
            for region in (True, -1, '0'):
                with self.subTest(region=region), self.assertRaises(ValueError):
                    self.remove(saved['credentialId'], region=region)


    def transport(self, callback):
        constructor = httpx.Client
        def client(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            return constructor(transport=httpx.MockTransport(callback), **kwargs)
        return patch.object(httpx, 'Client', side_effect=client)

    def test_snapshot_is_credential_free_with_canonical_order_and_protocol(self):
        with patch.object(secrets, 'get_key', side_effect=AssertionError('no passive key access')), \
                patch.object(secrets, 'get_config', side_effect=AssertionError('no passive key access')):
            result = self.service.snapshot()
        self.assertEqual([p['id'] for p in result['providers']],
                         ['deepseek', 'kimi', 'glm', 'minimax', 'mimo', 'qwen', 'doubao', 'openrouter', 'agnes', 'custom'])
        kimi = next(p for p in result['providers'] if p['id'] == 'kimi')
        coding = next(m for m in kimi['modes'] if m['id'] == 'coding')
        self.assertFalse(coding['available'])
        self.assertEqual(coding['protocol'], 'anthropic')
        self.assertNotIn('new-fixture-secret', json.dumps(result))

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

    def test_cached_custom_asr_metadata_never_reopens_system_key_store(self):
        raw = json.dumps({'api_key': 'cached-secret', 'base_url': 'https://cached.invalid/v1', 'model': 'saved-asr'})
        with patch.object(secrets, '_cache', {'custom_openai': raw}), patch.object(secrets, '_backend', side_effect=AssertionError('must not open')):
            public = self.service.snapshot()['cloudConfigs']['custom_asr']
        self.assertEqual(public['base'], 'https://cached.invalid/v1')
        self.assertEqual(public['modelId'], 'saved-asr')
        self.assertNotIn('cached-secret', json.dumps(public))

    def test_import_rejects_unsafe_legacy_endpoint_without_exposing_key_or_overwriting_public_copy(self):
        self.keys['custom_openai'] = json.dumps({'api_key': 'private-key', 'base_url': 'https://legacy.invalid/v1?key=private-key', 'model': 'asr'})
        with self.assertRaises(ValueError) as raised:
            self.service.execute('load_saved_configuration', dict(purpose='extract', provider='custom_asr'))
        self.assertNotIn('private-key', str(raised.exception))
        self.assertIsNone(self.workspace.store.preference('model-settings-cloud-configs'))

    def test_old_custom_asr_key_cannot_be_reused_at_an_edited_endpoint(self):
        self.keys['custom_openai'] = json.dumps({'api_key': 'legacy-key', 'base_url': 'https://legacy.invalid/v1', 'model': 'asr'})
        with patch('httpx.Client', side_effect=AssertionError('must not send to changed endpoint')), self.assertRaisesRegex(ValueError, '接口地址已更改'):
            self.service.execute('test_connection', dict(purpose='extract', provider='custom_asr',
                base='https://changed.invalid/v1', modelId='asr', credentialId='custom_openai'))

    def test_key_pages_are_static_creation_urls_and_reject_ui_supplied_targets(self):
        with patch.object(secrets, 'get_key', side_effect=AssertionError('no credential read')):
            self.assertEqual(self.service.key_url(dict(purpose='assistant', provider='deepseek')),
                             'https://platform.deepseek.com/api_keys')
            self.assertEqual(self.service.key_url(dict(purpose='extract', provider='openai')),
                             'https://platform.openai.com/api-keys')
            with self.assertRaises(ValueError):
                self.service.key_url(dict(purpose='assistant', provider='deepseek', url='https://unrelated.invalid'))
            with self.assertRaises(ValueError):
                self.service.key_url(dict(purpose='assistant', provider='custom'))

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

    def test_replacing_key_creates_new_snapshots_without_overwriting_old_credentials(self):
        first = self.save()
        old_keys = dict(self.keys)
        second = self.save(key='replacement-secret')
        self.assertNotEqual(first['credentialId'], second['credentialId'])
        self.assertTrue(all(self.keys[k] == v for k, v in old_keys.items()))
        self.assertEqual(len(providers.connections(self.workspace.store)), 4)
        self.assertEqual(len(self.workspace.connections()), 2)

    def test_unknown_capacity_fails_before_secret_write_and_keeps_connections(self):
        original = self.save()
        keys = dict(self.keys)
        with self.assertRaisesRegex(ValueError, '容量尚未确认'):
            self.save(modelIds=['unknown-model'], defaultModel='unknown-model')
        self.assertEqual(keys, self.keys)
        self.assertEqual(self.workspace.connections(), original['connections'])

    def test_unavailable_plan_and_wrong_official_endpoint_rejected_before_secret_read(self):
        with patch.object(secrets, 'get_key', side_effect=AssertionError('no credential read')):
            with self.assertRaisesRegex(ValueError, '暂不开放'):
                self.save(provider='kimi', mode='coding', base='', key='')
            with self.assertRaisesRegex(ValueError, '官方接口'):
                self.save(base='https://unrelated.invalid/v1', key='')

    def test_directory_connection_check_is_partial_not_authenticated_success(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data': [{'id': 'deepseek-flash'}]})
        with self.transport(respond):
            result = self.service.execute('test_connection', self.args())
        self.assertEqual(result['status'], 'partial')
        self.assertIn('尚未验证实际模型调用权限', result['message'])
        self.assertEqual(requests[0].method, 'GET')
        self.assertEqual(requests[0].content, b'')
        self.assertEqual(str(requests[0].url), 'https://api.deepseek.com/models')
        self.assertNotIn('new-fixture-secret', json.dumps(result))
        self.assertEqual(self.keys[result['credentialId']], 'new-fixture-secret')

    def test_connection_failure_never_echoes_server_body_or_secret_and_does_not_save(self):
        with self.transport(lambda _: httpx.Response(401, text='new-fixture-secret-private-server-body')):
            with self.assertRaises(RuntimeError) as raised:
                self.service.execute('test_connection', self.args())
        self.assertIn('401', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))
        self.assertEqual(self.keys, {})

    def test_redirect_is_not_followed_and_large_body_is_bounded(self):
        seen = []
        def redirect(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://unrelated.invalid'})
        with self.transport(redirect), self.assertRaisesRegex(RuntimeError, '302'):
            self.service.execute('test_connection', self.args())
        self.assertEqual(len(seen), 1)
        with self.transport(lambda _: httpx.Response(200, content=b'x' * (mc._MAX_RESPONSE + 1))), \
                self.assertRaisesRegex(RuntimeError, '过大'):
            self.service.execute('test_connection', self.args())

    def test_custom_capacity_requires_discovery_bound_to_its_credential(self):
        custom = dict(provider='custom', mode='api', base='https://custom.invalid/v1',
                      modelIds=['real-model'], defaultModel='real-model')
        with self.transport(lambda _: httpx.Response(200, json={'data': [{'id': 'real-model', 'context_length': 64000}]})):
            result = self.service.execute('discover_models', self.args(**custom))
        saved = self.save(**custom, credentialId=result['credentialId'], key='')
        self.assertEqual(saved['connections'][0]['window'], 64000)
        with self.assertRaisesRegex(ValueError, '不属于当前连接'):
            self.save(**dict(custom, base='https://other.invalid/v1'), credentialId=result['credentialId'], key='')

    def test_legacy_custom_models_reuse_persisted_capacity_without_discovery(self):
        credential = 'workspace-key-legacy-fixture'
        self.keys[credential] = 'isolated-legacy-secret'
        old = []
        for model in ('legacy-first', 'legacy-second'):
            old.append(providers.save_connection(dict(provider='custom', mode='api',
                base='https://legacy.invalid/v1', protocol='openai', model=model,
                window=64000, max_output=4096, threshold=.8, credential_id=credential,
                label=model), self.workspace.store))
        before = deepcopy(providers.connections(self.workspace.store))
        with patch.object(mc.Service, '_request_directory', side_effect=AssertionError('no network')), \
                patch.object(secrets, 'set_key_verified', side_effect=AssertionError('no replacement key')):
            result = self.save(provider='custom', base='https://legacy.invalid/v1',
                credentialId=credential, key='', modelIds=['legacy-first', 'legacy-second'],
                defaultModel='legacy-second')
        self.assertEqual(result['defaultConnectionId'], old[1]['id'])
        self.assertEqual(providers.connections(self.workspace.store), before)
        self.assertEqual([c['window'] for c in result['connections']], [64000, 64000])

    def test_legacy_capacity_cannot_cross_endpoint_protocol_provider_or_key(self):
        credential = 'workspace-key-legacy-fixture'
        self.keys[credential] = 'isolated-legacy-secret'
        original = dict(provider='custom', mode='api', base='https://legacy.invalid/v1',
            protocol='openai', model='legacy-only', window=64000, max_output=4096,
            threshold=.8, credential_id=credential, label='legacy-only')
        providers.save_connection(original, self.workspace.store)
        changes = [dict(base='https://other.invalid/v1'), dict(protocol='anthropic'),
                   dict(provider='deepseek', base='https://api.deepseek.com'),
                   dict(credential_id='workspace-key-other-fixture')]
        for change in changes:
            with self.subTest(change=change):
                other = dict(original, **change, model='other-model', label='other-model')
                self.keys[other['credential_id']] = 'isolated-other-secret'
                providers.save_connection(other, self.workspace.store)
                with self.assertRaisesRegex(ValueError, '上下文容量'):
                    self.save(provider=other['provider'], base=other['base'],
                        protocol=other['protocol'], credentialId=other['credential_id'], key='',
                        modelIds=['legacy-only'], defaultModel='legacy-only')
        with self.assertRaisesRegex(ValueError, '上下文容量'):
            self.save(provider='custom', base=original['base'], credentialId=credential,
                key='new-fixture-secret', modelIds=['legacy-only'], defaultModel='legacy-only')
        with self.assertRaisesRegex(ValueError, '字段无效'):
            self.save(provider='custom', base=original['base'], credentialId=credential, key='',
                modelIds=['invented'], defaultModel='invented', window=64000, max_output=4096)

    def test_unknown_directory_capabilities_stay_unknown_and_malformed_modalities_are_ignored(self):
        config = self.service._config(self.args(provider='custom', base='https://custom.invalid/v1'))
        rows = self.service._parse_models(config, [
            {'id': 'unknown'},
            {'id': 'invalid', 'architecture': {'output_modalities': True}},
            {'id': 'image-only', 'architecture': {'output_modalities': ['image']}},
        ])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['window'])
        self.assertFalse(rows[0]['tools_supported'])

    def test_asr_save_failure_restores_key_and_engine(self):
        original = json.dumps({'api_key': 'old-asr-secret'})
        self.keys['openai'] = original
        self.cfg.save.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, '已恢复原密钥'):
            self.service.execute('save_connection', dict(purpose='extract', provider='openai', key='new-asr-secret'))
        self.assertEqual(self.keys['openai'], original)
        self.assertEqual(self.cfg.engine_name, 'local_whisper_mlx')
        self.assertIsNone(self.workspace.store.preference('model-settings-cloud-configs'))

    def test_multiselect_database_failure_rolls_back_entire_group_and_default(self):
        first = self.save()
        before = deepcopy(providers.connections(self.workspace.store))
        with self.workspace.store.connect() as db:
            db.execute("CREATE TRIGGER fail_group BEFORE UPDATE ON preferences WHEN NEW.key='model-connection-groups' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(RuntimeError):
            self.save(key='replacement-secret', modelIds=['deepseek-v4-pro'], defaultModel='deepseek-v4-pro')
        self.assertEqual(providers.connections(self.workspace.store), before)
        self.assertEqual(self.workspace.store.preference('default-connection'), first['defaultConnectionId'])
        self.assertEqual(self.workspace.connections(), first['connections'])

    def test_second_model_validation_failure_does_not_leave_partial_group(self):
        original = providers.validated_connection
        count = 0
        def validate(value, store):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError('fixture second model failure')
            return original(value, store)
        with patch.object(providers, 'validated_connection', side_effect=validate), self.assertRaises(ValueError):
            self.save()
        self.assertEqual(providers.connections(self.workspace.store), [])
        self.assertIsNone(self.workspace.store.preference('default-connection'))

    def test_services_without_documented_free_probe_remain_explicitly_unverified(self):
        with patch('httpx.Client', side_effect=AssertionError('must not send billable probe')):
            result = self.service.execute('test_connection', self.args(
                provider='minimax', protocol='anthropic', base='https://api.minimax.cn/anthropic'))
        self.assertEqual(result['status'], 'partial')
        self.assertIn('暂无已确认的无消耗鉴权接口', result['message'])

    def test_asr_test_preserves_current_key_and_asr_save_uses_existing_namespace(self):
        self.keys['openai'] = json.dumps({'api_key': 'old-asr-secret'})
        with self.transport(lambda _: httpx.Response(200, json={'data': [{'id': 'gpt-4o-transcribe'}]})):
            self.service.execute('test_connection', dict(purpose='extract', provider='openai', key='new-asr-secret'))
        self.assertEqual(json.loads(self.keys['openai'])['api_key'], 'old-asr-secret')
        result = self.service.execute('save_connection', dict(purpose='extract', provider='openai', key='new-asr-secret'))
        self.assertEqual(result['credentialId'], 'openai')
        self.assertEqual(self.cfg.engine_name, 'openai')
        self.assertEqual(json.loads(self.keys['openai'])['api_key'], 'new-asr-secret')


class VerifiedSecretTests(unittest.TestCase):
    def test_write_requires_real_backend_readback_without_memory_fallback(self):
        values = {}
        backend = SimpleNamespace(set_password=lambda _service, pid, value: values.__setitem__(pid, value),
                                  get_password=lambda _service, pid: values.get(pid),
                                  delete_password=lambda _service, pid: values.pop(pid, None))
        with patch.object(secrets, '_backend', return_value=backend), patch.object(secrets, '_cache', {}), patch.object(secrets, '_memory', {}):
            secrets.set_key_verified('test-only', 'fixture-secret')
            self.assertEqual(secrets.get_key('test-only'), 'fixture-secret')
        backend.get_password = lambda *_: None
        with patch.object(secrets, '_backend', return_value=backend), patch.object(secrets, '_cache', {}), patch.object(secrets, '_memory', {}):
            with self.assertRaisesRegex(RuntimeError, '未能确认保存'):
                secrets.set_key_verified('test-only', 'fixture-secret')
            self.assertNotIn('test-only', secrets._memory)


if __name__ == '__main__':
    unittest.main()
