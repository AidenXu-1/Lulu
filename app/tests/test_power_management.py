import unittest

from src.core.power_management import CaffeinateAdapter, PowerManager


class _Process:
    def __init__(self):
        self.returncode = None
        self.terminate_calls = 0
        self.wait_calls = []
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


class _Adapter:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = []
        self.fail_start = False
        self.process = _Process()

    def start(self):
        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("cannot start")
        self.process = _Process()
        return self.process

    def stop(self, process):
        self.stop_calls.append(process)

    def running(self, process):
        return process is not None and process.poll() is None


class PowerManagerTests(unittest.TestCase):
    def test_concurrent_tasks_share_one_assertion_until_last_release(self):
        adapter = _Adapter()
        manager = PowerManager(adapter=adapter, enabled=True)
        self.assertTrue(manager.acquire("transcription"))
        self.assertTrue(manager.acquire("voice"))
        self.assertEqual(adapter.start_calls, 1)
        self.assertTrue(manager.is_asserting)

        manager.release("transcription")
        self.assertEqual(adapter.stop_calls, [])
        self.assertTrue(manager.is_asserting)
        manager.release("voice")
        self.assertEqual(len(adapter.stop_calls), 1)
        self.assertFalse(manager.is_asserting)

    def test_disabling_during_work_releases_and_reenable_reacquires(self):
        adapter = _Adapter()
        manager = PowerManager(adapter=adapter, enabled=True)
        manager.acquire("aggregate")
        manager.set_enabled(False)
        self.assertFalse(manager.is_asserting)
        self.assertEqual(len(adapter.stop_calls), 1)

        manager.set_enabled(True)
        self.assertTrue(manager.is_asserting)
        self.assertEqual(adapter.start_calls, 2)

    def test_start_failure_is_closed_and_does_not_raise_into_business_task(self):
        adapter = _Adapter()
        adapter.fail_start = True
        manager = PowerManager(adapter=adapter, enabled=True)
        self.assertFalse(manager.acquire("task"))
        self.assertFalse(manager.is_asserting)
        self.assertEqual(manager.active_owners, ("task",))

    def test_shutdown_stops_process_and_clears_every_owner(self):
        adapter = _Adapter()
        manager = PowerManager(adapter=adapter, enabled=True)
        manager.acquire("one")
        manager.acquire("two")
        manager.shutdown()
        self.assertEqual(manager.active_owners, ())
        self.assertFalse(manager.is_asserting)
        self.assertEqual(len(adapter.stop_calls), 1)


class CaffeinateAdapterTests(unittest.TestCase):
    def test_uses_idle_system_sleep_assertion_only(self):
        calls = []
        process = _Process()

        def factory(args, **kwargs):
            calls.append((args, kwargs))
            return process

        adapter = CaffeinateAdapter(process_factory=factory)
        self.assertIs(adapter.start(), process)
        self.assertEqual(calls[0][0], ["/usr/bin/caffeinate", "-i"])
        adapter.stop(process)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, [1.0])


if __name__ == "__main__":
    unittest.main()
