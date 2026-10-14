"""Signed tiny Apps and temporary directories only; no installation or Keychain."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from test_delivery_package_v2_public import signed_delivery
from src.core.agent_entry.component_manifest import verify_component_manifest
from src.core.agent_entry.delivery_identity import MANIFEST_NAME


class StandaloneInstallSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = signed_delivery(self.root/'source')
        self.manifest = json.loads((self.app.parent/MANIFEST_NAME).read_text())

    def prepare(self):
        from src.core.agent_entry.standalone_source import prepare_install_descriptor
        # The descriptor is inserted before the final signature, as in packaging.
        profile = self.app/'Contents/Resources/build_profile.json'
        payload = json.loads(profile.read_text()); payload['schema'] = 1
        profile.write_text(json.dumps(payload))
        prepare_install_descriptor(self.app, product_version='0.3.2',
            product_build='20260904.1', source_revision=self.manifest['source_revision'],
            capability_closed_set=('doctor', 'transcribe', 'version'))
        subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(self.app)],
                       check=True, capture_output=True)

    def test_app_alone_reconstructs_verifiable_materials(self):
        from src.core.agent_entry.standalone_source import inspect_install_source
        self.prepare()
        (self.app.parent/MANIFEST_NAME).unlink()
        proof = inspect_install_source(self.app)
        self.assertEqual(proof.receipt['source_kind'], 'signed_app')
        self.assertNotIn('release_dmg_sha256', proof.receipt)
        self.assertEqual(proof.receipt['app_component_set_digest'], proof.manifest['app_component_set_digest'])
        self.assertEqual(proof, inspect_install_source(self.app))
        path = self.root/MANIFEST_NAME
        path.write_bytes(proof.manifest_bytes)
        checked = verify_component_manifest(self.app, path)
        self.assertEqual(checked['component_manifest_sha256'], proof.receipt['component_manifest_sha256'])

    def test_modified_signed_descriptor_is_rejected(self):
        from src.core.agent_entry.standalone_source import DESCRIPTOR, inspect_install_source
        self.prepare()
        path = self.app/DESCRIPTOR
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(RuntimeError):
            inspect_install_source(self.app)

    def test_resigned_descriptor_cannot_disagree_with_build_profile(self):
        from src.core.agent_entry.standalone_source import DESCRIPTOR, inspect_install_source
        self.prepare()
        path = self.app/DESCRIPTOR
        value = json.loads(path.read_text()); value['product_build'] = '20990101.1'
        path.write_text(json.dumps(value))
        subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(self.app)],
                       check=True, capture_output=True)
        with self.assertRaises(RuntimeError):
            inspect_install_source(self.app)

    def test_legacy_app_requires_legacy_path(self):
        from src.core.agent_entry.standalone_source import inspect_install_source
        with self.assertRaisesRegex(RuntimeError, 'APP_INSTALL_DESCRIPTOR_REQUIRED'):
            inspect_install_source(self.app)

    def test_storage_cutover_keeps_old_versions_legacy_and_421_local(self):
        from src.core.agent_entry.standalone_source import prepare_install_descriptor, uses_local_storage
        for version, local in [('4.1.7', False), ('4.2.0', False), ('4.2.1', True),
                               ('4.2.2', True), ('5.0.1', True)]:
            with self.subTest(version=version):
                app = self.root / version / 'Lulu.app'
                (app / 'Contents/Resources').mkdir(parents=True)
                prepare_install_descriptor(app, product_version=version,
                    product_build='20261013.2', source_revision=self.manifest['source_revision'],
                    capability_closed_set=('doctor', 'transcribe', 'version'))
                self.assertEqual(uses_local_storage(app), local)
