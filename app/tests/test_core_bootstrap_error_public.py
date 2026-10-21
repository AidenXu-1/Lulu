"""Core returns closed actionable bootstrap errors after measured dispatch."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from threading import Event, Thread

from src.core.agent_entry.entrypoints import CoreCommandDispatcher
from src.core.cli_runtime.contracts import CliRuntimeTrustError


class CoreBootstrapErrorPublicTests(unittest.TestCase):



    def test_version_keeps_installation_checks_without_bootstrapping_transcription(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resources = root / 'Lulu.app/Contents/Resources'
            resources.mkdir(parents=True)
            (resources / "component_delivery.v2.json").write_text("fixture-marker")
            events = []
            def unavailable():
                events.append("runtime")
                raise CliRuntimeTrustError("TRUST_ANCHOR_INVALID", "JOB_HMAC_KEY_MISSING")
            dispatcher = CoreCommandDispatcher(product_identity={"adapter_id": "agent_cli", "shared": {
                "product_version": "0.3.2", "product_build": "fixture-build", "channel": "external-candidate"}},
                resource_root=resources, runtime_factory=unavailable)
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root),
                verify_installed=lambda: events.append("verify"), _all_targets=lambda: {})
            with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=installer):
                result = dispatcher({"command": "argv", "arguments": {"argv": ["version", "--output", "json"]}})
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(json.loads(result["stdout"])["ok"])
            self.assertEqual(json.loads(result["stdout"])["result"]["formal_cli_state"], "available")
            self.assertEqual(events, ["verify"])

    def test_version_rejects_invalid_installation_before_reporting_entry_available(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resources = root / 'Lulu.app/Contents/Resources'
            resources.mkdir(parents=True)
            (resources / 'component_delivery.v2.json').write_text('fixture-marker')
            events = []
            def invalid():
                events.append('verify')
                raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
            dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=resources,
                runtime_factory=lambda: events.append('runtime'))
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root),
                verify_installed=invalid, _all_targets=lambda: {})
            with patch('src.core.agent_entry.product_install.build_default_product_installer', return_value=installer):
                with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_COMPONENT_MISMATCH'):
                    dispatcher({'command': 'argv', 'arguments': {'argv': ['version', '--output', 'json']}})
            self.assertEqual(events, ['verify'])
