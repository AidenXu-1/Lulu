"""Independent contract probes; only exported source and disposable fixtures."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import shutil
import unittest

import test_product_install_v2_public as published
import test_generation9_formal_install_public as legacy
from src.core.agent_entry import product_install as implementation
from src.core.agent_entry.product_transaction import BARRIER, SCHEMA
from src.core.agent_entry.transaction import _replace_json, CrashInjected
from src.core.agent_entry.ownership import create_ownership_receipt, verify_ownership_receipt

ROOT = Path(__file__).resolve().parent
observations = {}


@contextmanager
def fixture():
    case = published.ProductInstallV2PublicTests(
        'test_base_install_is_ready_without_skill_and_reuses_same_product_for_optional_install')
    try:
        case.setUp()
        yield case
    finally:
        case.doCleanups()


class PowerLoss(BaseException):
    pass


class IndependentBoundaries(unittest.TestCase):
    def test_key_creation_before_first_journal_can_recover(self):
        with fixture() as case:
            original = case.keys.write_new
            def key_then_power_loss(name, value):
                original(name, value)
                raise PowerLoss('after memory key write, before ownership file')
            with patch.object(case.keys, 'write_new', side_effect=key_then_power_loss):
                with self.assertRaises(PowerLoss):
                    case.install_product()
            try:
                case.installer.recover_product('install-first-0001')
                recovery = 'returned'
            except Exception as exc:
                recovery = type(exc).__name__ + ': ' + str(exc)
            observations['key_before_journal'] = {
                'remaining_memory_keys': len(case.keys.values),
                'journal_exists': (case.paths.state_root/'product-transactions/install-first-0001/journal.json').exists(),
                'ownership_exists': (case.paths.state_root/'ownership.json').exists(),
                'app_exists': case.paths.app_bundle.exists(),
                'recovery': recovery,
            }
            self.assertEqual(len(case.keys.values), 0, 'Uncommitted key has no durable recovery binding')

    def test_optional_skill_rejects_symlink_ancestor_without_writing_target(self):
        with fixture() as case:
            case.install_product()
            third_party = case.root/'third-party-folder'
            third_party.mkdir()
            (third_party/'keep.txt').write_text('user content')
            case.paths.skill_target.parent.symlink_to(third_party, target_is_directory=True)
            try:
                result = case.installer.install_skill(transaction_id='optional-ancestor-0001')
                outcome = result.get('state')
            except Exception as exc:
                outcome = type(exc).__name__ + ': ' + str(exc)
            observations['optional_ancestor'] = {
                'outcome': outcome,
                'skill_created_through_alias': (third_party/'lulu-agent').exists(),
                'sentinel_preserved': (third_party/'keep.txt').read_text() == 'user content',
            }
            self.assertFalse((third_party/'lulu-agent').exists(), 'Skill written through unapproved ancestor')

    def test_managed_skill_drift_after_precheck_is_preserved_in_place(self):
        with fixture() as case:
            case.install_product()
            case.installer.install_skill(transaction_id='optional-before-upgrade-0001')
            watched = case.paths.skill_target/'SKILL.md'
            original = implementation.verify_delivery_dmg
            def verify_then_external_change(*args, **kwargs):
                delivery = original(*args, **kwargs)
                watched.write_text('THIRD_PARTY_CHANGE_AFTER_OWNERSHIP_CHECK')
                return delivery
            with patch.object(implementation, 'verify_delivery_dmg', side_effect=verify_then_external_change):
                try:
                    result = case.installer.install_from_dmg(case.dmg, source_app=case.app,
                        expected_sha256=case.expected, stage_root=case.root/'upgrade-stage',
                        transaction_id='upgrade-after-drift-0001')
                    outcome = result.get('state')
                except Exception as exc:
                    outcome = type(exc).__name__ + ': ' + str(exc)
            preserved = watched.read_text() == 'THIRD_PARTY_CHANGE_AFTER_OWNERSHIP_CHECK'
            observations['post_precheck_drift'] = {
                'outcome': outcome, 'unexpected_content_preserved_in_place': preserved,
                'backup_count': len(list(case.paths.skill_target.parent.glob('.lulu-agent.lulu-backup-*'))),
            }
            self.assertTrue(preserved, 'Unapproved new content adopted as rollback baseline and replaced')

    def test_optional_recovery_preserves_third_value_current_receipt(self):
        with fixture() as case:
            case.install_product()
            with self.assertRaises(CrashInjected):
                case.installer.install_skill(transaction_id='optional-current-drift-0001', crash_after='launcher:effect')
            current = case.paths.state_root/'current.json'
            current.write_text('THIRD_PARTY_CURRENT_RECEIPT')
            try:
                result = case.installer.recover_skill(transaction_id='optional-current-drift-0001')
                outcome = result.get('state')
            except Exception as exc:
                outcome = type(exc).__name__ + ': ' + str(exc)
            preserved = current.read_text() == 'THIRD_PARTY_CURRENT_RECEIPT'
            observations['optional_current_drift'] = {'outcome': outcome, 'current_preserved': preserved}
            self.assertTrue(preserved, 'Optional recovery overwrote an unrecognized current receipt')

    def test_v1_and_v2_dispatchers_respect_product_barrier(self):
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        with fixture() as case:
            case.install_product()
            old = legacy._fixture_app(case.root/'old-dispatcher')
            _replace_json(case.paths.state_root/BARRIER,
                {'schema_id': SCHEMA, 'transaction_id': 'update-barrier-0001', 'phase': 'PREPARED'})
            results = {}
            for name, app in [('v1', old), ('v2', case.paths.app_bundle)]:
                calls = []
                def business():
                    calls.append('business')
                    return {'jobs': []}
                dispatcher = CoreCommandDispatcher(product_identity={'adapter_id': 'agent_cli'},
                    resource_root=app/'Contents/Resources',
                    runtime_factory=lambda: SimpleNamespace(gui_jobs_list=business))
                with patch.object(implementation, 'build_default_product_installer', return_value=case.installer):
                    try:
                        dispatcher({'command': 'gui.jobs.list', 'arguments': {}})
                        outcome = 'business returned'
                    except Exception as exc:
                        outcome = type(exc).__name__ + ': ' + str(exc)
                results[name] = {'outcome': outcome, 'business_calls': len(calls)}
            observations['business_barrier'] = results
            self.assertEqual(results['v1']['business_calls'], 0, 'Legacy-format Core skips product barrier')
            self.assertEqual(results['v2']['business_calls'], 0)

    def test_real_v1_hmac_fixture_migrates_with_missing_old_dmg(self):
        from src.core.agent_entry.component_manifest import verify_component_manifest
        with fixture() as case:
            old = legacy._fixture_app(case.root/'actual-v1-reference')
            proof = verify_component_manifest(old, old/'Contents/Resources/agent_product_manifest.json')
            owner = create_ownership_receipt(case.paths.state_root/'ownership.json',
                key_store=case.keys, release_dmg_sha256='a'*64,
                component_manifest_sha256=proof['component_manifest_sha256'],
                app_component_set_digest=proof['app_component_set_digest'],
                installation_id='valid-v1-fixture-installation', candidate_generation=9)
            self.assertEqual(owner['schema_id'], 'lulu.agent-entry-ownership/v1')
            _replace_json(case.paths.state_root/'current.json', {
                'schema_id': 'lulu.agent-entry-current-action/v1', 'transaction_id': 'legacy-created-0001',
                'action': 'install', 'phase': 'COMMITTED', 'state': 'ready', 'effect_count': 3,
                'binding': implementation._binding(owner)})
            case.paths.launch_agent_target.parent.mkdir(parents=True)
            shutil.copy2(old/'Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist',case.paths.launch_agent_target)
            case.paths.skill_target.parent.mkdir(parents=True)
            shutil.copytree(old/'Contents/Resources/skills/lulu-agent',case.paths.skill_target)
            case.paths.launcher_target.parent.mkdir(parents=True)
            case.paths.launcher_target.write_bytes(implementation.LEGACY_LAUNCHER)
            case.paths.launcher_target.chmod(0o700)
            case.service.loaded = True
            case.paths.app_bundle.parent.mkdir(parents=True)
            # Existing App differs from the authenticated old reference; no old DMG exists.
            shutil.copytree(case.app,case.paths.app_bundle)
            case.installer.legacy_reference_app=old
            before=(case.paths.state_root/'ownership.json').read_bytes()
            result=case.install_product()
            after=verify_ownership_receipt(case.paths.state_root/'ownership.json',key_store=case.keys)
            backups=list(case.paths.state_root.glob('.ownership.json.lulu-backup-*'))
            observations['true_v1_migration']={
                'outcome':result['state'], 'before_schema':owner['schema_id'], 'after_schema':after['schema_id'],
                'same_installation':after['installation_id']==owner['installation_id'],
                'old_receipt_backup_exact':len(backups)==1 and backups[0].read_bytes()==before,
                'old_dmg_present':False,
                'fixture_trust':'Created through real HMAC API with MemoryKeys; not a real user credential',
            }
            self.assertEqual(result['state'],'ready')
            self.assertEqual(after['installation_id'],owner['installation_id'])
            self.assertEqual(backups[0].read_bytes(),before)
