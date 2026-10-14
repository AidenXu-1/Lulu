"""Authenticated upgrade identities: tiny signed Apps and memory keys only.

Reuse the review regression's fixture; its module creates Qt, so run this named
module through run_product_capability_tests.py with QT_QPA_PLATFORM=offscreen.
No installed App, Core process, Keychain or network is used.
"""
import base64
from copy import deepcopy
import hashlib
import hmac
import json
import shutil
import unittest
from unittest.mock import patch

import test_review_regressions as fixtures
from src.core.agent_entry import standalone_runtime as runtime
from src.core.agent_entry import standalone_history as history
from src.core.agent_entry.delivery_identity import MANIFEST_NAME
from src.core.agent_entry.installed_materials import MATERIALS_DIR, _private_json
from src.core.agent_entry.ownership import (
    _canonical, _key_name, verify_ownership_receipt, verify_ready_action_receipt,
)
from src.core.agent_entry.product_transaction import snapshot_digest
from src.core.agent_entry.standalone_state import install_state_root
from src.core.agent_entry.task_handover import FILENAME, open_handover, seal_handover


class HistoryIdentitySourcesTests(unittest.TestCase):
    setUp = fixtures.UpgradeRuntimeTests.setUp
    candidate = fixtures.UpgradeRuntimeTests.candidate

    def make_b(self):
        self.candidate('zz-identity-b', '/usr/bin/false')
        return runtime.prepare_current_app(backup_app=self.backup)

    @staticmethod
    def core_digest(state):
        manifest = _private_json(state/MATERIALS_DIR/MANIFEST_NAME)
        return next(row['sha256'] for row in manifest['components']
                    if row['path'] == 'Contents/MacOS/lulu-agent-core')

    def seal_sources(self, state, sources):
        owner = verify_ownership_receipt(state/'ownership.json', key_store=self.keys)
        path = state/MATERIALS_DIR/FILENAME
        old = _private_json(path)
        rows = open_handover(old, keys=self.keys, owner=owner, product_root=self.product)
        value = seal_handover(rows, keys=self.keys, owner=owner,
            previous_manifest=old['body']['previous_manifest'], product_root=self.product,
            gui_context=old['body'].get('gui_context'), source_cores=sources)
        path.write_text(json.dumps(value))

    def assert_no_capture(self, error, *, backup=None):
        target = install_state_root(self.app, self.product)
        before = snapshot_digest(self.product)
        values, writes = deepcopy(self.old_keys.values), list(self.old_keys.writes)
        with patch.object(history, 'historical_keys', side_effect=AssertionError('unverified identity reached private history')) as reader:
            with self.assertRaisesRegex(RuntimeError, error):
                runtime.prepare_current_app(backup_app=backup or self.backup)
        reader.assert_not_called()
        self.assertFalse(target.exists())
        self.assertEqual(snapshot_digest(self.product), before)
        self.assertEqual(self.old_keys.values, values)
        self.assertEqual(self.old_keys.writes, writes)

    def test_authenticated_other_installation_is_rejected_before_history_reads(self):
        b = self.make_b()
        owner = _private_json(b/'ownership.json')
        owner['installation_id'] = 'app-other-authenticated-installation'
        owner['installation_id_digest'] = hashlib.sha256(owner['installation_id'].encode()).hexdigest()
        # This is a valid, independently authenticated receipt, not a bad MAC.
        key = bytes(range(32))
        self.keys.write_new(_key_name(owner['installation_id_digest']), base64.b64encode(key).decode())
        owner['hmac_sha256'] = hmac.new(key, _canonical(owner), hashlib.sha256).hexdigest()
        (b/'ownership.json').write_text(json.dumps(owner))
        action = _private_json(b/'current.json')
        action['binding']['installation_id_digest'] = owner['installation_id_digest']
        (b/'current.json').write_text(json.dumps(action))
        handover = seal_handover([], keys=self.keys, owner=owner,
            previous_manifest=None, product_root=self.product,
            source_cores={self.core_digest(b): owner['core_cdhash']})
        (b/MATERIALS_DIR/FILENAME).write_text(json.dumps(handover))
        self.assertEqual(verify_ownership_receipt(b/'ownership.json', key_store=self.keys), owner)
        verify_ready_action_receipt(b/'current.json', ownership=owner)
        open_handover(handover, keys=self.keys, owner=owner, product_root=self.product)
        self.candidate('zz-identity-c', '/usr/bin/printf')
        self.assert_no_capture('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')

    def test_source_cores_edit_without_authentication_never_expands_allowlist(self):
        b = self.make_b()
        path = b/MATERIALS_DIR/FILENAME
        value = _private_json(path)
        value['body']['source_cores'] = {'e'*64: 'f'*40}
        path.write_text(json.dumps(value))
        self.candidate('zz-identity-c', '/usr/bin/printf')
        self.assert_no_capture('PRODUCT_TASK_HANDOVER_INVALID')

    def test_authenticated_conflicting_source_identity_is_rejected(self):
        b = self.make_b()
        self.seal_sources(b, {self.core_digest(self.a): 'f'*40})
        self.candidate('zz-identity-c', '/usr/bin/printf')
        self.assert_no_capture('PRODUCT_TASK_HANDOVER_INVALID')

    def test_changed_source_manifest_cannot_supply_identity(self):
        b = self.make_b()
        path = b/MATERIALS_DIR/MANIFEST_NAME
        value = _private_json(path)
        next(row for row in value['components'] if row['path'] == 'Contents/MacOS/lulu-agent-core')['sha256'] = '0'*64
        path.write_text(json.dumps(value))
        self.candidate('zz-identity-c', '/usr/bin/printf')
        self.assert_no_capture('AGENT_ENTRY_COMPONENT_MISMATCH')

    def test_all_authenticated_identities_exist_before_first_private_read_in_either_order(self):
        b = self.make_b()
        backup_b = self.root/'backup-b/Lulu.app'
        backup_b.parent.mkdir(); shutil.copytree(self.app, backup_b, symlinks=True)
        expected = {self.core_digest(state): _private_json(state/'ownership.json')['core_cdhash']
                    for state in (self.a, b)}
        self.assertEqual(len(set(expected.values())), 2)
        # Remove optional carried identity hints. The ready owners and bound
        # manifests alone must be gathered before the first predecessor read.
        self.seal_sources(b, {})
        self.candidate('zz-identity-c', '/usr/bin/printf')
        original = snapshot_digest(self.a)
        for order in ([self.a, b], [b, self.a]):
            calls = []
            def reader(_keys, _core, generation, *, known_cores):
                calls.append(generation)
                self.assertTrue(expected.items() <= known_cores.items())
                self.assertEqual(generation, self.owner['core_cdhash'])
                return self.old_keys
            with self.subTest(order=[path.name for path in order]), \
                    patch.object(runtime, 'previous_states', return_value=order), \
                    patch.object(history, 'historical_keys', side_effect=reader):
                target = runtime.prepare_current_app(backup_app=backup_b)
            self.assertEqual(calls, [self.owner['core_cdhash']])
            owner = verify_ownership_receipt(target/'ownership.json', key_store=self.keys)
            open_handover(_private_json(target/MATERIALS_DIR/FILENAME),
                keys=self.keys, owner=owner, product_root=self.product)
            self.assertEqual(snapshot_digest(self.a), original)

    def test_empty_jobs_do_not_bypass_binding_key_or_highwater_authentication(self):
        from src.core.job_ledger.storage_binding import REGISTRY_KEY, KEY_PREFIX, ANCHOR_PREFIX
        root = self.product/'task-generations'/self.owner['core_cdhash']
        snapshot = root/'jobs/job-ledger-v1/snapshot.json'
        self.assertEqual(_private_json(snapshot)['jobs'], [])
        pristine_values = deepcopy(self.old_keys.values)
        scope = json.loads(pristine_values[REGISTRY_KEY])['body']['scope']
        binding = root/'task-integrity-v1/binding.json'
        pristine_binding = binding.read_bytes()
        self.candidate('zz-empty-identity-c', '/usr/bin/printf')
        target = install_state_root(self.app, self.product)
        for attack in ('missing_registry', 'wrong_hmac_key', 'bad_binding', 'advanced_anchor'):
            with self.subTest(attack=attack):
                self.old_keys.values = deepcopy(pristine_values)
                binding.write_bytes(pristine_binding)
                if attack == 'missing_registry':
                    self.old_keys.values.pop(REGISTRY_KEY)
                elif attack == 'wrong_hmac_key':
                    value = json.loads(self.old_keys.values[KEY_PREFIX+scope])
                    value['key'] = base64.b64encode(bytes(range(32))).decode()
                    self.old_keys.values[KEY_PREFIX+scope] = json.dumps(value)
                elif attack == 'bad_binding':
                    value = json.loads(binding.read_bytes()); value['hmac'] = '0'*64
                    binding.write_text(json.dumps(value))
                else:
                    value = json.loads(self.old_keys.values[ANCHOR_PREFIX+scope])
                    value['sequence'] = 1; value['head_hmac'] = 'f'*64
                    self.old_keys.values[ANCHOR_PREFIX+scope] = json.dumps(value)
                before, values = snapshot_digest(root), deepcopy(self.old_keys.values)
                writes = list(self.old_keys.writes)
                with self.assertRaises(RuntimeError) as failure:
                    runtime.prepare_current_app(backup_app=self.backup)
                if attack == 'advanced_anchor':
                    self.assertEqual(str(failure.exception), 'INTEGRITY_ERROR')
                    self.assertEqual(failure.exception.reason_code, 'JOB_LEDGER_HIGH_WATER_ROLLBACK')
                else:
                    self.assertEqual(str(failure.exception), 'PRODUCT_TASK_HANDOVER_INVALID')
                self.assertFalse(target.exists())
                self.assertEqual(_private_json(snapshot)['jobs'], [])
                self.assertEqual(snapshot_digest(root), before)
                self.assertEqual(self.old_keys.values, values)
                self.assertEqual(self.old_keys.writes, writes)
