"""Fault injection uses temporary stores, fake keys and fake HTTP only."""
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from src.core import model_connections as mc
from tests import test_model_connections as connection_tests


class ConnectionStabilityTests(unittest.TestCase):
    setUp = connection_tests.ModelConnectionTests.setUp
    args = connection_tests.ModelConnectionTests.args
    save = connection_tests.ModelConnectionTests.save
    transport = connection_tests.ModelConnectionTests.transport

    def test_failed_new_connection_removes_only_its_newly_written_key(self):
        original = self.save()
        keys_before = dict(self.keys)
        connections_before = self.workspace.connections()
        with patch.object(mc.workspace, 'validated_connection', side_effect=sqlite3.OperationalError('fixture disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.save(key='another-temporary-fixture-key')
        self.assertEqual({k: v for k, v in self.keys.items() if v}, keys_before)
        self.assertEqual(self.workspace.connections(), connections_before)
        self.assertEqual(self.workspace.store.preference('default-connection'), original['defaultConnectionId'])

    def test_failed_existing_connection_never_removes_its_saved_key(self):
        original = self.save()
        keys_before = dict(self.keys)
        with patch.object(mc.workspace, 'validated_connection', side_effect=sqlite3.OperationalError('fixture disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.save(key='', credentialId=original['credentialId'])
        self.assertEqual(self.keys, keys_before)

    def test_transaction_failure_preserves_old_models_and_cleans_new_key(self):
        original = self.save()
        keys_before = dict(self.keys)
        connections_before = self.workspace.connections()
        connect = self.workspace.store.connect
        @contextmanager
        def fail_commit(**kwargs):
            with connect(**kwargs) as db:
                class Database:
                    def execute(self, sql, parameters=()):
                        if sql.startswith('INSERT INTO preferences') and parameters[0]=='default-connection':
                            raise sqlite3.OperationalError('fixture disk full')
                        return db.execute(sql, parameters)
                yield Database()
        with patch.object(self.workspace.store,'connect',side_effect=fail_commit):
            with self.assertRaisesRegex(RuntimeError,'原文件已保留'):
                self.save(key='new-transaction-fixture-key')
        self.assertEqual({k:v for k,v in self.keys.items() if v},keys_before)
        self.assertEqual(self.workspace.connections(),connections_before)
        self.assertEqual(self.workspace.store.preference('default-connection'),original['defaultConnectionId'])

    def test_error_after_transaction_commit_keeps_referenced_key(self):
        connect = self.workspace.store.connect
        @contextmanager
        def fail_after_commit(**kwargs):
            with connect(**kwargs) as db:
                yield db
                changed = db.total_changes
            if changed:
                raise OSError('fixture backup unavailable')
        with patch.object(self.workspace.store,'connect',side_effect=fail_after_commit):
            with self.assertRaisesRegex(RuntimeError,'连接已保存'):
                self.save()
        rows = self.workspace.connections()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(self.keys[row['credential_id']] for row in rows))

    def test_refresh_failure_after_commit_keeps_new_connection_key(self):
        with patch.object(self.service,'snapshot',side_effect=sqlite3.OperationalError('fixture read failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.save()
        rows = self.workspace.connections()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(self.keys[row['credential_id']] for row in rows))

    def test_cleanup_failure_is_explicit_and_does_not_touch_old_key(self):
        self.save()
        keys_before = dict(self.keys)
        def writer(cid, value):
            if not value:
                raise RuntimeError('fixture denied')
            self.keys[cid]=value
        with patch.object(mc.secrets,'set_key_verified',side_effect=writer), \
                patch.object(mc.workspace,'validated_connection',side_effect=sqlite3.OperationalError('fixture disk full')):
            with self.assertRaisesRegex(RuntimeError,'新密钥的清理也未能确认'):
                self.save(key='new-cleanup-fixture-key')
        self.assertTrue(all(self.keys[cid]==value for cid,value in keys_before.items()))

    def test_asr_save_failure_restores_key_selection_and_public_state(self):
        old_engine = self.cfg.engine_name
        self.keys['openai'] = '{"api_key":"original-temporary-asr-key"}'
        keys_before = dict(self.keys)
        public_before = self.service.snapshot()['cloudConfigs']
        self.cfg.save.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, '已恢复原密钥和默认模型'):
            self.service.execute('save_connection', dict(purpose='extract', provider='openai',
                key='new-temporary-asr-key', mode='api', region=0))
        self.assertEqual(self.cfg.engine_name, old_engine)
        self.assertEqual(self.keys, keys_before)
        self.assertEqual(self.service.snapshot()['cloudConfigs'], public_before)

    def test_directory_failures_do_not_save_keys_or_disclose_response(self):
        responses = [httpx.Response(302, headers={'location':'https://elsewhere.invalid/'}),
                     httpx.Response(200, text='private-fixture-body-not-json'),
                     httpx.Response(200, json={'data':'wrong-shape'}),
                     httpx.Response(200, content=b'x' * (mc._MAX_RESPONSE + 1))]
        for response in responses:
            with self.subTest(status=response.status_code, size=len(response.content)), \
                    self.transport(lambda request: response):
                with self.assertRaises(RuntimeError) as raised:
                    self.service.execute('test_connection', self.args())
                self.assertNotIn('private-fixture', str(raised.exception))
                self.assertNotIn('new-fixture-secret', str(raised.exception))
                self.assertEqual(self.keys, {})

    def test_directory_timeout_preserves_existing_connection(self):
        original = self.save()
        before = self.workspace.connections()
        def fail(request):
            raise httpx.ReadTimeout('fixture timeout', request=request)
        with self.transport(fail), self.assertRaisesRegex(RuntimeError, '填写内容已保留'):
            self.service.execute('test_connection', self.args(key='', credentialId=original['credentialId']))
        self.assertEqual(self.workspace.connections(), before)

    def test_provider_modes_and_regions_keep_their_registered_endpoint(self):
        seen=set()
        for provider in mc.workspace.catalog()['providers']:
            for mode in [dict(provider,id='api'),*provider['plans']]:
                for region in range(max(1,len(mode.get('regions',[])))):
                    with self.subTest(provider=provider['id'],mode=mode['id'],region=region):
                        args=self.args(provider=provider['id'],mode=mode['id'],region=region,base='',protocol='')
                        if provider['id']=='custom':
                            args.update(base='https://custom-fixture.invalid/v1',protocol='openai')
                        if mode.get('available') is False:
                            with self.assertRaises(ValueError):
                                self.service._config(args)
                            continue
                        config=self.service._config(args)
                        expected=args['base'] if provider['id']=='custom' else (
                            mode['regions'][region]['base'] if mode.get('regions') else mode['base'])
                        self.assertEqual(config['base'],mc.workspace.validate_url(expected))
                        self.assertEqual(config['region'],region)
                        self.assertEqual(config['protocol'],args['protocol'] if provider['id']=='custom' else mode['protocol'])
                        seen.add(provider['id'])
                        if provider['id']!='custom':
                            with self.assertRaises(ValueError):
                                self.service._config(dict(args,base='https://unrelated-fixture.invalid/v1'))
        self.assertEqual(seen,{p['id'] for p in mc.workspace.catalog()['providers']})
        self.assertEqual(self.keys,{})
