"""Update process observations: no real process, service, App or credentials."""
import errno
import subprocess
import unittest
from unittest.mock import patch

from src.core.updater import process_identity as identity
from src.core.updater.models import UpdateError


class ProcessExitTests(unittest.TestCase):
    started = 'Mon Oct 12 01:41:41 2026'
    executable = '/Applications/Lulu Test.app/Contents/MacOS/Lulu'

    def observe(self, status, command=None):
        return subprocess.CompletedProcess([], 0,
            f'{status} {self.started} {command or self.executable}\n', '')


    def test_zombie_is_stopped_for_all_existing_exit_gates(self):
        with patch.object(identity.subprocess, 'run', return_value=self.observe('Z+', '<defunct>')):
            self.assertIsNone(identity.process_identity(123))

    def test_zombie_never_counts_as_ready_live_child(self):
        with patch.object(identity.subprocess, 'run', return_value=self.observe('Z', '<defunct>')):
            self.assertFalse(identity.matches_process(123, self.started, '<defunct>'))

    def test_comparison_uses_the_observation_without_second_query(self):
        with patch.object(identity, 'process_identity', side_effect=AssertionError('second query')):
            self.assertTrue(identity.same_identity((self.started, self.executable), self.started, self.executable))
            self.assertFalse(identity.same_identity(('different', self.executable), self.started, self.executable))


    def test_macos_exit_transition_is_reobserved_until_explicit_zombie(self):
        for status in ('?E', '?', 'S', 'R'):
            with self.subTest(status=status), patch.object(identity.time, 'sleep'), \
                    patch.object(identity.subprocess, 'run', side_effect=[
                        self.observe(status, '(sleep)'), self.observe('Z', '<defunct>')]) as run:
                self.assertIsNone(identity.process_identity(123))
                self.assertEqual(run.call_count, 2)

    def test_transition_requires_confirmed_absence_after_empty_query(self):
        with patch.object(identity.time, 'sleep'), patch.object(identity.subprocess, 'run', side_effect=[
                self.observe('?E', '(sleep)'), subprocess.CompletedProcess([], 1, '', '')]), \
                patch.object(identity.os, 'kill', side_effect=ProcessLookupError(errno.ESRCH, 'gone')):
            self.assertIsNone(identity.process_identity(123))

    def test_transition_to_another_live_identity_is_not_exit(self):
        replacement = subprocess.CompletedProcess([], 0, f'S Tue Oct 13 01:41:41 2026 {self.executable}\n', '')
        with patch.object(identity.time, 'sleep'), patch.object(identity.subprocess, 'run', side_effect=[
                self.observe('S', '(Lulu)'), replacement]):
            value = identity.process_identity(123)
            self.assertIsNotNone(value)
            self.assertFalse(identity.same_identity(value, self.started, self.executable))

    def test_stable_live_or_stopped_identity_needs_only_one_query(self):
        for status in ('S', 'T'):
            with self.subTest(status=status), patch.object(identity.subprocess, 'run', return_value=self.observe(status)) as run:
                self.assertEqual(identity.process_identity(123), (self.started, self.executable))
                run.assert_called_once()

    def test_persistent_uncertainty_is_bounded_and_never_ready(self):
        for status in ('?E', '?', 'S'):
            with self.subTest(status=status), patch.object(identity.time, 'sleep'), \
                    patch.object(identity.subprocess, 'run', return_value=self.observe(status, '(sleep)')) as run:
                with self.assertRaises(UpdateError): identity.matches_process(123, self.started, self.executable)
                self.assertLessEqual(run.call_count, 7)

    def test_failed_query_requires_confirmed_absence(self):
        failed = subprocess.CompletedProcess([], 1, '', '')
        with patch.object(identity.subprocess, 'run', return_value=failed), patch.object(identity.os, 'kill', side_effect=ProcessLookupError(errno.ESRCH, 'gone')):
            self.assertIsNone(identity.process_identity(123))
        with patch.object(identity.subprocess, 'run', return_value=failed), patch.object(identity.os, 'kill', side_effect=PermissionError()):
            with self.assertRaises(UpdateError): identity.process_identity(123)
