"""P3A 主页采集应用服务的 fixture-first 测试。"""

from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import socket
import subprocess
import threading
import urllib.request
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.collect.adapters import DouyinAdapter, XiaohongshuAdapter  # noqa: E402
from src.core.collect import application as application_module  # noqa: E402
from src.core.collect.application import (  # noqa: E402
    CollectionApplicationService,
    CollectionCommand,
    XHS_RELEASE_DISABLED_MESSAGE,
    XhsBrowserStatus,
)
from src.core.collect.contracts import AdapterCapability, CollectResult  # noqa: E402
from src.core.collect.cover import CoverReceipt  # noqa: E402
from src.core.collect.cover_workflow import CoverWorkflowService  # noqa: E402
from src.core.collect.models import CollectedItem  # noqa: E402
from src.core.collect.xhs_browser import (  # noqa: E402
    CdpEndpoint,
    XhsBrowserError,
    XhsBrowserIdentity,
)
from src.core.collect.xhs_process import (  # noqa: E402
    XhsTaskOutcome,
    XhsTerminalReceipt,
)


DOUYIN_URL = "https://www.douyin.com/user/MS4w.fixture"
XHS_URL = "https://www.xiaohongshu.com/user/profile/public_brand"


class FakeSession:
    def __init__(self, snapshots=(), *, fail_get: bool = False, fail_read: bool = False):
        self.snapshots = list(snapshots)
        self.fail_get = fail_get
        self.fail_read = fail_read
        self.get_urls: list[str] = []
        self.close_count = 0
        self.cookie_reads = 0

    def get(self, url: str) -> None:
        self.get_urls.append(url)
        if self.fail_get:
            raise RuntimeError("sensitive profile/path/token must stay hidden")

    def run_js(self, script: str):
        if self.fail_read:
            raise RuntimeError("DOM token query must stay hidden")
        if "scrollBy" in script:
            return None
        return self.snapshots.pop(0)

    def close(self) -> None:
        self.close_count += 1

    def cookies(self, *_args, **_kwargs):
        self.cookie_reads += 1
        raise AssertionError("application service must not read cookies")


def xhs_item(index: int = 1, **overrides) -> CollectedItem:
    values = {
        "aweme_id": "",
        "work_id": f"note_{index}",
        "platform": "xiaohongshu",
        "work_url": f"https://www.xiaohongshu.com/explore/note_{index}",
        "author_id": "brand_author",
        "author_name": "公开品牌",
        "title": f"公开作品 {index}",
        "cover_status": "not_requested",
    }
    values.update(overrides)
    return CollectedItem(**values)


def xhs_outcome(
    completion: str = "complete",
    reason_code: str = "",
    items: tuple[CollectedItem, ...] = (),
    *,
    browser_disposition: str = "preserved",
) -> XhsTaskOutcome:
    terminal = {
        "complete": "completed",
        "partial": "completed",
        "cancelled": "cancelled_task_only",
    }.get(completion, "failed")
    return XhsTaskOutcome(
        receipt=XhsTerminalReceipt(
            request_id="0123456789abcdef0123456789abcdef",
            terminal=terminal,
            task_worker_dead=True,
            result_gate_closed=True,
            cdp_session_detached=True,
            collection_lease_released=True,
            browser_disposition=browser_disposition,
        ),
        completion=completion,
        reason_code=reason_code,
        items=items,
    )


class FakeXhsBrowserManager:
    def __init__(
        self,
        *,
        error_code: str = "",
        close_error_code: str = "",
    ) -> None:
        self.registry = SimpleNamespace(
            app_data=Path("/fixture/lulu"),
            load_xhs_browser_receipt=lambda: None,
        )
        self.identity = XhsBrowserIdentity(
            state="launched_ready",
            chrome_pid=41001,
            chrome_birth="fixture-birth",
            profile_identity_digest="a" * 64,
            endpoint=CdpEndpoint(
                "127.0.0.1",
                49152,
                "/devtools/browser/fixture",
            ),
        )
        self.error_code = error_code
        self.close_error_code = close_error_code
        self.alive = False
        self.ensure_calls = 0
        self.close_calls = 0
        self.release_calls = 0

    def ensure_ready(self):
        self.ensure_calls += 1
        if self.error_code:
            raise XhsBrowserError(self.error_code, "sensitive details hidden")
        self.alive = True
        return self.identity

    def process_probe(self, pid, birth):
        if self.alive and (pid, birth) == (
            self.identity.chrome_pid,
            self.identity.chrome_birth,
        ):
            return SimpleNamespace(pid=pid, birth=birth)
        return None

    def close_explicit(self):
        self.close_calls += 1
        if self.close_error_code:
            raise XhsBrowserError(
                self.close_error_code,
                "sensitive details hidden",
            )
        self.alive = False
        self.release_controller()

    def release_controller(self):
        self.release_calls += 1


