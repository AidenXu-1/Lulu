"""Crash recovery never steals a live browser, owner or XHS lease."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.core.collect.profiles import ProfileRegistry, ProfileError


class LeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = ProfileRegistry(Path(self.temp.name))
        self.resolution = self.registry.resolve("douyin", create=True)
        probe = patch("src.core.collect.browser_shell._port_is_available", return_value=True)
        self.port = probe.start()
        self.addCleanup(probe.stop)

    def stale(self, lease):
        previous = json.loads(lease.lock_path.read_text())
        previous.update(pid=99999999, started_at="2000-01-01T00:00:00+00:00")
        lease.lock_path.write_text(json.dumps(previous))
        return previous

    def assert_busy(self, action, code="profile_busy"):
        with self.assertRaises(ProfileError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_dead_owner_is_replaced_and_old_token_cannot_release_new_owner(self):
        first = self.registry.acquire("douyin")
        self.stale(first)
        second = self.registry.acquire("douyin")
        self.addCleanup(second.release)
        first.release()
        self.assertTrue(second.lock_path.exists())
        self.assertEqual(json.loads(second.lock_path.read_text())["token"], second.token)


    def test_dead_app_with_live_browser_cannot_be_reclaimed(self):
        lease = self.registry.acquire("douyin")
        self.stale(lease)
        singleton = self.resolution.path / "SingletonLock"
        singleton.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
        self.assert_busy(lambda: self.registry.acquire("douyin"))
        self.assertTrue(lease.lock_path.exists())






    def test_racing_recovery_has_only_one_winner(self):
        first = self.registry.acquire("douyin")
        self.stale(first)
        winners, errors = [], []
        start = threading.Barrier(2)
        def acquire():
            start.wait()
            try:
                winners.append(self.registry.acquire("douyin"))
            except ProfileError as exc:
                errors.append(exc.code)
        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(winners), 1)
        self.assertEqual(errors, ["profile_busy"])
        winners[0].release()
