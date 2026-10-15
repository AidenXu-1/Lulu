"""A successful bootout request can precede launchd's final removal."""
import subprocess
import unittest
from unittest.mock import patch

from src.core.agent_entry.effects import CurrentUserLaunchctlService


class LaunchctlStopCompletionTests(unittest.TestCase):
    def test_running_service_can_remain_visible_briefly_after_bootout(self):
        service = CurrentUserLaunchctlService()
        with patch.object(service, "is_loaded", side_effect=[True, True, False]), patch.object(
            service, "_run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            service.stop()
        run.assert_called_once_with(["bootout", service._service])


    def test_service_that_does_not_exit_is_rejected_at_deadline(self):
        service = CurrentUserLaunchctlService()
        with patch.object(service, "is_loaded", return_value=True), patch.object(
            service, "_run", return_value=subprocess.CompletedProcess([], 0)
        ), patch("src.core.agent_entry.effects.time.monotonic", side_effect=[0, 31]), self.assertRaisesRegex(
            RuntimeError, "AGENT_ENTRY_TRANSACTION_DRIFT"
        ):
            service.stop()