class DelayedXhsBrowserManager(FakeXhsBrowserManager):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release_ready = threading.Event()

    def ensure_ready(self):
        self.ensure_calls += 1
        self.alive = True
        self.entered.set()
        if not self.release_ready.wait(1.0):
            raise AssertionError("fixture late readiness was not released")
        return self.identity


class FakeXhsProcess:
    def __init__(
        self,
        outcome,
        *,
        timeout_count: int = 0,
        outcome_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.outcome = outcome
        self.timeout_count = timeout_count
        self.outcome_error = outcome_error
        self.close_error = close_error
        self.start_calls = 0
        self.cancel_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def next_outcome(self, _timeout, *, on_progress=None):
        if on_progress is not None:
            on_progress("正在读取公开主页…", 1)
        if self.outcome_error is not None:
            raise self.outcome_error
        if self.timeout_count:
            self.timeout_count -= 1
            raise TimeoutError
        return self.outcome

    def cancel(self) -> None:
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeCoverWorkflow:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls = []

    def run(self, result, *, snapshot, is_cancelled=None):
        self.calls.append((result, snapshot, is_cancelled))
        if self.failure is not None:
            raise self.failure
        rows = tuple(
            replace(
                item,
                cover_status="downloaded",
                cover_local_path=f"{item.platform}/{item.work_id}.jpg",
            )
            for item in result.items
        )
        outcomes = tuple(
            SimpleNamespace(
                stable_key=item.stable_key,
                status="downloaded",
                reason_code="",
                relative_path=item.cover_local_path,
            )
            for item in rows
        )
        return SimpleNamespace(
            collect_result=CollectResult.complete(rows),
            outcomes=outcomes,
        )


class FakeCoverLocalizer:
    startup_recovery_receipts = ()

    def __init__(self) -> None:
        self.calls = []

    def localize(
        self,
        *,
        platform,
        work_id,
        url,
        is_cancelled=None,
    ):
        self.calls.append((platform, work_id, url, is_cancelled))
        return CoverReceipt(
            "downloaded",
            relative_path=f"{platform}/{work_id}.jpg",
            size=3,
            sha256="a" * 64,
            mime_type="image/jpeg",
        )


class ApplicationServiceTests(unittest.TestCase):
    def _service(
        self,
        sessions=(),
        *,
        resolver=None,
        sleeper=lambda _seconds: None,
        cover_workflow=None,
        xhs_manager=None,
        xhs_processes=(),
    ):
        queue = list(sessions)
        process_queue = list(xhs_processes)
        calls: list[tuple[str, str, str | None]] = []
        process_calls: list[tuple[Path, XhsBrowserIdentity, str, int]] = []

        def session_factory(platform, profile_id, browser_path, _cancelled):
            calls.append((platform, profile_id, browser_path))
            if not queue:
                raise AssertionError("unexpected browser session")
            return queue.pop(0)

        def process_factory(app_data, identity, homepage_url, *, limit):
            process_calls.append((Path(app_data), identity, homepage_url, limit))
            if not process_queue:
                raise AssertionError("unexpected XHS task process")
            return process_queue.pop(0)

        manager = xhs_manager or FakeXhsBrowserManager()
        service = CollectionApplicationService(
            adapter_resolver=resolver or (lambda platform: XiaohongshuAdapter()),
            session_factory=session_factory,
            sleeper=sleeper,
            cover_workflow=cover_workflow,
            xhs_browser_manager=manager,
            xhs_process_factory=process_factory,
            xhs_poll_seconds=0.01,
        )
        return service, calls, process_calls, manager

    def test_default_manager_waits_for_one_late_ready_launch_at_p0_maximum(self):
        registry = object()
        manager = object()
        constructor = Mock(return_value=manager)
        with (
            patch.object(
                application_module,
                "ProfileRegistry",
                return_value=registry,
            ),
            patch.object(
                application_module,
                "_xhs_browser_manager_type",
                return_value=constructor,
            ) as manager_type,
            patch(
                "src.core.models_manager.app_data_dir",
                return_value=Path("/fixture/lulu"),
            ),
        ):
            created = application_module._default_xhs_browser_manager()

        self.assertIs(created, manager)
        manager_type.assert_called_once_with()
        args, kwargs = constructor.call_args
        self.assertEqual(args, (registry,))
        self.assertEqual(kwargs["discovery_timeout"], 30.0)
        self.assertTrue(callable(kwargs["normal_close"]))

    def test_same_open_call_can_transition_from_opening_to_late_ready(self):
        manager = DelayedXhsBrowserManager()
        service, _calls, process_calls, _manager = self._service(
            xhs_manager=manager
        )
        result: list[str] = []
        worker = threading.Thread(
            target=lambda: result.append(
                service.run_profile_login("xiaohongshu")
            )
        )

        worker.start()
        self.assertTrue(manager.entered.wait(0.5))
        opening = service.xhs_browser_status()
        self.assertEqual(
            (opening.code, opening.running, opening.busy),
            ("opening", False, True),
        )
        manager.release_ready.set()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["ready"])
        self.assertEqual(manager.ensure_calls, 1)
        self.assertEqual(process_calls, [])
        self.assertEqual(service.xhs_browser_status().code, "ready_idle")
        self.assertTrue(service.xhs_browser_status().running)

    def test_refresh_never_promotes_receipt_and_live_pid_without_p0_identity(self):
        manager = FakeXhsBrowserManager()
        manager.alive = True
        manager.registry.load_xhs_browser_receipt = lambda: SimpleNamespace(
            chrome_pid=manager.identity.chrome_pid,
            chrome_birth=manager.identity.chrome_birth,
        )
        service, _calls, _process_calls, _manager = self._service(
            xhs_manager=manager
        )

        status = service.xhs_browser_status(refresh=True)

        self.assertEqual(
            (status.code, status.running, status.busy),
            ("cdp_discovery_invalid", False, False),
        )
        self.assertEqual(manager.ensure_calls, 0)

    def test_untrusted_or_lost_open_stays_failed_without_automatic_retry(self):
        for code in ("cdp_discovery_invalid", "browser_lost"):
            with self.subTest(code=code):
                manager = FakeXhsBrowserManager(error_code=code)
                service, _calls, process_calls, _manager = self._service(
                    xhs_manager=manager
                )

                result = service.run_profile_login("xiaohongshu")
                status = service.xhs_browser_status()
                closed = service.close_xhs_browser()

                self.assertEqual(result, code)
                self.assertEqual((status.code, status.running), (code, False))
                self.assertEqual(manager.ensure_calls, 1)
                self.assertEqual(manager.close_calls, 0)
                self.assertEqual(closed.code, "absent")
                self.assertEqual(process_calls, [])

    def test_inspect_is_pure_and_reports_platform_capabilities(self):
        touched = []
        service = CollectionApplicationService(
            adapter_resolver=lambda _platform: touched.append("resolver"),
            session_factory=lambda *_args: touched.append("browser"),
        )
        douyin = service.inspect(DOUYIN_URL)
        xhs = service.inspect(XHS_URL)
        bili = service.inspect("https://space.bilibili.com/123")
        unknown = service.inspect("没有链接")

        self.assertEqual((douyin.identity.platform, douyin.allowed_modes),
                         ("douyin", ("anonymous_latest", "profile_all")))
        self.assertEqual((xhs.identity.platform, xhs.allowed_modes),
                         ("xiaohongshu", ("profile_public",)))
        self.assertFalse(bili.capability.available)
        self.assertEqual(unknown.identity.reason_code, "invalid_input")
        self.assertEqual(touched, [])

    def test_release_disabled_xhs_fails_before_manager_process_or_adapter(self):
        touched = []
        capability = lambda platform: (
            AdapterCapability("xiaohongshu", False, "release_disabled")
            if platform == "xiaohongshu"
            else application_module.release_adapter_capability(platform)
        )
        service = CollectionApplicationService(
            adapter_resolver=lambda platform: touched.append(("adapter", platform)),
            session_factory=lambda *args: touched.append(("session", args)),
            xhs_browser_manager=None,
            xhs_process_factory=lambda *args, **kwargs: touched.append(
                ("process", args, kwargs)
            ),
            capability_resolver=capability,
        )
        with patch.object(
            application_module,
            "_default_xhs_browser_manager",
            side_effect=AssertionError("release gate must precede manager construction"),
        ):
            entry = service.inspect(XHS_URL)
            result = service.collect(
                CollectionCommand(XHS_URL, "profile_public", profile_id="default")
            )
            statuses = (
                service.xhs_browser_status(refresh=True),
                service.open_xhs_browser(),
                service.close_xhs_browser(),
                service.release_xhs_browser_control(),
            )
            login = service.run_profile_login("xiaohongshu")

        self.assertEqual(
            (entry.capability.available, entry.capability.reason, entry.allowed_modes),
            (False, "release_disabled", ()),
        )
        self.assertEqual(
            (result.completion, result.reason_code, result.next_action),
            ("failed", "unsupported_link", XHS_RELEASE_DISABLED_MESSAGE),
        )
        self.assertTrue(all(status.code == "release_disabled" for status in statuses))
        self.assertEqual(login, "release_disabled")
        self.assertFalse(service.xhs_task_active())
        self.assertEqual(touched, [])

    def test_unknown_ambiguous_and_bilibili_fail_before_resolver_or_browser(self):
        touched = []
        service = CollectionApplicationService(
            adapter_resolver=lambda platform: touched.append(("resolver", platform)),
            session_factory=lambda *args: touched.append(("browser", args)),
        )
        cases = [
            ("没有链接", "invalid_input"),
            (f"{DOUYIN_URL} {XHS_URL}", "ambiguous_link"),
            ("https://space.bilibili.com/123", "unsupported_link"),
        ]
        for raw, reason in cases:
            with self.subTest(raw=raw):
                result = service.collect(CollectionCommand(raw, "profile_public"))
                self.assertEqual((result.completion, result.reason_code), ("failed", reason))
        self.assertEqual(touched, [])

    def test_douyin_two_modes_preserve_adapter_parameters(self):
        fetch_calls = []
        full_calls = []

        def resolver(_platform):
            return DouyinAdapter(
                fetch_latest_fn=lambda url, count: fetch_calls.append((url, count)) or [
                    CollectedItem("1")
                ],
                collect_all_fn=lambda url, progress, cancelled, path, mode: full_calls.append(
                    (url, path, mode, cancelled())
                ) or ([CollectedItem("2")], ""),
            )

        service = CollectionApplicationService(
            adapter_resolver=resolver,
            session_factory=lambda *_args: self.fail("browser shell must stay unused"),
        )
        quick = service.collect(CollectionCommand(DOUYIN_URL, "anonymous_latest", limit=7))
        full = service.collect(CollectionCommand(
            DOUYIN_URL,
            "profile_all",
            browser_path="/fake/chrome",
            browser_mode="low_risk",
        ))

        self.assertEqual(quick.completion, "complete")
        self.assertEqual(full.completion, "complete")
        self.assertEqual(fetch_calls, [(DOUYIN_URL, 7)])
        self.assertEqual(full_calls, [(DOUYIN_URL, "/fake/chrome", "low_risk", False)])

    def test_xhs_is_fixed_to_public_default_and_uses_persistent_browser(self):
        process = FakeXhsProcess(
            xhs_outcome("complete", items=(xhs_item(),))
        )
        service, session_calls, process_calls, manager = self._service(
            xhs_processes=(process,)
        )

        result = service.collect(CollectionCommand(
            XHS_URL,
            "profile_public",
            include_cover=True,
            browser_path="/must-be-ignored/chrome",
        ))

        self.assertEqual(result.completion, "complete")
        self.assertEqual(result.items[0].stable_key, "xiaohongshu:note_1")
        self.assertEqual(session_calls, [])
        self.assertEqual(manager.ensure_calls, 1)
        self.assertEqual(manager.close_calls, 0)
        self.assertTrue(manager.alive)
        self.assertEqual(process.start_calls, 1)
        self.assertEqual(
            (process_calls[0][0], process_calls[0][2], process_calls[0][3]),
            (Path("/fixture/lulu"), XHS_URL, 20),
        )
        self.assertIs(process_calls[0][1], manager.identity)
        self.assertEqual(
            service.xhs_browser_status(),
            XhsBrowserStatus(
                code="ready_idle",
                running=True,
                busy=False,
                message="专用浏览器已打开；登录状态由平台页面确认。",
            ),
        )

    def test_xhs_rejects_work_link_bad_mode_and_nondefault_without_browser(self):
        service, calls, process_calls, manager = self._service([])
        cases = [
            CollectionCommand("https://www.xiaohongshu.com/explore/note_1", "profile_public"),
            CollectionCommand(XHS_URL, "anonymous_latest"),
            CollectionCommand(XHS_URL, "profile_public", profile_id="other"),
        ]
        for command in cases:
            with self.subTest(command=command):
                result = service.collect(command)
                self.assertEqual(result.completion, "failed")
        self.assertEqual(calls, [])
        self.assertEqual(process_calls, [])
        self.assertEqual(manager.ensure_calls, 0)

    def test_xhs_complete_empty_and_cover_url_state(self):
        empty = FakeXhsProcess(xhs_outcome("complete"))
        cover = FakeXhsProcess(
            xhs_outcome(
                "complete",
                items=(
                    xhs_item(
                        2,
                        cover_url="https://sns-webpic.example/cover.jpg",
                        cover_status="url_only",
                    ),
                ),
            )
        )
        service, _calls, _process_calls, manager = self._service(
            xhs_processes=(empty, cover)
        )

        empty_result = service.collect(CollectionCommand(XHS_URL, "profile_public"))
        cover_result = service.collect(CollectionCommand(XHS_URL, "profile_public", include_cover=True))

        self.assertTrue(empty_result.confirmed_empty)
        self.assertEqual(cover_result.items[0].cover_status, "url_only")
        self.assertEqual(cover_result.items[0].cover_url, "https://sns-webpic.example/cover.jpg")
        self.assertEqual(manager.close_calls, 0)

    def test_xhs_partial_risk_keeps_only_safe_items_and_browser(self):
        for reason in [
            "login_expired",
            "captcha",
            "rate_limited",
            "platform_changed",
        ]:
            with self.subTest(reason=reason):
                process = FakeXhsProcess(
                    xhs_outcome(
                        "partial",
                        reason,
                        items=(xhs_item(),),
                    )
                )
                service, _calls, _process_calls, manager = self._service(
                    xhs_processes=(process,)
                )
                result = service.collect(CollectionCommand(XHS_URL, "profile_public"))
                self.assertEqual((result.completion, result.reason_code), ("partial", reason))
                self.assertEqual([item.work_id for item in result.items], ["note_1"])
                self.assertEqual(manager.close_calls, 0)
                self.assertTrue(manager.alive)

    def test_xhs_login_required_network_error_and_cancel_are_sanitized_and_retained(self):
        login = FakeXhsProcess(xhs_outcome("failed", "login_required"))
        network = FakeXhsProcess(xhs_outcome("failed", "network_error"))
        cancelled_process = FakeXhsProcess(xhs_outcome("cancelled", "cancelled"))
        service, _calls, _process_calls, manager = self._service(
            xhs_processes=(login, network, cancelled_process)
        )
        login_result = service.collect(CollectionCommand(XHS_URL, "profile_public"))
        network_result = service.collect(CollectionCommand(XHS_URL, "profile_public"))
        cancelled = service.collect(
            CollectionCommand(XHS_URL, "profile_public"),
            is_cancelled=lambda: True,
        )

        self.assertEqual(login_result.reason_code, "login_required")
        self.assertEqual(network_result.reason_code, "network_error")
        self.assertEqual(cancelled.completion, "cancelled")
        self.assertEqual(manager.close_calls, 0)
        self.assertTrue(manager.alive)

    def test_task_cancel_requests_task_only_stop_and_retains_browser(self):
        process = FakeXhsProcess(
            xhs_outcome("cancelled", "cancelled"),
            timeout_count=1,
        )
        checks = iter((False, True, True))
        service, _calls, _process_calls, manager = self._service(
            xhs_processes=(process,)
        )

        result = service.collect(
            CollectionCommand(XHS_URL, "profile_public"),
            is_cancelled=lambda: next(checks, True),
        )

        self.assertEqual(result.completion, "cancelled")
        self.assertEqual(process.cancel_calls, 1)
        self.assertEqual(manager.close_calls, 0)
        self.assertTrue(manager.alive)
        self.assertEqual(service.xhs_browser_status().code, "ready_idle")

    def test_task_cleanup_failure_stays_busy_and_cannot_close_browser(self):
        process = FakeXhsProcess(
            xhs_outcome("failed", "network_error"),
            outcome_error=RuntimeError("fixture task failure"),
            close_error=RuntimeError("fixture cleanup unproven"),
        )
        service, _calls, _process_calls, manager = self._service(
            xhs_processes=(process,)
        )

        result = service.collect(CollectionCommand(XHS_URL, "profile_public"))
        status = service.xhs_browser_status()

        self.assertEqual((result.completion, result.reason_code), (
            "failed",
            "network_error",
        ))
        self.assertEqual(
            (status.code, status.running, status.busy),
            ("task_cleanup_unproven", True, True),
        )
        self.assertTrue(service.xhs_task_active())
        self.assertEqual(
            service.close_xhs_browser().code,
            "collection_busy",
        )
        self.assertEqual(manager.close_calls, 0)

    def test_login_entry_opens_persistent_browser_without_cookie_or_task(self):
        service, calls, process_calls, manager = self._service([])
        progress = []
        result = service.run_profile_login(
            "xiaohongshu",
            browser_path="/must-be-ignored/chrome",
            progress=lambda text, count: progress.append((text, count)),
        )

        self.assertEqual(result, "ready")
        self.assertEqual(calls, [])
        self.assertEqual(process_calls, [])
        self.assertEqual(manager.ensure_calls, 1)
        self.assertEqual(manager.close_calls, 0)
        self.assertTrue(manager.alive)
        self.assertTrue(progress)

    def test_login_failure_returns_stable_code_without_sensitive_details(self):
        manager = FakeXhsBrowserManager(error_code="controller_busy")
        service, _calls, _process_calls, _manager = self._service(
            xhs_manager=manager
        )
        self.assertEqual(service.run_profile_login("xiaohongshu"), "controller_busy")
        self.assertEqual(manager.close_calls, 0)

    def test_close_active_session_is_bounded_and_idempotent(self):
        session = FakeSession()
        service, _calls, _process_calls, _manager = self._service([session])
        opened = service._open_session("xiaohongshu", "default", None, lambda: False)
        self.assertIsNotNone(opened)
        self.assertTrue(service.close_active_session())
        self.assertFalse(service.close_active_session())
        self.assertEqual(session.close_count, 1)

    def test_explicit_close_is_idle_only_and_reports_failure_honestly(self):
        manager = FakeXhsBrowserManager()
        service, _calls, _process_calls, _manager = self._service(
            xhs_manager=manager
        )
        self.assertTrue(service.open_xhs_browser().running)
        service._xhs_client = object()
        busy = service.close_xhs_browser()
        self.assertEqual((busy.code, busy.running, busy.busy), (
            "collection_busy",
            True,
            True,
        ))
        self.assertEqual(manager.close_calls, 0)

        service._xhs_client = None
        closed = service.close_xhs_browser()
        self.assertEqual((closed.code, closed.running), ("absent", False))
        self.assertEqual(manager.close_calls, 1)
        self.assertFalse(manager.alive)

        failed_manager = FakeXhsBrowserManager(
            close_error_code="close_failed_browser_alive"
        )
        failed_service, *_ = self._service(xhs_manager=failed_manager)
        failed_service.open_xhs_browser()
        failed = failed_service.close_xhs_browser()
        self.assertEqual(
            (failed.code, failed.running),
            ("close_failed_browser_alive", True),
        )
        self.assertTrue(failed_manager.alive)

    def test_exit_keep_releases_controller_without_closing_browser(self):
        manager = FakeXhsBrowserManager()
        service, _calls, _process_calls, _manager = self._service(
            xhs_manager=manager
        )
        service.open_xhs_browser()
        status = service.release_xhs_browser_control()

        self.assertEqual((status.code, status.running), ("retained", True))
        self.assertEqual(manager.release_calls, 1)
        self.assertEqual(manager.close_calls, 0)
        self.assertTrue(manager.alive)

    def test_p2_fixture_flow_has_hard_external_io_guards(self):
        process = FakeXhsProcess(
            xhs_outcome("complete", items=(xhs_item(),))
        )
        service, _calls, _process_calls, manager = self._service(
            xhs_processes=(process,)
        )
        with (
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")),
            patch.object(socket, "create_connection", side_effect=AssertionError("socket")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("URL")),
            patch.object(webbrowser, "open", side_effect=AssertionError("browser")),
        ):
            self.assertEqual(
                service.run_profile_login("xiaohongshu"),
                "ready",
            )
            result = service.collect(
                CollectionCommand(XHS_URL, "profile_public")
            )
            closed = service.close_xhs_browser()

        self.assertEqual(result.completion, "complete")
        self.assertEqual(closed.code, "absent")
        self.assertEqual(manager.close_calls, 1)

    def test_cover_stage_receives_only_trusted_items_and_preserves_order(self):
        workflow = FakeCoverWorkflow()
        valid = CollectedItem(
            "cover-ok",
            cover_url="https://images.example.test/cover.jpg",
            cover_status="url_only",
        )
        insecure = CollectedItem(
            "cover-http",
            cover_url="http://images.example.test/cover.jpg",
            cover_status="url_only",
        )
        missing = CollectedItem("cover-none", cover_url="")
        resolver = lambda _platform: DouyinAdapter(
            fetch_latest_fn=lambda _url, _count: [valid, insecure, missing]
        )
        service = CollectionApplicationService(
            adapter_resolver=resolver,
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=workflow,
        )

        execution = service.collect_with_cover(
            CollectionCommand(
                DOUYIN_URL,
                "anonymous_latest",
                include_cover=True,
            )
        )

        self.assertEqual(execution.collect_result.completion, "complete")
        self.assertEqual(
            [item.work_id for item in execution.collect_result.items],
            ["cover-ok", "cover-http", "cover-none"],
        )
        self.assertEqual(
            [item.work_id for item in workflow.calls[0][0].items],
            ["cover-ok"],
        )
        self.assertEqual(
            [item.cover_status for item in execution.collect_result.items],
            ["downloaded", "url_only", "not_requested"],
        )
        self.assertEqual(
            (
                execution.cover_summary.eligible_count,
                execution.cover_summary.downloaded_count,
                execution.cover_summary.failed_count,
            ),
            (1, 1, 0),
        )

    def test_cover_snapshot_false_or_missing_workflow_never_runs_download(self):
        workflow = FakeCoverWorkflow()
        adapter = SimpleNamespace(
            collect=lambda *_args, **_kwargs: CollectResult.complete(
                [
                    CollectedItem(
                        "no-cover",
                        cover_url="https://images.example.test/cover.jpg",
                        cover_status="url_only",
                    )
                ]
            )
        )
        service = CollectionApplicationService(
            adapter_resolver=lambda _platform: adapter,
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=workflow,
        )
        disabled = service.collect_with_cover(
            CollectionCommand(DOUYIN_URL, "anonymous_latest", include_cover=False)
        )
        self.assertEqual(workflow.calls, [])
        self.assertFalse(disabled.cover_summary.enabled)

        closed_entry = CollectionApplicationService(
            adapter_resolver=lambda _platform: adapter,
            session_factory=lambda *_args: self.fail("browser must stay unused"),
        ).collect_with_cover(
            CollectionCommand(DOUYIN_URL, "anonymous_latest", include_cover=True)
        )
        self.assertEqual(closed_entry.cover_summary.code, "entry_disabled")
        self.assertEqual(
            closed_entry.collect_result.items[0].cover_status, "url_only"
        )

    def test_main_completion_is_independent_for_complete_partial_and_cancelled(self):
        for main in (
            CollectResult.complete(
                [
                    CollectedItem(
                        "complete",
                        cover_url="https://images.example.test/complete.jpg",
                        cover_status="url_only",
                    )
                ]
            ),
            CollectResult.partial(
                [
                    CollectedItem(
                        "partial",
                        cover_url="https://images.example.test/partial.jpg",
                        cover_status="url_only",
                    )
                ],
                reason_code="rate_limited",
                next_action="稍后重试",
            ),
            CollectResult.cancelled(
                [
                    CollectedItem(
                        "cancelled",
                        cover_url="https://images.example.test/cancelled.jpg",
                        cover_status="url_only",
                    )
                ]
            ),
        ):
            with self.subTest(completion=main.completion):
                workflow = FakeCoverWorkflow()
                adapter = SimpleNamespace(
                    collect=lambda *_args, value=main, **_kwargs: value
                )
                service = CollectionApplicationService(
                    adapter_resolver=lambda _platform: adapter,
                    session_factory=lambda *_args: self.fail("browser must stay unused"),
                    cover_workflow=workflow,
                )
                execution = service.collect_with_cover(
                    CollectionCommand(
                        DOUYIN_URL,
                        "anonymous_latest",
                        include_cover=True,
                    )
                )
                self.assertEqual(
                    (
                        execution.collect_result.completion,
                        execution.collect_result.reason_code,
                        execution.collect_result.next_action,
                    ),
                    (main.completion, main.reason_code, main.next_action),
                )
                self.assertEqual(
                    execution.collect_result.items[0].cover_status,
                    "downloaded",
                )

        workflow = FakeCoverWorkflow()
        failed = CollectionApplicationService(
            adapter_resolver=lambda _platform: SimpleNamespace(
                collect=lambda *_args, **_kwargs: CollectResult.failed(
                    "network_error"
                )
            ),
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=workflow,
        ).collect_with_cover(
            CollectionCommand(DOUYIN_URL, "anonymous_latest", include_cover=True)
        )
        self.assertEqual(failed.collect_result.completion, "failed")
        self.assertEqual(workflow.calls, [])

    def test_cover_exception_fails_only_cover_and_keeps_main_partial(self):
        original = CollectedItem(
            "safe",
            cover_url="https://images.example.test/safe.jpg",
            cover_status="url_only",
        )
        main = CollectResult.partial(
            [original],
            reason_code="rate_limited",
            next_action="稍后重试",
        )
        service = CollectionApplicationService(
            adapter_resolver=lambda _platform: SimpleNamespace(
                collect=lambda *_args, **_kwargs: main
            ),
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=FakeCoverWorkflow(
                failure=RuntimeError("secret URL must stay hidden")
            ),
        )
        execution = service.collect_with_cover(
            CollectionCommand(DOUYIN_URL, "anonymous_latest", include_cover=True)
        )
        self.assertEqual(
            (
                execution.collect_result.completion,
                execution.collect_result.reason_code,
                execution.collect_result.next_action,
            ),
            ("partial", "rate_limited", "稍后重试"),
        )
        self.assertEqual(execution.collect_result.items[0].cover_status, "failed")
        self.assertEqual(
            execution.collect_result.items[0].last_error_code,
            "cover:cover_commit_failed",
        )
        self.assertEqual(execution.cover_summary.failed_count, 1)

    def test_cover_stage_forwards_same_cancel_callback_without_system_io(self):
        workflow = FakeCoverWorkflow()
        adapter = SimpleNamespace(
            collect=lambda *_args, **_kwargs: CollectResult.complete(
                [
                    CollectedItem(
                        "cancel-contract",
                        cover_url="https://images.example.test/cancel.jpg",
                        cover_status="url_only",
                    )
                ]
            )
        )
        service = CollectionApplicationService(
            adapter_resolver=lambda _platform: adapter,
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=workflow,
        )
        cancelled = lambda: False
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
            service.collect_with_cover(
                CollectionCommand(
                    DOUYIN_URL,
                    "anonymous_latest",
                    include_cover=True,
                ),
                is_cancelled=cancelled,
            )
        self.assertIs(workflow.calls[0][2], cancelled)
        self.assertTrue(workflow.calls[0][1].include_cover)

    def test_real_r1_workflow_contract_integrates_with_fake_localizer(self):
        localizer = FakeCoverLocalizer()
        workflow = CoverWorkflowService(localizer)
        adapter = SimpleNamespace(
            collect=lambda *_args, **_kwargs: CollectResult.complete(
                [
                    CollectedItem(
                        "r1",
                        cover_url="https://images.example.test/r1.jpg",
                        cover_status="url_only",
                    )
                ]
            )
        )
        service = CollectionApplicationService(
            adapter_resolver=lambda _platform: adapter,
            session_factory=lambda *_args: self.fail("browser must stay unused"),
            cover_workflow=workflow,
        )
        execution = service.collect_with_cover(
            CollectionCommand(
                DOUYIN_URL,
                "anonymous_latest",
                include_cover=True,
            )
        )
        self.assertEqual(
            execution.collect_result.items[0].cover_status, "downloaded"
        )
        self.assertEqual(execution.cover_summary.downloaded_count, 1)
        self.assertEqual(len(localizer.calls), 1)


if __name__ == "__main__":
    unittest.main()
