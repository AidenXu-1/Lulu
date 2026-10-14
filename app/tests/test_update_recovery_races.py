"""Deterministic two-entry scheduling with real temp flock, no real App/process.

All process identities, Popen, Core stopping and process enumeration are in-memory
substitutes. The actual recovery function operates only on tiny temporary Apps.
"""
from contextlib import contextmanager
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_update_launch as fixture_module
from src.core.updater import helper_entry, restart_recovery as recovery


class RecoveryRaceReview(unittest.TestCase):
    setUp = fixture_module.UpdateLaunchTests.setUp
    plan = fixture_module.UpdateLaunchTests.plan

    def test_other_copy_or_symlink_keeps_recovery_before_business_startup(self):
        from src.core.updater.models import UpdateError
        path, plan = self.plan()
        other = self.target.parent / 'other-executable'
        other.write_text('fixture')
        link = self.target.parent / 'linked-executable'
        link.symlink_to(self.executable)
        for executable in (other, link):
            with self.subTest(executable=executable.name), \
                 patch.object(recovery, 'InstallTransaction', return_value=self.tx), \
                 patch.object(recovery, 'matches_process', return_value=False), \
                 patch.object(recovery, 'running_app', return_value=False), \
                 patch.object(recovery.subprocess, 'Popen') as spawn:
                with self.assertRaises(UpdateError) as caught:
                    recovery.launch_pending_recovery(root=self.tx.root, executable=executable)
                self.assertEqual(caught.exception.code, 'recovery_source')
                spawn.assert_not_called()
                self.assertTrue(self.tx.lock.exists())
                self.assertTrue((self.target/'old').is_file())

    def run_race(self, gap, recovery_wins=False):
        path, plan = self.plan()
        local = threading.local()
        mutex = threading.RLock()
        active = set()
        workers = []
        failures = []
        events = []
        normal_results = []
        recovery_results = []
        launched_recovery = []
        final_reopens = []
        escaped_to_business = []
        first_has_guard = threading.Event()
        let_first_proceed = threading.Event()
        second_has_guard = threading.Event()
        second_started = threading.Event()
        actual_guard = recovery.helper_guard
        actual_recover = self.tx.recover

        def note(kind, value=None):
            with mutex:
                events.append((round(time.monotonic(), 3), getattr(local, 'pid', None), kind, value))

        def pid():
            return getattr(local, 'pid', 99999)

        def alive(number, *_):
            with mutex:
                return int(number) in active

        def others(_target, *, exclude_pid=None):
            with mutex:
                return bool(active-{exclude_pid})

        @contextmanager
        def guarded(root, **kwargs):
            with actual_guard(root, **kwargs) as acquired:
                note('guard', acquired)
                if acquired and pid() == 1001:
                    first_has_guard.set()
                    if not gap and not recovery_wins:
                        assert let_first_proceed.wait(2)
                if acquired and pid() == 1002:
                    second_has_guard.set()
                if acquired and pid() == 2001 and recovery_wins:
                    start(1002, ordinary)
                    self.assertTrue(second_started.wait(1))
                yield acquired

        def start(number, action):
            with mutex:
                assert number not in active
                active.add(number)
            def body():
                local.pid = number
                try:
                    action()
                except BaseException as exc:
                    with mutex:
                        failures.append((number, type(exc).__name__, str(exc)))
                finally:
                    note('exit')
                    with mutex:
                        active.remove(number)
            worker = threading.Thread(target=body, name='isolated-recovery-'+str(number))
            workers.append(worker)
            worker.start()

        def ordinary():
            if pid() == 1002:
                second_started.set()
            result = recovery.launch_pending_recovery(root=self.tx.root, executable=self.executable)
            note('normal-result', result)
            with mutex:
                normal_results.append((pid(), result))
                if not result and self.tx.lock.exists():
                    escaped_to_business.append(pid())

        def spawn(argv, **_kwargs):
            note('spawn', argv[1] if len(argv)>1 else 'old-gui')
            if '--lulu-update-recover' not in argv:
                final_reopens.append(pid())
                return Mock(pid=3000)
            index = len(launched_recovery)
            number = 2001+index
            launched_recovery.append(number)
            self.assertLessEqual(len(launched_recovery), 2, 'more than one handoff')
            if gap and index == 0:
                # While A still owns the guard, B arrives. C exists as a process
                # but is held before its guard attempt until B definitely wins.
                start(1002, ordinary)
                self.assertTrue(second_started.wait(1))
            def child():
                if gap and index == 0:
                    assert second_has_guard.wait(2), 'second ordinary entry must win the gap'
                result = recovery.run_recovery(*argv[2:])
                note('recovery-result', result)
                recovery_results.append((number, result))
            start(number, child)
            return Mock(pid=number)

        def restore(plan_path):
            self.assertEqual(plan_path, path)
            with mutex:
                self.assertEqual(active, {pid()}, 'no other entry may remain at recovery')
            note('recover')
            return actual_recover(plan_path)

        with patch.object(recovery, 'InstallTransaction', return_value=self.tx), \
             patch.object(recovery, 'helper_guard', side_effect=guarded), \
             patch.object(recovery, 'running_app', side_effect=others), \
             patch.object(recovery, 'matches_process', side_effect=alive), \
             patch.object(recovery, 'current_identity', side_effect=lambda: ('start-'+str(pid()), str(self.executable))), \
             patch.object(recovery.os, 'getpid', side_effect=pid), \
             patch.object(recovery.subprocess, 'Popen', side_effect=spawn), \
             patch.object(recovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'apfs', '')), \
             patch.object(helper_entry, 'stop_update_core') as stop_core, \
             patch.object(self.tx, 'recover', side_effect=restore) as restore_call:
            start(1001, ordinary)
            self.assertTrue(first_has_guard.wait(1))
            if not gap and not recovery_wins:
                start(1002, ordinary)
                self.assertTrue(second_started.wait(1))
                let_first_proceed.set()
            deadline = time.monotonic()+9
            while time.monotonic()<deadline:
                with mutex:
                    remaining = bool(active)
                if not remaining:
                    break
                time.sleep(.02)
            for worker in workers:
                worker.join(.1)
            self.assertFalse(any(worker.is_alive() for worker in workers), events)
            self.assertFalse(failures, failures)
            self.assertFalse(escaped_to_business, events)
            self.assertEqual(sorted(normal_results), [(1001, True), (1002, True)], events)
            self.assertEqual(len(final_reopens), 1, events)
            self.assertEqual(sum(result == 0 for _, result in recovery_results), 1, events)
            self.assertEqual(restore_call.call_count, 1, events)
            self.assertEqual(stop_core.call_count, 1, events)
            if gap:
                self.assertEqual(recovery_results, [(2001, 3), (2002, 0)], events)
            else:
                self.assertEqual(recovery_results, [(2001, 0)], events)
        self.assertFalse(self.tx.lock.exists())
        self.assertTrue((self.target/'old').is_file())
        self.assertEqual(self.store.load().ui_state, 'install_error_rollback')

    def test_two_ordinary_entries_elect_one_recovery(self):
        self.run_race(gap=False)

    def test_second_entry_wins_spawn_gap_then_one_handoff_recovers(self):
        self.run_race(gap=True)

    def test_recovery_child_holds_guard_until_late_ordinary_entry_yields(self):
        self.run_race(gap=False, recovery_wins=True)
