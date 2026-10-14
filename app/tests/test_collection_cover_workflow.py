"""C0 + R1 封面共享工作流的 fixture-only 测试。"""

from __future__ import annotations

from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import COLLECT_COVER_CONTRACT_VERSION  # noqa: E402
from src.core.collect.contracts import CollectResult  # noqa: E402
from src.core.collect.cover import CoverReceipt  # noqa: E402
from src.core.collect.cover_workflow import CoverWorkflowService  # noqa: E402
from src.core.collect.models import CollectedItem  # noqa: E402
from src.core.collect.preferences import (  # noqa: E402
    CollectCoverPreferenceService,
    CollectCoverPreferenceSnapshot,
)


GOOD_SHA = "a" * 64


def downloaded(
    work_id: str,
    *,
    status: str = "downloaded",
    relative_path: str | None = None,
) -> CoverReceipt:
    return CoverReceipt(
        status=status,
        relative_path=relative_path or f"xiaohongshu/{work_id}.jpg",
        size=17,
        sha256=GOOD_SHA,
        mime_type="image/jpeg",
    )


def item(
    work_id: str,
    *,
    cover_url: str | None = None,
    cover_status: str = "url_only",
    cover_local_path: str = "",
    last_error_code: str = "",
) -> CollectedItem:
    return CollectedItem(
        aweme_id="",
        platform="xiaohongshu",
        work_id=work_id,
        title=f"work-{work_id}",
        cover_url=(
            f"https://images.example.test/{work_id}.jpg"
            if cover_url is None
            else cover_url
        ),
        cover_status=cover_status,
        cover_local_path=cover_local_path,
        last_error_code=last_error_code,
    )


class FakeLocalizer:
    def __init__(self, handler=None, *, startup=()) -> None:
        self.handler = handler or (
            lambda platform, work_id, url, is_cancelled: downloaded(work_id)
        )
        self.startup_recovery_receipts = tuple(startup)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def localize(
        self,
        *,
        platform: str,
        work_id: str,
        url: str,
        is_cancelled=None,
    ) -> CoverReceipt:
        with self._lock:
            self.calls.append(
                {
                    "platform": platform,
                    "work_id": work_id,
                    "url": url,
                    "is_cancelled": is_cancelled,
                }
            )
        return self.handler(platform, work_id, url, is_cancelled)


class FakeConfig:
    def __init__(self, value=False, marker=COLLECT_COVER_CONTRACT_VERSION):
        self.collect_cover = value
        self.collect_cover_contract_version = marker

    def save(self):
        return True


