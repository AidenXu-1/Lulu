"""P2C 共享浏览器壳的 fixture-first 物理隔离与生命周期测试。"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.adapters import XiaohongshuAdapter, resolve_adapter  # noqa: E402
from core.collect.browser_shell import (  # noqa: E402
    BrowserLaunchSpec,
    BrowserShell,
    BrowserShellError,
    debug_port_for,
)
from core.collect.contracts import (  # noqa: E402
    CollectRequest,
    LinkIdentity,
    adapter_capability,
)
from core.collect.profiles import ProfileRegistry  # noqa: E402


class _FakeOptions:
    def __init__(self) -> None:
        self.browser_path = None
        self.user_data_path = None
        self.cache_path = None
        self.tmp_path = None
        self.download_path = None
        self.local_port = None
        self.arguments: list[str] = []
        self.load_mode = None
        self.retry_times = None
        self.retry_interval = None
        self.timeouts: dict[str, float] = {}

    def set_browser_path(self, value):
        self.browser_path = str(value)
        return self

    def set_user_data_path(self, value):
        self.user_data_path = str(value)
        return self

    def set_cache_path(self, value):
        self.cache_path = str(value)
        return self

    def set_tmp_path(self, value):
        self.tmp_path = str(value)
        return self

    def set_download_path(self, value):
        self.download_path = str(value)
        return self

    def set_local_port(self, value):
        self.local_port = int(value)
        return self

    def set_argument(self, value):
        self.arguments.append(str(value))
        return self

    def set_load_mode(self, value):
        self.load_mode = str(value)
        return self

    def set_retry(self, *, times=None, interval=None):
        self.retry_times = times
        self.retry_interval = interval
        return self

    def set_timeouts(self, *, page_load=None, script=None):
        self.timeouts = {"page_load": page_load, "script": script}
        return self

    def auto_port(self, *_args, **_kwargs):
        raise AssertionError("P2C 持久 profile 禁止使用 DrissionPage auto_port()")


class _FakePage:
    def __init__(self, options: _FakeOptions, *, fail_quit: bool = False) -> None:
        self.options = options
        self.fail_quit = fail_quit
        self.quit_count = 0
        self.quit_timeouts: list[float | None] = []

    def quit(self, *, timeout=None):
        self.quit_count += 1
        self.quit_timeouts.append(timeout)
        if self.fail_quit:
            raise RuntimeError("fixture quit failure")


class BrowserShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_data = Path(self.temp.name) / "app-data"
        self.registry = ProfileRegistry(self.app_data)
        self.options: list[_FakeOptions] = []
        self.pages: list[_FakePage] = []
        self.fail_launch = False
        self.fail_next_quit = False

    def _options_factory(self):
        options = _FakeOptions()
        self.options.append(options)
        return options

    def _page_factory(self, options):
        if self.fail_launch:
            raise RuntimeError("fixture launch failure")
        page = _FakePage(options, fail_quit=self.fail_next_quit)
        self.fail_next_quit = False
        self.pages.append(page)
        return page

    def _shell(self, **kwargs):
        return BrowserShell(
            self.registry,
            options_factory=self._options_factory,
            page_factory=self._page_factory,
            port_available=kwargs.pop("port_available", lambda _port: True),
            **kwargs,
        )

    def test_cancel_during_launch_closes_page_and_releases_both_leases(self):
        cancelled = False
        pages = []
        def factory(options):
            nonlocal cancelled
            cancelled = True
            page = _FakePage(options)
            pages.append(page)
            return page
        shell = BrowserShell(self.registry, options_factory=self._options_factory,
                             page_factory=factory, port_available=lambda _port: True)
        self.assertIsNone(shell.open(BrowserLaunchSpec("douyin"),
                                     is_cancelled=lambda: cancelled))
        self.assertEqual(pages[0].quit_count, 1)
        with self._shell().open(BrowserLaunchSpec("douyin")):
            pass

    def test_platform_sessions_use_distinct_identity_paths_ports_and_locks(self):
        shell = self._shell()
        douyin = shell.open(BrowserLaunchSpec("douyin", browser_path="/fake/chrome"))
        xhs = shell.open(BrowserLaunchSpec("xiaohongshu", browser_path="/fake/chrome"))
        self.addCleanup(douyin.close)
        self.addCleanup(xhs.close)

        self.assertNotEqual(douyin.paths.user_data_dir, xhs.paths.user_data_dir)
        self.assertNotEqual(douyin.paths.cache_dir, xhs.paths.cache_dir)
        self.assertNotEqual(douyin.paths.temp_dir, xhs.paths.temp_dir)
        self.assertNotEqual(douyin.paths.download_temp_dir, xhs.paths.download_temp_dir)
        self.assertNotEqual(douyin.debug_port, xhs.debug_port)
        self.assertNotEqual(douyin.profile_lease.lock_path, xhs.profile_lease.lock_path)
        self.assertNotEqual(douyin.port_lease.lock_path, xhs.port_lease.lock_path)

        for session, expected_platform in ((douyin, "douyin"), (xhs, "xiaohongshu")):
            options = session.page.options
            self.assertIn(expected_platform, session.paths.user_data_dir.parts)
            self.assertEqual(options.browser_path, "/fake/chrome")
            self.assertEqual(options.user_data_path, str(session.paths.user_data_dir))
            self.assertEqual(options.cache_path, str(session.paths.cache_dir))
            self.assertEqual(options.tmp_path, str(session.paths.temp_dir))
            self.assertEqual(options.download_path, str(session.paths.download_temp_dir))
            self.assertEqual(options.local_port, session.debug_port)
            self.assertIn("--no-first-run", options.arguments)

        self.assertIsNone(douyin.page.options.load_mode)
        self.assertIsNone(douyin.page.options.retry_times)
        self.assertEqual(douyin.page.options.timeouts, {})
        self.assertEqual(xhs.page.options.load_mode, "eager")
        self.assertEqual(xhs.page.options.retry_times, 0)
        self.assertEqual(xhs.page.options.retry_interval, 0)
        self.assertEqual(
            xhs.page.options.timeouts,
            {"page_load": 2.0, "script": 2.0},
        )

    def test_closing_one_platform_does_not_touch_other_identity_or_page(self):
        shell = self._shell()
        douyin = shell.open(BrowserLaunchSpec("douyin"))
        xhs = shell.open(BrowserLaunchSpec("xiaohongshu"))
        self.addCleanup(xhs.close)
        (douyin.paths.user_data_dir / "Cookies").write_text("douyin", encoding="utf-8")
        xhs_cookie = xhs.paths.user_data_dir / "Cookies"
        xhs_storage = xhs.paths.user_data_dir / "Local Storage" / "leveldb" / "fixture"
        xhs_indexed_db = xhs.paths.user_data_dir / "IndexedDB" / "fixture"
        xhs_cookie.write_text("xiaohongshu", encoding="utf-8")
        xhs_storage.parent.mkdir(parents=True)
        xhs_storage.write_text("keep", encoding="utf-8")
        xhs_indexed_db.parent.mkdir(parents=True)
        xhs_indexed_db.write_text("keep-indexed-db", encoding="utf-8")

        douyin.close()

        self.assertEqual(douyin.page.quit_count, 1)
        self.assertEqual(douyin.page.quit_timeouts, [None])
        self.assertEqual(xhs.page.quit_count, 0)
        self.assertEqual(xhs_cookie.read_text(encoding="utf-8"), "xiaohongshu")
        self.assertEqual(xhs_storage.read_text(encoding="utf-8"), "keep")
        self.assertEqual(xhs_indexed_db.read_text(encoding="utf-8"), "keep-indexed-db")
        self.assertTrue(xhs.profile_lease.lock_path.exists())

        xhs.close()
        self.assertEqual(xhs.page.quit_timeouts, [0.5])
        replacement = shell.open(BrowserLaunchSpec("xiaohongshu"))
        replacement.close()

    def test_xhs_cancel_bound_closes_once_and_releases_both_leases(self):
        navigation_started = threading.Event()
        cancelled = threading.Event()
        sessions = []
        outcome = []

        class _BoundedPage(_FakePage):
            def __init__(self, options):
                super().__init__(options)
                self.get_count = 0
                self.read_count = 0

            def get(self, _url):
                self.get_count += 1
                navigation_started.set()
                time.sleep(self.options.timeouts["page_load"])

            def run_js(self, _script):
                self.read_count += 1
                raise AssertionError("取消后不得读取页面快照")

        pages = []

        def page_factory(options):
            page = _BoundedPage(options)
            pages.append(page)
            return page

        shell = BrowserShell(
            self.registry,
            options_factory=self._options_factory,
            page_factory=page_factory,
            port_available=lambda _port: True,
        )

        def open_session(profile_id):
            session = shell.open(
                BrowserLaunchSpec("xiaohongshu", profile_id=profile_id),
                is_cancelled=cancelled.is_set,
            )
            sessions.append(session)
            return session

        adapter = XiaohongshuAdapter(open_session_fn=open_session)
        collect_request = CollectRequest(
            LinkIdentity(
                platform="xiaohongshu",
                kind="homepage",
                canonical_url="https://www.xiaohongshu.com/user/profile/public_brand",
                extracted_id="public_brand",
                confidence="exact",
            ),
            mode="profile_public",
            profile_id="default",
        )
        worker = threading.Thread(
            target=lambda: outcome.append(
                adapter.collect(collect_request, is_cancelled=cancelled.is_set)
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(navigation_started.wait(0.5))
        cancelled_at = time.monotonic()
        cancelled.set()
        worker.join(2.9)

        self.assertFalse(worker.is_alive())
        self.assertLessEqual(time.monotonic() - cancelled_at, 3.0)
        self.assertEqual((outcome[0].completion, outcome[0].reason_code), ("cancelled", "cancelled"))
        self.assertEqual(pages[0].get_count, 1)
        self.assertEqual(pages[0].read_count, 0)
        self.assertEqual(pages[0].quit_count, 1)
        self.assertEqual(pages[0].quit_timeouts, [0.5])
        self.assertFalse(sessions[0].profile_lease.lock_path.exists())
        self.assertFalse(sessions[0].port_lease.lock_path.exists())

        replacement = shell.open(BrowserLaunchSpec("xiaohongshu"))
        replacement.close()

    def test_same_profile_is_busy_while_cross_platform_is_allowed(self):
        shell = self._shell()
        first = shell.open(BrowserLaunchSpec("douyin"))
        self.addCleanup(first.close)
        with self.assertRaises(BrowserShellError) as caught:
            shell.open(BrowserLaunchSpec("douyin"))
        self.assertEqual(caught.exception.code, "profile_busy")

        xhs = shell.open(BrowserLaunchSpec("xiaohongshu"))
        xhs.close()

    def test_debug_port_collision_fails_before_page_factory_and_releases_profile(self):
        shell = self._shell(port_available=lambda _port: False)
        with self.assertRaises(BrowserShellError) as caught:
            shell.open(BrowserLaunchSpec("xiaohongshu"))
        self.assertEqual(caught.exception.code, "browser_port_busy")
        self.assertEqual(self.pages, [])

        recovered = self._shell().open(BrowserLaunchSpec("xiaohongshu"))
        recovered.close()

    def test_shared_injected_port_is_locked_across_platforms(self):
        shell = self._shell(
            port_selector=lambda _platform, _profile: 45555,
            max_port_attempts=1,
        )
        douyin = shell.open(BrowserLaunchSpec("douyin"))
        self.addCleanup(douyin.close)
        with self.assertRaises(BrowserShellError) as caught:
            shell.open(BrowserLaunchSpec("xiaohongshu"))
        self.assertEqual(caught.exception.code, "browser_port_busy")
        self.assertEqual(len(self.pages), 1)

    def test_debug_port_collision_retries_with_a_strict_upper_bound(self):
        base = 45555
        attempted = []

        def available(port):
            attempted.append(port)
            return port == base + 1

        session = self._shell(
            port_selector=lambda *_: base,
            port_available=available,
            max_port_attempts=2,
        ).open(BrowserLaunchSpec("xiaohongshu"))
        self.addCleanup(session.close)

        self.assertEqual(attempted, [base, base + 1])
        self.assertEqual(session.debug_port, base + 1)
        self.assertEqual(session.page.options.local_port, base + 1)

    def test_invalid_platform_and_invalid_port_fail_closed(self):
        with self.assertRaises(BrowserShellError) as caught:
            self._shell().open(BrowserLaunchSpec("bilibili"))
        self.assertEqual(caught.exception.code, "profile_unavailable")

        with self.assertRaises(BrowserShellError) as caught:
            self._shell(port_selector=lambda *_: 80).open(BrowserLaunchSpec("douyin"))
        self.assertEqual(caught.exception.code, "invalid_debug_port")
        self.assertEqual(self.pages, [])

    def test_cancel_before_open_has_zero_filesystem_or_factory_effect(self):
        result = self._shell().open(
            BrowserLaunchSpec("xiaohongshu"),
            is_cancelled=lambda: True,
        )
        self.assertIsNone(result)
        self.assertFalse(self.app_data.exists())
        self.assertEqual(self.options, [])
        self.assertEqual(self.pages, [])

    def test_launch_failure_releases_profile_and_port_leases(self):
        self.fail_launch = True
        shell = self._shell()
        with self.assertRaises(BrowserShellError) as caught:
            shell.open(BrowserLaunchSpec("douyin"))
        self.assertEqual(caught.exception.code, "browser_launch_failed")

        self.fail_launch = False
        recovered = shell.open(BrowserLaunchSpec("douyin"))
        recovered.close()

    def test_quit_failure_still_releases_only_its_own_leases(self):
        self.fail_next_quit = True
        shell = self._shell()
        douyin = shell.open(BrowserLaunchSpec("douyin"))
        xhs = shell.open(BrowserLaunchSpec("xiaohongshu"))
        self.addCleanup(xhs.close)

        with self.assertRaises(BrowserShellError) as caught:
            douyin.close()
        self.assertEqual(caught.exception.code, "browser_close_failed")
        self.assertTrue(xhs.profile_lease.lock_path.exists())
        self.assertEqual(xhs.page.quit_count, 0)

        replacement = shell.open(BrowserLaunchSpec("douyin"))
        replacement.close()

    def test_legacy_douyin_alias_is_never_reused_by_xiaohongshu(self):
        legacy = self.app_data / "douyin_profile"
        legacy.mkdir(parents=True)
        shell = self._shell()
        douyin = shell.open(BrowserLaunchSpec("douyin"))
        xhs = shell.open(BrowserLaunchSpec("xiaohongshu"))
        self.addCleanup(douyin.close)
        self.addCleanup(xhs.close)

        self.assertEqual(douyin.paths.user_data_dir, legacy)
        self.assertNotEqual(xhs.paths.user_data_dir, legacy)
        self.assertIn("xiaohongshu", xhs.paths.user_data_dir.parts)

    def test_symlink_runtime_root_is_rejected_without_touching_target(self):
        profile = self.registry.resolve("xiaohongshu", create=True).path
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("safe", encoding="utf-8")
        try:
            (profile / ".lulu_runtime").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"当前文件系统不支持符号链接: {exc}")

        with self.assertRaises(BrowserShellError) as caught:
            self._shell().open(BrowserLaunchSpec("xiaohongshu"))
        self.assertEqual(caught.exception.code, "symlink_rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")
        self.assertEqual(self.pages, [])

    def test_symlink_cache_tmp_and_download_dirs_each_fail_closed(self):
        for child in ("cache", "tmp", "downloads"):
            with self.subTest(child=child), tempfile.TemporaryDirectory() as temp:
                app_data = Path(temp) / "app-data"
                registry = ProfileRegistry(app_data)
                profile = registry.resolve("xiaohongshu", create=True).path
                runtime = profile / ".lulu_runtime"
                runtime.mkdir()
                outside = Path(temp) / f"outside-{child}"
                outside.mkdir()
                sentinel = outside / "keep"
                sentinel.write_text("safe", encoding="utf-8")
                try:
                    (runtime / child).symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"当前文件系统不支持符号链接: {exc}")

                shell = BrowserShell(
                    registry,
                    options_factory=self._options_factory,
                    page_factory=self._page_factory,
                    port_available=lambda _port: True,
                )
                with self.assertRaises(BrowserShellError) as caught:
                    shell.open(BrowserLaunchSpec("xiaohongshu"))
                self.assertEqual(caught.exception.code, "symlink_rejected")
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_debug_port_mapping_is_stable_and_cross_platform_distinct(self):
        self.assertEqual(debug_port_for("douyin", "default"), debug_port_for("douyin", "default"))
        self.assertNotEqual(
            debug_port_for("douyin", "default"),
            debug_port_for("xiaohongshu", "default"),
        )
        self.assertEqual(debug_port_for("douyin", "default"), 43649)

    def test_douyin_open_page_keeps_old_signature_and_delegates_platform(self):
        from core.collect import douyin_browser

        captured = []
        fake_page = object()

        class _FakeShell:
            def __init__(self, registry):
                captured.append(registry)

            def open(self, spec, *, is_cancelled=None):
                captured.append((spec, is_cancelled))
                return fake_page

        with patch.object(douyin_browser, "ProfileRegistry", return_value=self.registry), patch.object(
            douyin_browser, "BrowserShell", _FakeShell
        ):
            result = douyin_browser._open_page("/fake/chrome")

        self.assertIs(result, fake_page)
        spec, cancelled = captured[1]
        self.assertEqual(spec.platform_id, "douyin")
        self.assertEqual(spec.profile_id, "default")
        self.assertEqual(spec.browser_path, "/fake/chrome")
        self.assertIsNone(cancelled)

    def test_xiaohongshu_adapter_is_available_without_opening_browser(self):
        self.assertTrue(adapter_capability("xiaohongshu").available)
        self.assertIsInstance(resolve_adapter("xiaohongshu"), XiaohongshuAdapter)
        self.assertEqual(self.pages, [])


if __name__ == "__main__":
    unittest.main()
