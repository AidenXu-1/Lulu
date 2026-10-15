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
