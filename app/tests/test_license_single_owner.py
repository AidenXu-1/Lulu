"""GUI uses the measured Core for all license actions, without copying secrets."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import json
from unittest.mock import Mock, patch

from src.core.license.models import LicenseCache, LicenseStatus, FULL_ACCESS


class LicenseSingleOwnerTests(unittest.TestCase):
    def test_installed_gui_never_constructs_a_local_keychain_service(self):
        from src.core.license.external_service import build_external_service
        with patch('sys.frozen', True, create=True), patch('sys.executable', '/Applications/Lulu.app/Contents/MacOS/Lulu'), patch('src.core.license.external_service.StrictKeyringStore', side_effect=AssertionError('GUI must not open license keychain')):
            service = build_external_service()
        from src.core.license.core_service import CoreLicenseService
        self.assertIsInstance(service, CoreLicenseService)

    def test_repeated_startup_is_single_owner_and_status_contains_no_cache(self):
        from src.core.license.core_service import CoreLicenseService, dispatch_license_action
        from src.core.license._service_core import LicenseService
        from src.core.license.store import MemorySecureStore
        from src.core.cli_runtime.license_projection import ReadOnlySharedLicenseProjection
        now = datetime(2026, 9, 8, 12)
        cache = LicenseCache(1, 'identity', 'anonymous', 'license', 'device', 'private-device-hash', (FULL_ACCESS,), 'private-lease-token', now, now+timedelta(days=1), now+timedelta(days=7), now)
        store = MemorySecureStore(); store.save_cache(cache)
        core = LicenseService(store, Mock(), now=lambda: now)
        projection = ReadOnlySharedLicenseProjection(store, now=lambda: now)
        calls = []
        def dispatch(command, args):
            calls.append((command, args))
            result = dispatch_license_action(core, args)
            self.assertNotIn('private-', str(result))
            self.assertNotIn('cache', result)
            return result
        gui = CoreLicenseService(dispatch)
        for _ in range(3):
            self.assertTrue(gui.startup_status().allowed)
            self.assertTrue(projection.cached_status_projection().allowed)
        self.assertIsNone(gui.cached_status_projection().cache)
        store.save_cache(LicenseCache.from_json(dict(cache.to_json(), grace_until=(now-timedelta(seconds=1)).isoformat())))
        self.assertFalse(gui.cached_status_projection().allowed)
        self.assertEqual(len(calls), 5)

    def test_requests_are_closed_and_mutations_are_never_replayed(self):
        from src.core.license.core_service import CoreLicenseService, dispatch_license_action
        local = Mock()
        for args in ({}, {'action':'clear_cache'}, {'action':'activate'}, {'action':'activate','license_code':True}, {'action':'startup_status','license_code':'x'}):
            with self.subTest(args=args), self.assertRaises(ValueError): dispatch_license_action(local, args)
        self.assertEqual(local.mock_calls, [])
        dispatch = Mock(side_effect=RuntimeError('private transport details'))
        status = CoreLicenseService(dispatch).activate('synthetic-activation-code')
        self.assertFalse(status.allowed)
        self.assertNotIn('private', status.message)
        dispatch.assert_called_once()

    def test_retired_license_entry_never_opens_credentials_or_runtime(self):
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        with TemporaryDirectory() as directory:
            runtime = Mock(side_effect=AssertionError("No runtime for retired command"))
            dispatcher = CoreCommandDispatcher(
                product_identity={}, resource_root=Path(directory), runtime_factory=runtime,
            )
            with patch("src.core.agent_entry.product_install.build_default_product_installer",
                       side_effect=AssertionError("No installer or credentials")) as installer, patch(
                "src.core.license.external_service.build_core_license_service",
                side_effect=AssertionError("No license service"),
            ) as service:
                for action in ("startup_status", "activate", "refresh", "deactivate", "clear_cache"):
                    with self.subTest(action=action):
                        result = dispatcher({"command": "gui.license", "arguments": {"action": action}})
                        self.assertFalse(result["allowed"])
                        self.assertEqual(result["code"], "feature_retired")
                        self.assertIn("无需激活", result["message"])
                from src.core.license.core_service import CoreLicenseService
                old_client = CoreLicenseService(lambda command, arguments: dispatcher(
                    {"command": command, "arguments": arguments}
                ))
                self.assertEqual(old_client.startup_status().code, "feature_retired")
                for action in ("status", "activate", "refresh", "deactivate", "resume-deactivation"):
                    reply = dispatcher({"command": "argv", "arguments": {"argv": ["auth", action, "--output", "json"]}})
                    self.assertEqual(reply["exit_code"], 30)
                    self.assertEqual(reply["stderr"], "")
                    self.assertEqual(json.loads(reply["stdout"])["error"]["details"]["reason_code"], "LICENSE_FEATURE_RETIRED")
            runtime.assert_not_called()
            installer.assert_not_called()
            service.assert_not_called()
            self.assertFalse(dispatcher._runtime_loaded)

    def test_non_status_reply_fails_closed(self):
        from src.core.license.core_service import CoreLicenseService
        for reply in ({'allowed':True}, {'allowed':'true'}, None):
            self.assertFalse(CoreLicenseService(lambda *_: reply).startup_status().allowed)