class CoverWorkflowCase(unittest.TestCase):
    def run_workflow(
        self,
        localizer: FakeLocalizer,
        items,
        *,
        include_cover=True,
        result=None,
        is_cancelled=None,
    ):
        collect_result = result or CollectResult.complete(tuple(items))
        return CoverWorkflowService(localizer).run(
            collect_result,
            snapshot=CollectCoverPreferenceSnapshot(include_cover),
            is_cancelled=is_cancelled,
        )

    def test_task_snapshot_is_immutable_after_preference_changes(self):
        config = FakeConfig(False)
        preferences = CollectCoverPreferenceService(config)
        frozen = preferences.snapshot()
        self.assertTrue(preferences.set(True).success)

        localizer = FakeLocalizer()
        old_task = self.run_workflow(
            localizer, [item("old")], include_cover=frozen.include_cover
        )
        self.assertEqual(localizer.calls, [])
        self.assertEqual(
            old_task.collect_result.items[0].cover_status, "not_requested"
        )

        new_task = self.run_workflow(
            localizer,
            [item("new")],
            include_cover=preferences.snapshot().include_cover,
        )
        self.assertEqual(new_task.collect_result.items[0].cover_status, "downloaded")
        self.assertEqual([call["work_id"] for call in localizer.calls], ["new"])

    def test_disabled_snapshot_never_calls_localizer(self):
        localizer = FakeLocalizer()
        result = self.run_workflow(
            localizer,
            [item("url"), item("none", cover_url="")],
            include_cover=False,
        )
        self.assertEqual(localizer.calls, [])
        self.assertEqual(
            [entry.cover_status for entry in result.collect_result.items],
            ["not_requested", "not_requested"],
        )
        self.assertEqual(result.collect_result.items[0].cover_url, item("url").cover_url)

    def test_untrusted_identity_or_url_fails_closed_before_localizer(self):
        localizer = FakeLocalizer()
        missing_id = item("")
        insecure = item("http", cover_url="http://images.example.test/a.jpg")
        credential = item(
            "credential",
            cover_url="https://user:secret@images.example.test/a.jpg",
        )
        result = self.run_workflow(
            localizer, [missing_id, insecure, credential]
        )
        self.assertEqual(localizer.calls, [])
        self.assertTrue(
            all(entry.cover_status == "url_only" for entry in result.collect_result.items)
        )

    def test_download_concurrency_is_fixed_at_two(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        two_started = threading.Event()

        def handler(_platform, work_id, _url, _cancel):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_started.set()
            self.assertTrue(two_started.wait(1.0))
            time.sleep(0.02)
            with lock:
                active -= 1
            return downloaded(work_id)

        localizer = FakeLocalizer(handler)
        result = self.run_workflow(
            localizer, [item(str(index)) for index in range(6)]
        )
        self.assertEqual(maximum, 2)
        self.assertEqual(len(localizer.calls), 6)
        self.assertTrue(
            all(entry.cover_status == "downloaded" for entry in result.collect_result.items)
        )

    def test_cancel_stops_new_slots_and_keeps_completed_downloads(self):
        cancelled = threading.Event()
        first_pair = threading.Barrier(2)

        def handler(_platform, work_id, _url, _is_cancelled):
            first_pair.wait(timeout=1.0)
            if work_id == "1":
                cancelled.set()
            return downloaded(work_id)

        localizer = FakeLocalizer(handler)
        result = self.run_workflow(
            localizer,
            [item(str(index)) for index in range(1, 6)],
            is_cancelled=cancelled.is_set,
        )
        statuses = [entry.cover_status for entry in result.collect_result.items]
        self.assertEqual(statuses[:2], ["downloaded", "downloaded"])
        self.assertEqual(statuses[2:], ["url_only", "url_only", "url_only"])
        self.assertEqual(len(localizer.calls), 2)
        self.assertTrue(
            all(
                outcome.reason_code == "cover_cancelled"
                for outcome in result.outcomes[2:]
            )
        )

    def test_kernel_cancel_receipt_returns_url_only_and_preserves_url(self):
        localizer = FakeLocalizer(
            lambda *_args: CoverReceipt.failed("cover_cancelled")
        )
        original = item("cancelled")
        result = self.run_workflow(localizer, [original])
        updated = result.collect_result.items[0]
        self.assertEqual(updated.cover_status, "url_only")
        self.assertEqual(updated.cover_url, original.cover_url)
        self.assertEqual(updated.cover_local_path, "")

    def test_failure_preserves_url_existing_error_and_main_result(self):
        localizer = FakeLocalizer(
            lambda *_args: CoverReceipt.failed("cover_timeout")
        )
        original = item("failure", last_error_code="platform_changed")
        main = CollectResult.partial(
            (original,),
            reason_code="rate_limited",
            next_action="稍后重试",
            cursor={"page": 2},
        )
        result = self.run_workflow(localizer, [original], result=main)
        updated = result.collect_result
        self.assertEqual(updated.completion, main.completion)
        self.assertEqual(updated.reason_code, main.reason_code)
        self.assertEqual(updated.next_action, main.next_action)
        self.assertEqual(dict(updated.cursor or {}), {"page": 2})
        self.assertEqual(updated.items[0].cover_url, original.cover_url)
        self.assertEqual(updated.items[0].cover_status, "failed")
        self.assertEqual(updated.items[0].last_error_code, "platform_changed")

    def test_localizer_exception_fails_closed_without_changing_main_completion(self):
        def explode(*_args):
            raise RuntimeError("remote URL must not leak")

        original = item("exception")
        main = CollectResult.cancelled((original,))
        result = self.run_workflow(
            FakeLocalizer(explode), [original], result=main
        )
        self.assertEqual(result.collect_result.completion, "cancelled")
        self.assertEqual(result.collect_result.reason_code, "cancelled")
        self.assertEqual(result.collect_result.items[0].cover_status, "failed")
        self.assertEqual(
            result.outcomes[0].reason_code, "cover_commit_failed"
        )
        self.assertEqual(
            result.collect_result.items[0].last_error_code,
            "cover:cover_commit_failed",
        )

    def test_success_clears_only_an_existing_cover_error(self):
        previous_cover_failure = item(
            "retry", last_error_code="cover:cover_timeout"
        )
        collection_failure = item(
            "collection", last_error_code="platform_changed"
        )
        result = self.run_workflow(
            FakeLocalizer(), [previous_cover_failure, collection_failure]
        )
        self.assertEqual(result.collect_result.items[0].last_error_code, "")
        self.assertEqual(
            result.collect_result.items[1].last_error_code, "platform_changed"
        )

    def test_only_strict_managed_download_receipt_is_accepted(self):
        invalid = (
            CoverReceipt(
                "downloaded",
                relative_path="/tmp/a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu/../a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu//a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu/./a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="douyin/a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                reason_code="cover_io_failed",
                relative_path="xiaohongshu/a.jpg",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu/a.jpg",
                size=0,
                sha256=GOOD_SHA,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu/a.jpg",
                size=1,
                sha256="A" * 64,
                mime_type="image/jpeg",
            ),
            CoverReceipt(
                "downloaded",
                relative_path="xiaohongshu/a.gif",
                size=1,
                sha256=GOOD_SHA,
                mime_type="image/gif",
            ),
        )
        for index, receipt in enumerate(invalid):
            with self.subTest(index=index):
                result = self.run_workflow(
                    FakeLocalizer(lambda *_args, receipt=receipt: receipt),
                    [item(str(index))],
                )
                self.assertEqual(
                    result.collect_result.items[0].cover_status, "failed"
                )
                self.assertEqual(
                    result.outcomes[0].reason_code, "cover_commit_failed"
                )

        good = self.run_workflow(FakeLocalizer(), [item("good")])
        self.assertEqual(good.collect_result.items[0].cover_status, "downloaded")
        self.assertEqual(
            good.collect_result.items[0].cover_local_path,
            "xiaohongshu/good.jpg",
        )

    def test_reconcile_recovered_previous_restores_visible_old_cover(self):
        localizer = FakeLocalizer(
            lambda _platform, work_id, url, _cancel: (
                downloaded(work_id, status="recovered_previous")
                if url == ""
                else self.fail("recovery must use empty URL")
            )
        )
        persisted = item(
            "recover",
            cover_status="downloaded",
            cover_local_path="xiaohongshu/stale.jpg",
        )
        result = CoverWorkflowService(localizer).reconcile_persisted([persisted])
        self.assertEqual(result.items[0].cover_status, "downloaded")
        self.assertEqual(
            result.items[0].cover_local_path, "xiaohongshu/recover.jpg"
        )
        self.assertEqual(result.outcomes[0].status, "recovered_previous")
        self.assertEqual(localizer.calls[0]["url"], "")

    def test_unresolved_recovery_overrides_persisted_downloaded_display(self):
        localizer = FakeLocalizer(
            lambda *_args: CoverReceipt.failed("cover_commit_uncertain")
        )
        persisted = item(
            "blocked",
            cover_status="downloaded",
            cover_local_path="xiaohongshu/old.jpg",
        )
        result = CoverWorkflowService(localizer).reconcile_persisted([persisted])
        self.assertEqual(result.items[0].cover_status, "failed")
        self.assertEqual(result.items[0].cover_local_path, "")
        self.assertEqual(
            result.items[0].last_error_code, "cover:cover_commit_uncertain"
        )
        self.assertEqual(
            result.outcomes[0].reason_code, "cover_commit_uncertain"
        )

    def test_no_pending_recovery_preserves_persisted_record(self):
        localizer = FakeLocalizer(
            lambda *_args: CoverReceipt.failed("cover_invalid_url")
        )
        persisted = item(
            "clean",
            cover_status="downloaded",
            cover_local_path="xiaohongshu/clean.jpg",
        )
        result = CoverWorkflowService(localizer).reconcile_persisted([persisted])
        self.assertEqual(result.items, (persisted,))
        self.assertEqual(result.outcomes[0].status, "downloaded")
        self.assertEqual(localizer.calls[0]["url"], "")

    def test_same_target_unresolved_recovery_blocks_new_download(self):
        localizer = FakeLocalizer(
            lambda *_args: CoverReceipt.failed("cover_commit_uncertain")
        )
        original = item("blocked")
        result = self.run_workflow(localizer, [original])
        self.assertEqual(len(localizer.calls), 1)
        self.assertEqual(localizer.calls[0]["url"], original.cover_url)
        self.assertEqual(result.collect_result.items[0].cover_status, "failed")
        self.assertEqual(
            result.outcomes[0].reason_code, "cover_commit_uncertain"
        )

    def test_startup_receipts_are_exposed_without_calling_recover_again(self):
        startup = (CoverReceipt.failed("cover_commit_uncertain"),)
        localizer = FakeLocalizer(startup=startup)
        service = CoverWorkflowService(localizer)
        self.assertEqual(service.startup_recovery_receipts, startup)
        self.assertFalse(hasattr(localizer, "recover_pending"))
        self.assertEqual(localizer.calls, [])

    def test_all_fake_paths_leave_system_dns_and_socket_untouched(self):
        localizer = FakeLocalizer()
        with (
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("system DNS")
            ),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("real socket"),
            ),
        ):
            workflow = CoverWorkflowService(localizer)
            workflow.run(
                CollectResult.complete((item("fake"),)),
                snapshot=CollectCoverPreferenceSnapshot(True),
            )
            workflow.reconcile_persisted(
                [
                    item(
                        "persisted",
                        cover_status="downloaded",
                        cover_local_path="xiaohongshu/persisted.jpg",
                    )
                ]
            )


if __name__ == "__main__":
    unittest.main()
