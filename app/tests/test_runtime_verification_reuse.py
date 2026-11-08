"""Startup reuses verified bytes, never mutable results or stale package evidence."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from src.core.agent_entry.verification_cache import (
    _runtime_component_cache, verify_runtime_component_manifest,
)


class RuntimeVerificationReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(_runtime_component_cache.cache_clear)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'Lulu.app'
        self.app.mkdir()
        self.code = self.app / 'code'
        self.code.write_bytes(b'good')
        self.manifest = self.root / 'Lulu.components.v2.json'
        self.manifest.write_bytes(b'expected')

    def verify(self):
        return verify_runtime_component_manifest(self.app, self.manifest)






    def test_cold_signed_package_verification_reads_tree_once(self):
        from test_delivery_package_v2_public import signed_delivery
        from src.core.agent_entry import component_manifest, delivery_identity
        app = signed_delivery(self.root / 'cold-signed')
        manifest = app.parent / 'Lulu.components.v2.json'
        with patch.object(delivery_identity, '_inventory',
                          wraps=delivery_identity._inventory) as inventory:
            result = component_manifest.verify_component_manifest(app, manifest)
        self.assertEqual(result['state'], 'ready')
        # Reading a large installed bundle twice delays every fresh GUI process.
        # Both the component comparison and complete tree identity use one read.
        self.assertEqual(inventory.call_count, 1)

    def test_single_scan_still_rejects_changed_directory_before_signature(self):
        from test_delivery_package_v2_public import signed_delivery
        from src.core.agent_entry import component_manifest
        app = signed_delivery(self.root / 'directory-changed')
        manifest = app.parent / 'Lulu.components.v2.json'
        (app / 'Contents/UnexpectedEmptyDirectory').mkdir()
        with patch.object(component_manifest, 'verify_final_signature',
                          side_effect=AssertionError('Tree mismatch must fail first')):
            with self.assertRaisesRegex(RuntimeError, 'COMPONENT_MISMATCH'):
                component_manifest.verify_component_manifest(app, manifest)
