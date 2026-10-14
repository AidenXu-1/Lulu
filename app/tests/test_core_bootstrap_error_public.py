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
    def test_two_first_requests_share_one_initialized_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entered, release = Event(), Event()
            calls, results, errors = [], [], []
            runtime = SimpleNamespace(gui_jobs_list=lambda: {"jobs": [{"job_id": "same-core-job"}]})
            def factory():
                calls.append("initialize"); entered.set()
                if not release.wait(3): raise RuntimeError("test release missing")
                return runtime
            dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=root, runtime_factory=factory)
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root))
            def request():
                try: results.append(dispatcher({"command": "gui.jobs.list", "arguments": {}}))
                except Exception as exc: errors.append(exc)
            with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=installer):
                first, second = Thread(target=request), Thread(target=request)
                first.start(); self.assertTrue(entered.wait(2)); second.start()
                release.set(); first.join(3); second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(calls, ["initialize"])
            self.assertEqual(results, [{"jobs": [{"job_id": "same-core-job"}]}] * 2)

    def test_product_install_lock_prevents_first_runtime_effect(self):
        import fcntl, os
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); effects = []
            def factory():
                effects.append("runtime")
                return SimpleNamespace(gui_jobs_list=lambda: {"jobs": []})
            dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=root,
                runtime_factory=factory)
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root))
            descriptor = os.open(root / "product-install.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=installer):
                    with self.assertRaisesRegex(RuntimeError, "PRODUCT_INSTALLATION_BUSY"):
                        dispatcher({"command": "gui.jobs.list", "arguments": {}})
                self.assertEqual(effects, [])
            finally: os.close(descriptor)

    def test_missing_job_key_is_not_disguised_as_component_transport_failure(self):
        def unavailable():
            raise CliRuntimeTrustError("TRUST_ANCHOR_INVALID", "JOB_HMAC_KEY_MISSING")
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            dispatcher = CoreCommandDispatcher(product_identity={"adapter_id": "agent_cli"},
                resource_root=root, runtime_factory=unavailable)
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root))
            with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=installer):
                result = dispatcher({"command": "argv", "arguments": {"argv": ["transcribe", "prepare", "/tmp/synthetic.wav", "--output", "json"]}})
            self.assertEqual(result["exit_code"], 60)
            self.assertEqual(result["stderr"], "")
            payload = json.loads(result["stdout"])
            self.assertEqual(payload["schema_id"], "lulu.agent/v1")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "INTEGRITY_ERROR")
            self.assertEqual(payload["error"]["details"], {"reason_code": "JOB_HMAC_KEY_MISSING"})
            self.assertFalse(payload["error"]["retryable"])

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
            self.assertEqual(events, ["verify"])
