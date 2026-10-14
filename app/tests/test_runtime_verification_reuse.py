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

    def test_shared_runtime_calls_reuse_validation_and_return_detached_results(self):
        with patch('src.core.agent_entry.component_manifest.verify_component_manifest',
                   return_value={'state': 'ready', 'extra_paths': []}) as verifier:
            result = self.verify()
            result['extra_paths'].append('untrusted')
            self.assertEqual(self.verify(), {'state': 'ready', 'extra_paths': []})
            self.assertEqual(verifier.call_count, 1)

    def test_content_change_with_restored_mtime_and_manifest_change_revalidate(self):
        with patch('src.core.agent_entry.component_manifest.verify_component_manifest',
                   return_value={'state': 'ready'}) as verifier:
            self.verify()
            old = self.code.stat()
            self.code.write_bytes(b'evil')
            os.utime(self.code, ns=(old.st_atime_ns, old.st_mtime_ns))
            self.verify()
            self.manifest.write_bytes(b'changed!')
            self.verify()
            self.assertEqual(verifier.call_count, 3)

    def test_failure_is_not_cached_and_change_during_verification_is_rejected(self):
        with patch('src.core.agent_entry.component_manifest.verify_component_manifest',
                   side_effect=RuntimeError('bad signature')) as verifier:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'bad signature'):
                    self.verify()
            self.assertEqual(verifier.call_count, 2)
        def mutate(*args):
            self.manifest.write_bytes(b'different manifest')
            return {'state': 'ready'}
        with patch('src.core.agent_entry.component_manifest.verify_component_manifest', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, 'GENERATION_CHANGED'):
                self.verify()

    def test_deleted_external_manifest_does_not_reuse_success(self):
        with patch('src.core.agent_entry.component_manifest.verify_component_manifest',
                   return_value={'state': 'ready'}) as verifier:
            self.verify()
            self.manifest.unlink()
            verifier.side_effect = FileNotFoundError('manifest missing')
            with self.assertRaises(FileNotFoundError):
                self.verify()
            self.assertEqual(verifier.call_count, 2)

    def test_real_signed_package_identity_and_peer_startup_share_one_scan(self):
        from test_delivery_package_v2_public import signed_delivery
        from src.core.agent_entry import component_manifest, requirements
        app = signed_delivery(self.root / 'signed')
        manifest = app.parent / 'Lulu.components.v2.json'
        with patch.object(requirements, 'component_manifest_path', return_value=manifest), \
             patch.object(component_manifest, 'verify_component_manifest',
                          wraps=component_manifest.verify_component_manifest) as verifier:
            verify_runtime_component_manifest(app, manifest)
            requirements.load_peer_requirements(app, audit_session_id=1)
            requirements.load_gui_peer_requirements(app, audit_session_id=1)
            self.assertEqual(verifier.call_count, 1)
            (app/'Contents/MacOS/lulu-agent-cli').write_bytes(b'tampered executable')
            with self.assertRaises(RuntimeError):
                requirements.load_peer_requirements(app, audit_session_id=1)
            self.assertEqual(verifier.call_count, 2)
