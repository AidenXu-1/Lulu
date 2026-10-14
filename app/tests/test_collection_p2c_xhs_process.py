"""小红书常驻浏览器 P1 task-only 取消的离线进程合同。"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.xhs_browser import CdpEndpoint, XhsBrowserIdentity  # noqa: E402
from core.collect.profiles import ProfileRegistry  # noqa: E402
from core.collect.xhs_process import (  # noqa: E402
    CleanupPolicy,
    XhsProcessClient,
    XhsProcessError,
    XhsTerminalReceipt,
    build_collect_request,
)
from core.collect.xhs_process_worker import (  # noqa: E402
    SCHEMA_VERSION,
    WorkerProtocolError,
    _validate_request,
)
import core.collect.xhs_process_worker as xhs_process_worker  # noqa: E402


PROFILE_URL = "https://www.xiaohongshu.com/user/profile/public_brand"
FAST_POLICY = CleanupPolicy(
    graceful_seconds=0.12,
    terminate_seconds=0.18,
    total_seconds=1.2,
)


def _sentinel_forever() -> None:
    while True:
        time.sleep(1)


def _identity(pid: int = 71001) -> XhsBrowserIdentity:
    return XhsBrowserIdentity(
        state="reused_ready",
        chrome_pid=pid,
        chrome_birth=f"fixture-birth-{pid}",
        profile_identity_digest="a" * 64,
        endpoint=CdpEndpoint(
            host="127.0.0.1",
            port=52341,
            browser_path="/devtools/browser/fixture-browser",
        ),
    )


class XhsTaskOnlyProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_data = Path(self.temp.name) / "app-data"
        self.clients: list[XhsProcessClient] = []
        self.sentinels: list[multiprocessing.Process] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        for process in self.sentinels:
            if process.is_alive():
                process.terminate()
            process.join(1)

    def _client(
        self,
        fixture_mode: str,
        *,
        identity: XhsBrowserIdentity | None = None,
        browser_is_alive=None,
        request_id: str | None = None,
    ) -> XhsProcessClient:
        client = XhsProcessClient(
            self.app_data,
            identity or _identity(),
            PROFILE_URL,
            request_id=request_id,
            fixture_mode=fixture_mode,
            policy=FAST_POLICY,
            browser_is_alive=browser_is_alive or (lambda _identity: True),
        )
        self.clients.append(client)
        return client

    def _sentinel(self) -> multiprocessing.Process:
        process = multiprocessing.get_context("spawn").Process(
            target=_sentinel_forever,
        )
        process.start()
        self.sentinels.append(process)
        self.assertTrue(process.is_alive())
        return process

    def test_success_detaches_session_releases_lease_and_preserves_browser(self):
        client = self._client("success", request_id="success")
        client.start()

        outcome = client.next_outcome(2.0)

        self.assertEqual(outcome.receipt.terminal, "completed")
        self.assertEqual(outcome.receipt.browser_disposition, "preserved")
        self.assertTrue(outcome.receipt.task_worker_dead)
        self.assertTrue(outcome.receipt.result_gate_closed)
        self.assertTrue(outcome.receipt.cdp_session_detached)
        self.assertTrue(outcome.receipt.collection_lease_released)
        self.assertEqual(outcome.completion, "complete")
        self.assertEqual([item.work_id for item in outcome.items], ["fixture_work"])
        self.assertFalse(client.result_gate_open)
        self.assertFalse(
            self.app_data.joinpath(
                "collection_profiles/.xhs_browser/collection.lock"
            ).exists()
        )

    def test_worker_release_gate_precedes_request_attach_and_adapter(self):
        class Connection:
            def __init__(self):
                self.frames = []
                self.closed = False

            def recv_bytes(self, _limit):
                raise AssertionError("release gate must precede request receive")

            def send_bytes(self, raw):
                self.frames.append(json.loads(raw.decode("utf-8")))

            def close(self):
                self.closed = True

        connection = Connection()
        with patch.object(
            xhs_process_worker,
            "homepage_collection_available",
            return_value=False,
        ):
            xhs_process_worker.task_worker_main(connection)

        self.assertTrue(connection.closed)
        self.assertEqual(
            connection.frames,
            [{
                "schema_version": SCHEMA_VERSION,
                "request_id": "unknown",
                "event": "worker_failed",
                "code": "release_disabled",
                "cdp_session_detached": False,
            }],
        )

    def test_cooperative_cancel_is_task_only_and_finishes_within_three_seconds(self):
        client = self._client("cooperative_cancel", request_id="cooperative")
        client.start()
        self.assertTrue(client.wait_until_attached(1.0))

        started = time.monotonic()
        client.cancel()
        outcome = client.next_outcome(3.0)
        elapsed = time.monotonic() - started

        self.assertLessEqual(elapsed, 3.0)
        self.assertEqual(outcome.receipt.terminal, "cancelled_task_only")
        self.assertEqual(outcome.receipt.browser_disposition, "preserved")
        self.assertEqual((outcome.completion, outcome.reason_code), ("cancelled", "cancelled"))
        self.assertEqual(outcome.items, ())
        self.assertFalse(client.result_gate_open)

    def test_permanently_blocked_worker_is_only_process_force_stopped(self):
        chrome = self._sentinel()
        helper = self._sentinel()
        identity = _identity(chrome.pid)
        client = self._client(
            "ignore_cancel",
            identity=identity,
            request_id="forced",
            browser_is_alive=lambda current: (
                current.chrome_pid == chrome.pid and chrome.is_alive()
            ),
        )
        client.start()
        self.assertTrue(client.wait_until_attached(1.0))
        worker_pid = client.worker_pid

        started = time.monotonic()
        client.cancel()
        outcome = client.next_outcome(3.0)

        self.assertLessEqual(time.monotonic() - started, 3.0)
        self.assertEqual(outcome.receipt.terminal, "cancelled_task_only")
        self.assertEqual(outcome.receipt.browser_disposition, "preserved")
        self.assertIsNotNone(worker_pid)
        self.assertNotIn(worker_pid, {chrome.pid, helper.pid})
        self.assertTrue(chrome.is_alive())
        self.assertTrue(helper.is_alive())
        self.assertTrue(client.join(0))

    def test_late_result_after_cancel_has_zero_callback_or_item_side_effect(self):
        callbacks = []
        progress = []
        client = self._client("late_result_after_cancel", request_id="late")
        client.start()
        self.assertTrue(client.wait_until_attached(1.0))

        client.cancel()
        outcome = client.next_outcome(
            3.0,
            on_progress=lambda label, count: progress.append((label, count)),
            on_result=callbacks.append,
        )

        self.assertEqual(outcome.receipt.terminal, "cancelled_task_only")
        self.assertEqual(outcome.items, ())
        self.assertEqual(callbacks, [])
        self.assertEqual(progress, [])

    def test_controller_lease_is_untouched_by_success_or_cancel(self):
        registry = ProfileRegistry(self.app_data)
        lease = registry.acquire_xhs_controller(
            owner_pid=9001,
            owner_birth="fixture-controller",
            identity_is_alive=lambda _pid, _birth: True,
        )
        self.addCleanup(lease.release)
        before = lease.lock_path.read_bytes()

        success = self._client("success", request_id="controller_success")
        success.start()
        self.assertEqual(success.next_outcome(2.0).receipt.terminal, "completed")

        cancelled = self._client("ignore_cancel", request_id="controller_cancel")
        cancelled.start()
        self.assertTrue(cancelled.wait_until_attached(1.0))
        cancelled.cancel()
        self.assertEqual(
            cancelled.next_outcome(3.0).receipt.terminal,
            "cancelled_task_only",
        )

        self.assertTrue(lease.lock_path.exists())
        self.assertEqual(lease.lock_path.read_bytes(), before)

    def test_cancel_releases_collection_lease_for_immediate_retry(self):
        first = self._client("ignore_cancel", request_id="first")
        first.start()
        self.assertTrue(first.wait_until_attached(1.0))
        first.cancel()
        self.assertEqual(
            first.next_outcome(3.0).receipt.terminal,
            "cancelled_task_only",
        )

        retry = self._client("success", request_id="retry")
        retry.start()
        outcome = retry.next_outcome(2.0)

        self.assertEqual(outcome.receipt.terminal, "completed")
        self.assertEqual([item.work_id for item in outcome.items], ["fixture_work"])

    def test_second_concurrent_task_fails_closed_without_touching_first(self):
        first = self._client("ignore_cancel", request_id="owner")
        first.start()
        self.assertTrue(first.wait_until_attached(1.0))
        second = self._client("success", request_id="contender")

        with self.assertRaises(XhsProcessError) as caught:
            second.start()
        self.assertEqual(caught.exception.code, "xhs_collection_busy")
        self.assertTrue(first.join(0) is False)

        first.cancel()
        first.next_outcome(3.0)

    def test_browser_loss_is_reported_honestly_without_changing_task_cleanup(self):
        client = self._client(
            "success",
            request_id="browser_lost",
            browser_is_alive=lambda _identity: False,
        )
        client.start()
        outcome = client.next_outcome(2.0)

        self.assertEqual(outcome.receipt.terminal, "completed")
        self.assertEqual(outcome.receipt.browser_disposition, "lost")
        self.assertTrue(outcome.receipt.collection_lease_released)

    def test_terminal_receipt_requires_exact_v2_task_only_proofs(self):
        valid = {
            "schema_version": SCHEMA_VERSION,
            "request_id": "receipt",
            "terminal": "cancelled_task_only",
            "task_worker_dead": True,
            "result_gate_closed": True,
            "cdp_session_detached": True,
            "collection_lease_released": True,
            "browser_disposition": "preserved",
        }
        receipt = XhsTerminalReceipt.from_frame(valid)
        self.assertEqual(receipt.terminal, "cancelled_task_only")

        invalid_frames = [
            {**valid, "task_worker_dead": False},
            {**valid, "result_gate_closed": 1},
            {**valid, "cdp_session_detached": False},
            {**valid, "collection_lease_released": False},
            {**valid, "browser_disposition": "unknown"},
            {**valid, "terminal": "cancelled_clean"},
            {
                **valid,
                "known_children_alive": 0,
            },
        ]
        for frame in invalid_frames:
            with self.subTest(frame=frame), self.assertRaises(XhsProcessError) as caught:
                XhsTerminalReceipt.from_frame(frame)
            self.assertEqual(caught.exception.code, "xhs_protocol_invalid")

    def test_request_binds_verified_p0_identity_and_rejects_bad_endpoint(self):
        request = build_collect_request(
            PROFILE_URL,
            _identity(),
            request_id="bound",
        )
        self.assertEqual(request["schema_version"], SCHEMA_VERSION)
        self.assertEqual(request["cdp_host"], "127.0.0.1")
        self.assertEqual(request["cdp_port"], 52341)
        self.assertEqual(request["browser_ws_path"], "/devtools/browser/fixture-browser")
        self.assertEqual(len(request["browser_identity_id"]), 64)

        bad = XhsBrowserIdentity(
            state="reused_ready",
            chrome_pid=1,
            chrome_birth="bad",
            profile_identity_digest="b" * 64,
            endpoint=CdpEndpoint(
                host="0.0.0.0",
                port=52341,
                browser_path="/devtools/browser/bad",
            ),
        )
        with self.assertRaises(XhsProcessError) as caught:
            build_collect_request(PROFILE_URL, bad)
        self.assertEqual(caught.exception.code, "xhs_protocol_invalid")

    def test_worker_revalidates_loopback_homepage_and_rejects_transient_values(self):
        valid = build_collect_request(
            PROFILE_URL,
            _identity(),
            request_id="worker_validate",
        )
        self.assertEqual(_validate_request(valid), valid)

        invalid = (
            {**valid, "cdp_host": "localhost"},
            {**valid, "homepage_url": "https://user@www.xiaohongshu.com/user/profile/id"},
            {**valid, "homepage_url": f"{PROFILE_URL}?xsec_token=secret"},
            {**valid, "homepage_url": f"{PROFILE_URL}#fragment"},
            {**valid, "homepage_url": "https://www.xiaohongshu.com:bad/user/profile/id"},
            {**valid, "browser_ws_path": "/devtools/page/not-browser"},
        )
        for frame in invalid:
            with self.subTest(frame=frame), self.assertRaises(WorkerProtocolError):
                _validate_request(frame)

    def test_policy_keeps_force_stop_budget_below_three_seconds(self):
        for kwargs in (
            {"graceful_seconds": 1.5, "terminate_seconds": 1.5, "total_seconds": 3.0},
            {"graceful_seconds": 0.5, "terminate_seconds": 0.5, "total_seconds": 3.1},
            {"graceful_seconds": True, "terminate_seconds": 0.5, "total_seconds": 2.0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CleanupPolicy(**kwargs)

    def test_production_supervisor_contains_no_browser_tree_kill_or_old_clean_receipt(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "src/core/collect/xhs_process.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "killpg(",
            "os.kill(",
            "pgrep",
            "known_children_alive",
            "leases_recovered",
            "port_available",
            "completed_clean",
            "cancelled_clean",
            "acquire_xhs_process_leases",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
