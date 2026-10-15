"""GUI uses the measured Core for all license actions, without copying secrets."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import json
import sys
from unittest.mock import Mock, patch

from src.core.license.models import LicenseCache, LicenseStatus, FULL_ACCESS


class LicenseSingleOwnerTests(unittest.TestCase):


    def test_retired_license_entry_never_opens_credentials_or_runtime(self):
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        with TemporaryDirectory() as directory:
            runtime = Mock(side_effect=AssertionError("No runtime for retired command"))
            dispatcher = CoreCommandDispatcher(
                product_identity={}, resource_root=Path(directory), runtime_factory=runtime,
            )
            with patch("src.core.agent_entry.product_install.build_default_product_installer",
                       side_effect=AssertionError("No installer or credentials")) as installer, patch.dict(sys.modules, {"src.core.license.external_service": None}):
                for action in ("startup_status", "activate", "refresh", "deactivate", "clear_cache"):
                    with self.subTest(action=action):
                        result = dispatcher({"command": "gui.license", "arguments": {"action": action}})
                        self.assertFalse(result["allowed"])
                        self.assertEqual(result["code"], "feature_retired")
                        self.assertIn("无需激活", result["message"])
                for action in ("status", "activate", "refresh", "deactivate", "resume-deactivation"):
                    reply = dispatcher({"command": "argv", "arguments": {"argv": ["auth", action, "--output", "json"]}})
                    self.assertEqual(reply["exit_code"], 30)
                    self.assertEqual(reply["stderr"], "")
                    self.assertEqual(json.loads(reply["stdout"])["error"]["details"]["reason_code"], "LICENSE_FEATURE_RETIRED")
            runtime.assert_not_called()
            installer.assert_not_called()
            self.assertFalse(dispatcher._runtime_loaded)
