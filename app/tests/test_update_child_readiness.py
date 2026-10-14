"""No process launch, system signals, sleeps or user state."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.core.updater import helper_entry


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class UpdateChildReadinessTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.child = Mock(pid=42)
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.tx = Mock()
        self.tx.validate_ready.return_value = True
        self.path = Mock(spec=Path)
        self.path.exists.return_value = True
        self.path.is_symlink.return_value = False
        self.identity = (42, "fixture-start", "/fixture/Lulu")

    def wait(self):
        with patch.object(helper_entry.time, "monotonic", self.clock.monotonic), \
                patch.object(helper_entry.time, "sleep", self.clock.sleep), \
                patch.object(helper_entry, "matches_process", return_value=True):
            return helper_entry.wait_for_child_ready(
                self.tx, self.path, {}, self.child, self.identity, timeout=45)

    def test_ready_child_must_survive_observation_before_success(self):
        self.assertTrue(self.wait())
        self.assertGreaterEqual(self.clock.now, 8)
        self.child.terminate.assert_not_called()

    def test_crash_after_ready_is_not_success(self):
        self.child.poll.side_effect = lambda: 1 if self.clock.now >= 3 else None
        self.assertFalse(self.wait())
        self.assertLess(self.clock.now, 4)
        self.child.terminate.assert_not_called()

    def test_no_ready_stops_child_and_confirms_exit_before_rollback(self):
        self.path.exists.return_value = False
        self.assertFalse(self.wait())
        self.child.terminate.assert_called_once_with()
        self.child.wait.assert_called_once_with(timeout=5)

    def test_invalid_ready_stops_child_before_rollback(self):
        self.tx.validate_ready.return_value = False
        self.assertFalse(self.wait())
        self.child.terminate.assert_called_once_with()
        self.child.wait.assert_called_once_with(timeout=5)

    def test_first_ready_after_deadline_is_rejected(self):
        self.path.exists.side_effect = lambda: self.clock.now >= 45
        self.assertFalse(self.wait())
        self.tx.validate_ready.assert_not_called()
        self.child.wait.assert_called_once_with(timeout=5)

    def test_cannot_confirm_child_exit_blocks_rollback(self):
        from src.core.updater.install import RollbackBlocked
        self.tx.validate_ready.return_value = False
        self.child.wait.side_effect = subprocess.TimeoutExpired("fixture", 5)
        with self.assertRaises(RollbackBlocked):
            self.wait()

    @patch('src.core.updater.helper_entry.PreSwapHandover')
    def test_helper_does_not_recover_or_relaunch_when_child_cannot_exit(self, _handover):
        from src.core.updater.install import RollbackBlocked
        from src.core.updater.models import UpdateSnapshot
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates/transactions/fixture/install_plan.json"
            path.parent.mkdir(parents=True)
            transaction = Mock()
            transaction.load_plan.return_value = {
                "old_pid": 1, "old_pid_start_time": "old", "old_executable_path": "/old",
                "staged_app": tmp, "target_parent": tmp,
            }
            transaction.execute_local.side_effect = RollbackBlocked("fixture")
            store = Mock()
            store.load.return_value = UpdateSnapshot()
            with patch.object(helper_entry, "InstallTransaction", return_value=transaction), \
                    patch.object(helper_entry, "StateStore", return_value=store), \
                    patch.object(helper_entry, "process_identity", return_value=None), \
                    patch.object(helper_entry.subprocess, "Popen") as popen, \
                    patch.object(helper_entry, "cleanup_successful_update") as cleanup:
                self.assertEqual(helper_entry.run_helper(str(path)), 5)
            transaction.recover.assert_not_called()
            transaction.release.assert_not_called()
            popen.assert_not_called()
            cleanup.assert_not_called()
            self.assertEqual(store.save.call_args.args[0].state, "failed")
            self.assertEqual(store.save.call_args.args[0].error_code, "update_child_still_running")
