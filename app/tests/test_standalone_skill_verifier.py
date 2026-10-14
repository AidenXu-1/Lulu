"""Signed temporary Apps and in-memory keys; never touch installed state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_standalone_install_source as fixtures
from test_generation9_formal_install_public import MemoryKeys
from src.core.agent_entry.standalone_state import prepare_install_state

ROOT = Path(__file__).resolve().parents[2]


def load(path):
    spec = importlib.util.spec_from_file_location('standalone_verifier_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StandaloneSkillVerifierTests(unittest.TestCase):
    def setUp(self):
        fixtures.StandaloneInstallSourceTests.setUp(self)
        fixtures.StandaloneInstallSourceTests.prepare(self)
        self.product = self.root/'product'
        self.product.mkdir()
        self.state = prepare_install_state(self.app, product_root=self.product, keys=MemoryKeys())
        self.launcher = self.root/'lulu'
        self.launcher.write_bytes(('#!/bin/sh\nset -eu\nexec '+str(self.app/'Contents/MacOS/lulu-agent-cli')+' "$@"\n').encode())
        self.launcher.chmod(0o700)
        self.skill = self.root/'skill'
        shutil.copytree(self.app/'Contents/Resources/skills/lulu-agent', self.skill)
        self.verifiers = [
            load(ROOT/'agent-skill/lulu-agent/scripts/verify_lulu.py'),
            load(ROOT/'app/skills/lulu-agent/scripts/verify_lulu_generation9.py'),
        ]

    def verify(self, verifier):
        kwargs = {} if verifier is self.verifiers[0] else dict(launcher=self.launcher, installed_skill=self.skill)
        with patch.object(verifier, 'APP', self.app):
            return verifier.verify(self.app, state_root=self.state, **kwargs)

    def reject(self):
        for verifier in self.verifiers:
            with self.subTest(verifier=verifier.__file__), self.assertRaises((OSError, RuntimeError, ValueError)):
                self.verify(verifier)

    def change_json(self, relative, mutate):
        p = self.state/relative
        value = json.loads(p.read_bytes())
        mutate(value)
        p.write_text(json.dumps(value))

    def test_current_generation_works_without_old_dmg_materials(self):
        for verifier in self.verifiers:
            result = self.verify(verifier)
            self.assertTrue(result['verified'])
            self.assertEqual(result['product_build'], '20260904.1')
            self.assertEqual(result['component_manifest_sha256'], hashlib.sha256(
                (self.state/'product-materials-v2/Lulu.components.v2.json').read_bytes()).hexdigest())

    def prepare_local(self):
        from test_local_task_migration import LocalTaskMigrationTests
        from src.core.agent_entry.local_task_storage import prepare_local_app
        LocalTaskMigrationTests.set_version(self, '4.2.1', '20261013.3')
        self.state = prepare_local_app(self.app, self.product)
        shutil.rmtree(self.skill)
        shutil.copytree(self.app/'Contents/Resources/skills/lulu-agent', self.skill)

    def test_local_descriptor_verifies_and_resolves_its_private_generation(self):
        self.prepare_local()
        home = self.root/'home'
        product = home/'Library/Application Support/Lulu'
        product.parent.mkdir(parents=True)
        shutil.copytree(self.product, product)
        for verifier in self.verifiers:
            with self.subTest(verifier=verifier.__file__):
                self.assertTrue(self.verify(verifier)['verified'])
                kwargs = {} if verifier is self.verifiers[0] else dict(launcher=self.launcher, installed_skill=self.skill)
                with patch.object(verifier, 'APP', self.app), patch.object(Path, 'home', return_value=home):
                    result = verifier.verify(self.app, **kwargs)
                    self.assertTrue(result['verified'])
                    self.assertEqual(result['product_version'], '4.2.1')

    def test_local_descriptor_unknown_scheme_is_rejected(self):
        self.prepare_local()
        path = self.app/'Contents/Resources/standalone-install.v1.json'
        value = json.loads(path.read_bytes())
        value['storage_scheme'] = 'unrecognized'
        path.write_text(json.dumps(value))
        self.reject()

    def test_default_resolves_descriptor_bound_generation_not_legacy_root(self):
        home = self.root/'home'
        product = home/'Library/Application Support/Lulu'
        product.parent.mkdir(parents=True)
        shutil.copytree(self.product, product)
        (product/'AgentEntry/current.json').write_text('{"phase":"COMMITTED"}')
        for verifier in self.verifiers:
            kwargs = {} if verifier is self.verifiers[0] else dict(launcher=self.launcher, installed_skill=self.skill)
            with patch.object(verifier, 'APP', self.app), patch.object(Path, 'home', return_value=home):
                self.assertTrue(verifier.verify(self.app, **kwargs)['verified'])

    def test_incomplete_action_is_rejected(self):
        self.change_json('current.json', lambda v: v.update(phase='PREPARING'))
        self.reject()

    def test_owner_cannot_point_to_other_app(self):
        self.change_json('ownership.json', lambda v: v.update(app_path='/Applications/Other.app'))
        self.reject()

    def test_source_receipt_cannot_bind_another_descriptor(self):
        self.change_json('product-materials-v2/verified-app.json', lambda v: v.update(descriptor_sha256='0'*64))
        self.reject()

    def test_core_registration_cannot_redirect_executable(self):
        import plistlib
        p = self.state/'core.plist';value = plistlib.loads(p.read_bytes())
        value['ProgramArguments'] = ['/tmp/untrusted-core']
        p.write_bytes(plistlib.dumps(value))
        self.reject()

    def test_changed_app_file_is_rejected(self):
        (self.app/'Contents/Resources/extra.txt').write_text('unknown file')
        self.reject()

    def test_changed_manifest_is_rejected(self):
        self.change_json('product-materials-v2/Lulu.components.v2.json', lambda v: v.update(product_build='20990101.1'))
        self.reject()

    def test_world_readable_receipt_is_rejected(self):
        (self.state/'ownership.json').chmod(0o644)
        self.reject()

    def test_symlinked_material_directory_is_rejected(self):
        directory = self.state/'product-materials-v2';other = self.root/'other-materials'
        directory.rename(other);directory.symlink_to(other)
        self.reject()

    def test_malformed_descriptor_does_not_fall_back_to_old_verification(self):
        (self.app/'Contents/Resources/standalone-install.v1.json').write_text('{}')
        self.reject()

    def test_bundled_verifier_rejects_changed_shortcut_and_skill(self):
        verifier = self.verifiers[1]
        self.launcher.write_text('#!/bin/sh\nexec /tmp/other "$@"\n')
        with self.assertRaises(RuntimeError): self.verify(verifier)
        self.launcher.write_bytes(('#!/bin/sh\nset -eu\nexec '+str(self.app/'Contents/MacOS/lulu-agent-cli')+' "$@"\n').encode())
        (self.skill/'extra.txt').write_text('changed')
        with self.assertRaises(RuntimeError): self.verify(verifier)

    def test_public_skill_remains_independent_of_optional_files(self):
        self.launcher.unlink();shutil.rmtree(self.skill)
        self.assertTrue(self.verify(self.verifiers[0])['verified'])

    def test_receipt_change_during_signature_verification_is_rejected(self):
        for verifier in self.verifiers:
            with self.subTest(verifier=verifier.__file__):
                path = self.state/'current.json';original = path.read_bytes()
                tree = verifier._v2_module()
                with patch.object(verifier, '_v2_module', return_value=tree), patch.object(
                        tree, 'verify_final_signature', side_effect=lambda app: path.write_bytes(original+b' ')):
                    with self.assertRaises(RuntimeError): self.verify(verifier)
                path.write_bytes(original)

    def test_native_signature_failure_is_never_ignored(self):
        for verifier in self.verifiers:
            tree = verifier._v2_module()
            with patch.object(verifier, '_v2_module', return_value=tree), patch.object(
                    tree, 'verify_final_signature', side_effect=RuntimeError('FINAL_APP_SIGNATURE_INVALID')):
                with self.assertRaisesRegex(RuntimeError, 'FINAL_APP_SIGNATURE_INVALID'): self.verify(verifier)

    def test_descriptor_duplicate_keys_are_rejected(self):
        path = self.app/'Contents/Resources/standalone-install.v1.json'
        path.write_bytes(path.read_bytes().rstrip()[:-1]+b',"candidate_generation":9}')
        self.reject()

    def test_public_and_bundled_standalone_validation_stay_identical(self):
        import inspect
        for name in ('_verify_standalone', '_verify_v2_components'):
            self.assertEqual(inspect.getsource(getattr(self.verifiers[0], name)),
                             inspect.getsource(getattr(self.verifiers[1], name)))
