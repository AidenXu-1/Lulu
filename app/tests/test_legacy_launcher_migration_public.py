"""Exact shipped v1 launcher bytes, independent of the migration implementation."""
import hashlib
import shutil
import unittest
from pathlib import Path
from contextlib import contextmanager

from test_product_independent_boundaries import fixture
import test_generation9_formal_install_public as legacy
from src.core.agent_entry.component_manifest import verify_component_manifest
from src.core.agent_entry.ownership import create_ownership_receipt, verify_ownership_receipt
from src.core.agent_entry.transaction import _replace_json

# Extracted from d3325838fe47bf559fc1a5f6d089e254d61573b1:
# app/src/core/agent_entry/formal_install.py:_LAUNCHER. Do not import the new constant.
HISTORICAL_LAUNCHER = b'#!/bin/sh\nexec /Applications/Lulu.app/Contents/MacOS/lulu-agent-cli "$@"\n'
HISTORICAL_SHA256 = 'e0ce4d1e61f4c2cc1332938edeafba9c39da8e88e566cb8d74efc904fb9caac6'


@contextmanager
def migrated_v1_fixture():
    with fixture() as case:
        old = legacy._fixture_app(case.root/'history')
        proof = verify_component_manifest(old, old/'Contents/Resources/agent_product_manifest.json')
        owner = create_ownership_receipt(case.paths.state_root/'ownership.json', key_store=case.keys,
            release_dmg_sha256='a'*64, component_manifest_sha256=proof['component_manifest_sha256'],
            app_component_set_digest=proof['app_component_set_digest'],
            installation_id='historical-v1-authenticated-fixture', candidate_generation=9)
        binding={field:owner[field] for field in ('release_dmg_sha256','component_manifest_sha256',
            'app_component_set_digest','installation_id_digest','candidate_generation')}
        _replace_json(case.paths.state_root/'current.json', {'schema_id':'lulu.agent-entry-current-action/v1',
            'transaction_id':'historical-install-0001','action':'install','phase':'COMMITTED','state':'ready',
            'effect_count':3,'binding':binding})
        case.paths.launch_agent_target.parent.mkdir(parents=True)
        shutil.copy2(old/'Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist',case.paths.launch_agent_target)
        case.paths.skill_target.parent.mkdir(parents=True)
        shutil.copytree(old/'Contents/Resources/skills/lulu-agent',case.paths.skill_target)
        case.paths.launcher_target.parent.mkdir(parents=True)
        case.paths.launcher_target.write_bytes(HISTORICAL_LAUNCHER);case.paths.launcher_target.chmod(0o700)
        case.paths.app_bundle.parent.mkdir(parents=True)
        shutil.copytree(case.app,case.paths.app_bundle)
        backup=case.paths.app_bundle.with_name('Lulu.backup-historical-v1')
        shutil.copytree(old,backup)
        case.service.loaded=True
        # Let the public installer find the exact receipt-bound old backup itself.
        assert case.installer.legacy_reference_app is None
        yield case,owner,backup


class LegacyLauncherMigrationTests(unittest.TestCase):
    def test_shipped_73_byte_launcher_migrates_with_real_v1_hmac_and_missing_old_dmg(self):
        self.assertEqual(len(HISTORICAL_LAUNCHER),73)
        self.assertEqual(hashlib.sha256(HISTORICAL_LAUNCHER).hexdigest(),HISTORICAL_SHA256)
        with migrated_v1_fixture() as (case,owner,backup):
            receipt_before=(case.paths.state_root/'ownership.json').read_bytes()
            result=case.install_product()
            after=verify_ownership_receipt(case.paths.state_root/'ownership.json',key_store=case.keys)
            self.assertEqual(result['state'],'ready');self.assertEqual(after['schema_id'],'lulu.agent-entry-ownership/v3')
            self.assertEqual(after['installation_id'],owner['installation_id'])
            self.assertEqual(case.installer.legacy_reference_app,backup)
            self.assertEqual(case.installer.skill_status(),'ready')
            self.assertTrue(backup.is_dir())
            receipts=list(case.paths.state_root.glob('.ownership.json.lulu-backup-*'))
            launchers=list(case.paths.launcher_target.parent.glob('.lulu.lulu-backup-*'))
            self.assertEqual(len(receipts),1);self.assertEqual(receipts[0].read_bytes(),receipt_before)
            self.assertEqual(len(launchers),1);self.assertEqual(launchers[0].read_bytes(),HISTORICAL_LAUNCHER)

    def test_unrecognized_launcher_bytes_and_permissions_are_preserved_before_dmg_work(self):
        variants=(HISTORICAL_LAUNCHER+b' ',
            b'#!/bin/sh\nexec "/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli" "$@"\n',
            HISTORICAL_LAUNCHER.replace(b'/Applications/',b'/tmp/'),
            HISTORICAL_LAUNCHER+b'echo extra\n',None)
        for raw in variants:
            with self.subTest(raw=raw),migrated_v1_fixture() as (case,_owner,_backup):
                if raw is not None:case.paths.launcher_target.write_bytes(raw)
                else:case.paths.launcher_target.chmod(0o755)
                before=case.paths.launcher_target.read_bytes()
                with self.assertRaisesRegex(RuntimeError,'AGENT_ENTRY_OWNERSHIP_CONFLICT'):case.install_product()
                self.assertEqual(case.paths.launcher_target.read_bytes(),before)
                self.assertEqual(case.events,[])
                self.assertFalse((case.paths.state_root/'product-transactions').exists())
                self.assertFalse(list(case.paths.launcher_target.parent.glob('.lulu.lulu-backup-*')))

    def test_historical_bytes_do_not_bypass_old_hmac_or_managed_tree_validation(self):
        for target in ('hmac','skill','core'):
            with self.subTest(target=target),migrated_v1_fixture() as (case,owner,_backup):
                if target=='hmac':
                    owner['hmac_sha256']='0'*64;_replace_json(case.paths.state_root/'ownership.json',owner)
                elif target=='skill':(case.paths.skill_target/'unknown.txt').write_text('user file')
                else:case.paths.launch_agent_target.write_bytes(case.paths.launch_agent_target.read_bytes()+b' ')
                with self.assertRaises(RuntimeError):case.install_product()
                self.assertEqual(case.events,[])
                self.assertFalse((case.paths.state_root/'product-transactions').exists())
                self.assertEqual(case.paths.launcher_target.read_bytes(),HISTORICAL_LAUNCHER)
