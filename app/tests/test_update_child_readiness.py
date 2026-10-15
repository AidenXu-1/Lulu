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
