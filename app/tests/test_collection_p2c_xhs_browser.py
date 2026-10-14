"""小红书专用常驻 Chrome P0 的纯离线生命周期与安全门。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import patch
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.profiles import ProfileRegistry  # noqa: E402
import core.collect.xhs_browser as xhs_browser  # noqa: E402
from core.collect.xhs_browser import (  # noqa: E402
    ChromeInstallation,
    ChromeProcess,
    LaunchServicesSubmission,
    XhsBrowserError,
    XhsBrowserManager,
)


class XhsBrowserManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app_data = self.root / "app-data"
        self.daily_chrome = self.root / "daily-chrome"
        self.daily_chrome.mkdir()
        self.registry = ProfileRegistry(self.app_data)
        self.chrome_app = self.root / "Google Chrome.app"
        self.chrome = self.root / "Google Chrome"
        self.chrome.write_text("fixture", encoding="utf-8")
        self.installation = ChromeInstallation(
            self.chrome,
            "136.0.7103.48",
            app_bundle=self.chrome_app,
        )
        self.processes: dict[tuple[int, str], ChromeProcess] = {}
        self.launches: list[tuple[str, ...]] = []
        self.clock_ns = 2_000_000_000
        self.next_pid = 7100
        self.port = 52341
        self.browser_path = "/devtools/browser/fixture-browser"
        self.discovery_writer = self._write_valid_discovery
        self.listener_result = {"127.0.0.1"}
        self.cdp_browser = "Chrome/136.0.7103.48"
        self.cdp_ws_path = self.browser_path
        self.cdp_ws_port = self.port
        self.close_requested = False

    def test_release_disabled_precedes_controller_profile_and_launch(self):
        manager = self._manager()
        with (
            patch.object(
                xhs_browser,
                "homepage_collection_available",
                return_value=False,
            ),
            patch.object(
                manager,
                "_acquire_controller",
                side_effect=AssertionError("不得取得 controller"),
            ),
            self.assertRaises(XhsBrowserError) as caught,
        ):
            manager.ensure_ready()

        self.assertEqual(caught.exception.code, "release_disabled")
        self.assertIsNone(manager._controller)
        self.assertFalse(
            self.app_data.joinpath(
                "collection_profiles/xiaohongshu/default"
            ).exists()
        )
        self.assertEqual(self.launches, [])

    def _profile_from_arguments(self, arguments: tuple[str, ...]) -> Path:
        value = next(
            item for item in arguments if item.startswith("--user-data-dir=")
        )
        return Path(value.split("=", 1)[1])

    def _write_valid_discovery(self, profile: Path) -> None:
        target = profile / "DevToolsActivePort"
        target.write_text(
            f"{self.port}\n{self.browser_path}\n",
            encoding="utf-8",
        )

    def _launcher(
        self,
        installation: ChromeInstallation,
        arguments: tuple[str, ...],
    ) -> LaunchServicesSubmission:
        self.launches.append(arguments)
        self.next_pid += 1
        birth = f"birth-{self.next_pid}"
        process = ChromeProcess(
            pid=self.next_pid,
            birth=birth,
            executable=installation.executable,
            owner_uid=os.getuid(),
            command_line=" ".join((str(installation.executable), *arguments)),
            birth_epoch_ns=self.clock_ns + 1,
        )
        self.processes[(process.pid, process.birth)] = process
        self.discovery_writer(self._profile_from_arguments(arguments))
        return LaunchServicesSubmission("launch_pending", self.clock_ns)

    def _process_probe(self, pid: int, birth: str):
        return self.processes.get((pid, birth))

    def _cdp_probe(self, port: int):
        self.assertEqual(port, self.port)
        return {
            "Browser": self.cdp_browser,
            "webSocketDebuggerUrl": (
                f"ws://127.0.0.1:{self.cdp_ws_port}{self.cdp_ws_path}"
            ),
        }

    def _normal_close(self, identity):
        self.close_requested = True
        self.processes.pop((identity.chrome_pid, identity.chrome_birth), None)
        return True

    def _manager(self, **overrides):
        values = {
            "chrome_probe": lambda: self.installation,
            "launch_chrome": self._launcher,
            "process_probe": self._process_probe,
            "process_snapshot": lambda _installation: tuple(
                self.processes.values()
            ),
            "listener_processes": lambda _port: tuple(self.processes.values()),
            "cdp_probe": self._cdp_probe,
            "listener_hosts": lambda _pid, _port: set(self.listener_result),
            "normal_close": self._normal_close,
            "controller_identity": lambda: (6001, "lulu-controller-current"),
            "daily_chrome_roots": (self.daily_chrome,),
            "now_ns": lambda: self.clock_ns,
            "discovery_timeout": 0.15,
        }
        values.update(overrides)
        return XhsBrowserManager(self.registry, **values)

    def test_t01_uses_only_fixed_xhs_default_profile_and_redacted_receipt(self):
        manager = self._manager()
        identity = manager.ensure_ready()
        self.addCleanup(manager.release_controller)

        profile = self._profile_from_arguments(self.launches[0])
        self.assertEqual(
            profile,
            self.app_data / "collection_profiles/xiaohongshu/default",
        )
        self.assertEqual(identity.state, "launched_ready")
        self.assertNotIn(str(self.port), repr(identity))
        self.assertNotIn(self.browser_path, repr(identity))
        receipt_path = (
            self.app_data / "collection_profiles/.xhs_browser/runtime.json"
        )
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertNotIn("cdp_port", payload)
        self.assertNotIn("endpoint", payload)
        self.assertNotIn("browser_path", payload)
        self.assertNotIn(str(self.port), receipt_path.read_text(encoding="utf-8"))

    def test_t02_rejects_symlink_ancestor_and_protected_path_relationship(self):
        self.app_data.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.app_data / "collection_profiles").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"当前文件系统不支持符号链接: {exc}")
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "profile_identity_blocked")
        self.assertEqual(self.launches, [])

        with tempfile.TemporaryDirectory() as temp:
            app_data = Path(temp) / "nested/app-data"
            registry = ProfileRegistry(app_data)
            manager = XhsBrowserManager(
                registry,
                chrome_probe=lambda: self.installation,
                launch_chrome=lambda *_args: self.fail("不得启动"),
                process_probe=lambda *_args: None,
                cdp_probe=lambda _port: {},
                listener_hosts=lambda *_args: set(),
                controller_identity=lambda: (6002, "controller-two"),
                daily_chrome_roots=(app_data.parent,),
            )
            with self.assertRaises(XhsBrowserError) as caught:
                manager.ensure_ready()
            self.assertEqual(caught.exception.code, "profile_identity_blocked")
            self.assertFalse(
                (app_data / "collection_profiles/xiaohongshu/default").exists()
            )

    def test_t02_rejects_douyin_inode_collision_without_deleting_either_path(self):
        profile = self.registry.resolve("xiaohongshu", create=True).path
        douyin = self.app_data / "collection_profiles/douyin"
        try:
            douyin.symlink_to(profile, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"当前文件系统不支持符号链接: {exc}")
        marker = profile / "keep"
        marker.write_text("safe", encoding="utf-8")

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "profile_identity_blocked")
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
        self.assertEqual(self.launches, [])

    def test_t03_chrome_135_or_unknown_is_rejected_without_launch_or_download(self):
        for version in ("135.0.1", "unknown"):
            with self.subTest(version=version):
                self.installation = ChromeInstallation(
                    self.chrome,
                    version,
                    app_bundle=self.chrome_app,
                )
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(
                    caught.exception.code,
                    "chrome_version_unsupported",
                )
                self.assertEqual(self.launches, [])

    def test_t03_missing_chrome_is_stable_and_has_no_launch(self):
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(chrome_probe=lambda: None).ensure_ready()
        self.assertEqual(caught.exception.code, "chrome_missing")
        self.assertEqual(self.launches, [])

    def test_t04_chrome_136_launch_has_exact_dynamic_loopback_arguments(self):
        manager = self._manager()
        manager.ensure_ready()
        self.addCleanup(manager.release_controller)

        arguments = self.launches[0]
        self.assertEqual(
            arguments,
            (
                "--user-data-dir="
                f"{self.app_data}/collection_profiles/xiaohongshu/default",
                "--profile-directory=Default",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                "https://www.xiaohongshu.com/",
            ),
        )
        joined = " ".join(arguments)
        for forbidden in (
            "--load-extension",
            "--disable-extensions-except",
            "--use-mock-keychain",
            "--password-store=basic",
        ):
            self.assertNotIn(forbidden, joined)

    def test_t05_stale_discovery_is_not_accepted_for_fresh_launch(self):
        def stale(profile: Path):
            self._write_valid_discovery(profile)
            os.utime(
                profile / "DevToolsActivePort",
                ns=(1_000_000_000, 1_000_000_000),
            )

        self.discovery_writer = stale
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "cdp_discovery_invalid")

    def test_t06_malformed_and_symlink_discovery_files_fail_closed(self):
        writers = []

        def malformed_port(profile: Path):
            (profile / "DevToolsActivePort").write_text(
                "70000\n/devtools/browser/fixture\n",
                encoding="utf-8",
            )

        writers.append(malformed_port)

        def malformed_path(profile: Path):
            (profile / "DevToolsActivePort").write_text(
                f"{self.port}\n/devtools/page/not-browser\n",
                encoding="utf-8",
            )

        writers.append(malformed_path)

        def symlink_file(profile: Path):
            outside = self.root / "forged-port"
            outside.write_text(
                f"{self.port}\n{self.browser_path}\n",
                encoding="utf-8",
            )
            (profile / "DevToolsActivePort").symlink_to(outside)

        writers.append(symlink_file)

        for index, writer in enumerate(writers):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                self.app_data = Path(temp) / "app-data"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()
                self.discovery_writer = writer
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_discovery_invalid")

    def test_t06_wrong_discovery_owner_is_rejected(self):
        profile = self.registry.resolve("xiaohongshu", create=True).path
        self._write_valid_discovery(profile)
        with patch(
            "core.collect.xhs_browser.os.getuid",
            return_value=os.getuid() + 1,
        ):
            with self.assertRaises(XhsBrowserError) as caught:
                XhsBrowserManager._read_discovery(
                    profile,
                    launch_epoch_ns=None,
                )
        self.assertEqual(caught.exception.code, "cdp_discovery_invalid")

    def test_t07_any_non_loopback_listener_fails_closed(self):
        for hosts in ({"0.0.0.0"}, {"127.0.0.1", "192.168.1.8"}, set()):
            with self.subTest(hosts=hosts):
                self.listener_result = hosts
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_exposure_blocked")
                self.launches.clear()
                self.processes.clear()
                self.app_data = self.root / f"app-data-{len(hosts)}-{len(str(hosts))}"
                self.registry = ProfileRegistry(self.app_data)

    def test_t08_fake_cdp_version_path_port_and_process_loss_are_rejected(self):
        cases = (
            ("Chrome/135.0", self.browser_path, self.port, True),
            ("Chrome/136.0", "/devtools/browser/other", self.port, True),
            ("Chrome/136.0", self.browser_path, self.port + 7, True),
            ("Chrome/136.0", self.browser_path, self.port, False),
        )
        for index, (browser, ws_path, ws_port, process_stays) in enumerate(cases):
            with self.subTest(index=index):
                self.cdp_browser = browser
                self.cdp_ws_path = ws_path
                self.cdp_ws_port = ws_port
                original_probe = self._process_probe
                probe_calls = 0

                def process_probe(pid, birth):
                    nonlocal probe_calls
                    probe_calls += 1
                    if not process_stays and probe_calls >= 1:
                        return None
                    return original_probe(pid, birth)

                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager(process_probe=process_probe).ensure_ready()
                self.assertIn(
                    caught.exception.code,
                    {"cdp_exposure_blocked", "browser_lost"},
                )
                self.app_data = self.root / f"app-data-cdp-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

    def test_t08_only_exact_google_chrome_or_chrome_brand_is_accepted(self):
        valid = (
            "Chrome/136",
            "Chrome/136.0.7103.48",
            "Google Chrome/136.0.7103.48",
            "Chrome/137.0",
        )
        for index, browser in enumerate(valid):
            with self.subTest(valid=browser):
                self.cdp_browser = browser
                manager = self._manager()
                identity = manager.ensure_ready()
                self.assertEqual(identity.state, "launched_ready")
                manager.release_controller()
                self.app_data = self.root / f"app-data-brand-valid-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

        invalid = (
            "Firefox/136.0",
            "Chromium/136.0",
            "FakeChrome/136.0",
            "ChromeFake/136.0",
            "Google Chromium/136.0",
            "chrome/136.0",
            "CHROME/136.0",
            " Chrome/136.0",
            "Chrome/136.0 ",
            "Chrome/136.0 extra",
            "Chrome/0136.0",
            "Chrome/136.0.0.0.1",
            "Chrome/136beta",
            "",
            None,
        )
        for index, browser in enumerate(invalid):
            with self.subTest(invalid=browser):
                self.cdp_browser = browser
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_exposure_blocked")
                self.app_data = self.root / f"app-data-brand-invalid-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

    def test_t08_chrome_brand_version_boundary_is_136(self):
        for index, browser in enumerate(
            ("Chrome/0", "Chrome/135", "Chrome/135.999.999.999")
        ):
            with self.subTest(rejected=browser):
                self.cdp_browser = browser
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_exposure_blocked")
                self.app_data = self.root / f"app-data-version-boundary-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

        self.cdp_browser = "Google Chrome/136.0"
        manager = self._manager()
        self.assertEqual(manager.ensure_ready().state, "launched_ready")
        manager.release_controller()

    def test_t08_pid_owner_executable_and_profile_command_identity_are_exact(self):
        mutations = (
            lambda process: ChromeProcess(
                process.pid,
                process.birth,
                process.executable,
                os.getuid() + 1,
                process.command_line,
                process.birth_epoch_ns,
            ),
            lambda process: ChromeProcess(
                process.pid,
                process.birth,
                self.root / "Not Google Chrome",
                os.getuid(),
                process.command_line,
                process.birth_epoch_ns,
            ),
            lambda process: ChromeProcess(
                process.pid,
                process.birth,
                process.executable,
                os.getuid(),
                process.command_line.replace(
                    "collection_profiles/xiaohongshu/default",
                    "collection_profiles/douyin/default",
                ),
                process.birth_epoch_ns,
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                def listeners(_port, *, mutate=mutate):
                    return tuple(mutate(item) for item in self.processes.values())

                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager(listener_processes=listeners).ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_exposure_blocked")
                self.app_data = self.root / f"app-data-process-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

    def test_t09_daily_chrome_and_unknown_profile_occupant_are_never_contacted(self):
        contacts = []
        manager = self._manager(
            daily_chrome_roots=(self.app_data / "collection_profiles",),
            cdp_probe=lambda _port: contacts.append("cdp"),
        )
        with self.assertRaises(XhsBrowserError) as caught:
            manager.ensure_ready()
        self.assertEqual(caught.exception.code, "profile_identity_blocked")
        self.assertEqual(contacts, [])
        self.assertEqual(self.launches, [])

        self.app_data = self.root / "app-data-occupied"
        self.registry = ProfileRegistry(self.app_data)
        profile = self.registry.resolve("xiaohongshu", create=True).path
        (profile / "SingletonLock").write_text("unknown", encoding="utf-8")
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "profile_in_use_unknown")
        self.assertEqual(self.launches, [])

    def test_controller_lease_is_exclusive_and_dead_owner_can_be_recovered(self):
        old_process = ChromeProcess(
            6200,
            "old-controller",
            self.chrome,
            os.getuid(),
            "fixture",
        )
        self.processes[(old_process.pid, old_process.birth)] = old_process
        old = self.registry.acquire_xhs_controller(
            owner_pid=old_process.pid,
            owner_birth=old_process.birth,
            identity_is_alive=lambda *_args: False,
        )
        self.addCleanup(old.release)
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "controller_busy")
        self.assertEqual(self.launches, [])

        self.processes.clear()
        manager = self._manager()
        manager.ensure_ready()
        manager.release_controller()
        old.release()

    def test_t22_release_controller_preserves_chrome_and_next_lulu_adopts(self):
        first = self._manager(
            controller_identity=lambda: (6301, "lulu-first"),
        )
        launched = first.ensure_ready()
        first.release_controller()

        second = self._manager(
            controller_identity=lambda: (6302, "lulu-second"),
        )
        adopted = second.ensure_ready()
        self.addCleanup(second.release_controller)

        self.assertEqual(len(self.launches), 1)
        self.assertEqual(adopted.state, "adopted_ready")
        self.assertEqual(adopted.chrome_pid, launched.chrome_pid)
        self.assertEqual(adopted.chrome_birth, launched.chrome_birth)

    def test_t23_dead_receipt_never_reuses_old_endpoint_and_launches_again(self):
        first = self._manager(
            controller_identity=lambda: (6401, "lulu-first"),
        )
        launched = first.ensure_ready()
        first.release_controller()
        self.processes.pop((launched.chrome_pid, launched.chrome_birth))

        self.port += 1
        self.browser_path = "/devtools/browser/restarted-browser"
        self.cdp_ws_path = self.browser_path
        self.cdp_ws_port = self.port
        second = self._manager(
            controller_identity=lambda: (6402, "lulu-second"),
        )
        restarted = second.ensure_ready()
        self.addCleanup(second.release_controller)

        self.assertEqual(restarted.state, "launched_ready")
        self.assertEqual(len(self.launches), 2)
        self.assertNotEqual(restarted.chrome_pid, launched.chrome_pid)

    def test_t24_corrupt_receipt_fails_as_unknown_occupant_without_launch(self):
        root = self.app_data / "collection_profiles/.xhs_browser"
        root.mkdir(parents=True)
        (root / "runtime.json").write_text('{"schema_version":99}', encoding="utf-8")
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "profile_in_use_unknown")
        self.assertEqual(self.launches, [])

    def test_t25_fixture_path_has_zero_real_process_or_network_entrypoints(self):
        manager = self._manager()
        with patch(
            "core.collect.xhs_browser.subprocess.Popen",
            side_effect=AssertionError("不得启动真实浏览器"),
        ) as popen, patch(
            "core.collect.xhs_browser.build_opener",
            side_effect=AssertionError("不得打开真实URL"),
        ) as opener, patch(
            "core.collect.xhs_browser._system_chrome",
            side_effect=AssertionError("不得读取真实Chrome"),
        ) as system_chrome:
            manager.ensure_ready()
        self.addCleanup(manager.release_controller)
        popen.assert_not_called()
        opener.assert_not_called()
        system_chrome.assert_not_called()

    def test_explicit_close_uses_only_normal_close_and_preserves_profile(self):
        manager = self._manager()
        manager.ensure_ready()
        profile = self._profile_from_arguments(self.launches[0])
        marker = profile / "Cookies"
        marker.write_text("keep", encoding="utf-8")

        manager.close_explicit(timeout=0.2)

        self.assertTrue(self.close_requested)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(
            (self.app_data / "collection_profiles/.xhs_browser/runtime.json").exists()
        )

    def test_close_failure_is_honest_and_never_deletes_profile_or_receipt(self):
        manager = self._manager(normal_close=lambda _identity: False)
        manager.ensure_ready()
        self.addCleanup(manager.release_controller)
        profile = self._profile_from_arguments(self.launches[0])

        with self.assertRaises(XhsBrowserError) as caught:
            manager.close_explicit(timeout=0.1)

        self.assertEqual(caught.exception.code, "close_failed_browser_alive")
        self.assertTrue(profile.exists())
        self.assertTrue(
            (self.app_data / "collection_profiles/.xhs_browser/runtime.json").exists()
        )

    def test_ls_a01_launch_argv_uses_exact_open_bundle_and_args(self):
        arguments = XhsBrowserManager._launch_arguments(
            self.app_data / "collection_profiles/xiaohongshu/default"
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch.object(
            xhs_browser.os,
            "uname",
            return_value=SimpleNamespace(sysname="Darwin"),
        ), patch.object(
            xhs_browser.subprocess,
            "run",
            return_value=completed,
        ) as run, patch.object(
            xhs_browser.subprocess,
            "Popen",
            side_effect=AssertionError("不得直接 Popen Chrome"),
        ) as popen:
            receipt = xhs_browser._launch_process(
                self.installation,
                arguments,
            )

        self.assertEqual(receipt.state, "launch_pending")
        popen.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "/usr/bin/open",
                "-n",
                "-a",
                str(self.chrome_app),
                "--args",
                *arguments,
            ],
        )

    def test_ls_a02_direct_chrome_popen_has_no_fallback(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="generic launch failure",
        )
        with patch.object(
            xhs_browser.os,
            "uname",
            return_value=SimpleNamespace(sysname="Darwin"),
        ), patch.object(
            xhs_browser.subprocess,
            "run",
            return_value=completed,
        ), patch.object(
            xhs_browser.subprocess,
            "Popen",
            side_effect=AssertionError("禁止直接启动 Chrome"),
        ) as popen:
            with self.assertRaises(XhsBrowserError) as caught:
                xhs_browser._launch_process(
                    self.installation,
                    XhsBrowserManager._launch_arguments(self.app_data),
                )
        self.assertEqual(caught.exception.code, "launchservices_submit_failed")
        popen.assert_not_called()

    def test_ls_a03_open_helper_is_never_used_as_chrome_pid(self):
        manager = self._manager(
            launch_chrome=lambda installation, arguments: (
                self._launcher(installation, arguments)
            ),
        )
        identity = manager.ensure_ready()
        self.addCleanup(manager.release_controller)

        self.assertEqual(identity.chrome_pid, self.next_pid)
        self.assertFalse(hasattr(LaunchServicesSubmission, "pid"))

    def test_ls_a04_nonzero_submit_is_stable_and_does_not_fallback(self):
        def rejected(_installation, _arguments):
            raise XhsBrowserError(
                "launchservices_submit_failed",
                "fixture submit failed",
            )

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(launch_chrome=rejected).ensure_ready()
        self.assertEqual(caught.exception.code, "launchservices_submit_failed")
        self.assertEqual(self.processes, {})

    def test_ls_a05_zero_submit_without_fresh_discovery_is_never_ready(self):
        def no_discovery(_installation, _arguments):
            return LaunchServicesSubmission("launch_pending", self.clock_ns)

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(launch_chrome=no_discovery).ensure_ready()
        self.assertEqual(caught.exception.code, "cdp_discovery_invalid")
        self.assertFalse(
            (
                self.app_data
                / "collection_profiles/.xhs_browser/runtime.json"
            ).exists()
        )

    def test_ls_a06_stale_discovery_cannot_bind_launch(self):
        def stale(profile: Path):
            self._write_valid_discovery(profile)
            os.utime(
                profile / "DevToolsActivePort",
                ns=(self.clock_ns - 1, self.clock_ns - 1),
            )

        self.discovery_writer = stale
        with self.assertRaises(XhsBrowserError) as caught:
            self._manager().ensure_ready()
        self.assertEqual(caught.exception.code, "cdp_discovery_invalid")

    def test_ls_a07_zero_or_multiple_listener_pids_are_ambiguous(self):
        def multiple(_port):
            original = next(iter(self.processes.values()))
            other = ChromeProcess(
                original.pid + 1,
                f"{original.birth}-other",
                original.executable,
                original.owner_uid,
                original.command_line,
                original.birth_epoch_ns,
            )
            return (original, other)

        for index, listeners in enumerate((lambda _port: (), multiple)):
            with self.subTest(index=index):
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager(listener_processes=listeners).ensure_ready()
                self.assertEqual(
                    caught.exception.code,
                    "launch_identity_ambiguous",
                )
                self.app_data = self.root / f"app-data-listener-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(process_snapshot=lambda _installation: None).ensure_ready()
        self.assertEqual(caught.exception.code, "launch_identity_ambiguous")
        self.assertEqual(self.launches, [])

    def test_ls_a08_birth_must_be_new_nonzero_and_stable(self):
        def listeners_with_epoch(epoch_ns):
            def listeners(_port):
                original = next(iter(self.processes.values()))
                return (
                    ChromeProcess(
                        original.pid,
                        original.birth,
                        original.executable,
                        original.owner_uid,
                        original.command_line,
                        epoch_ns,
                    ),
                )

            return listeners

        for index, epoch_ns in enumerate((0, self.clock_ns - 1)):
            with self.subTest(epoch_ns=epoch_ns):
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager(
                        listener_processes=listeners_with_epoch(epoch_ns)
                    ).ensure_ready()
                self.assertEqual(
                    caught.exception.code,
                    "launch_identity_ambiguous",
                )
                self.app_data = self.root / f"app-data-birth-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

        probe_calls = 0

        def drifting_probe(pid, birth):
            nonlocal probe_calls
            probe_calls += 1
            original = self.processes.get((pid, birth))
            if original is None:
                return None
            return ChromeProcess(
                original.pid,
                original.birth,
                original.executable,
                original.owner_uid,
                original.command_line,
                original.birth_epoch_ns + 1,
            )

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(process_probe=drifting_probe).ensure_ready()
        self.assertEqual(caught.exception.code, "browser_lost")

    def test_ls_a09_official_bundle_brand_version_signature_and_team_are_required(self):
        mutations = (
            {"bundle_identifier": "org.chromium.Chromium"},
            {"team_identifier": "NOTGOOGLE"},
            {"signature_valid": False},
        )
        for index, changes in enumerate(mutations):
            with self.subTest(changes=changes):
                values = {
                    "executable": self.chrome,
                    "version": "136.0.7103.48",
                    "bundle_identifier": "com.google.Chrome",
                    "app_bundle": self.chrome_app,
                    "team_identifier": "EQHXZ8M8AV",
                    "signature_valid": True,
                }
                values.update(changes)
                self.installation = ChromeInstallation(**values)
                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager().ensure_ready()
                self.assertEqual(
                    caught.exception.code,
                    "chrome_identity_untrusted",
                )
                self.app_data = self.root / f"app-data-official-{index}"
                self.registry = ProfileRegistry(self.app_data)

    def test_ls_a10_profile_default_dynamic_port_and_loopback_are_unique(self):
        def duplicate_profile(_port):
            original = next(iter(self.processes.values()))
            return (
                ChromeProcess(
                    original.pid,
                    original.birth,
                    original.executable,
                    original.owner_uid,
                    original.command_line
                    + " --profile-directory=Default",
                    original.birth_epoch_ns,
                ),
            )

        with self.assertRaises(XhsBrowserError) as caught:
            self._manager(listener_processes=duplicate_profile).ensure_ready()
        self.assertEqual(caught.exception.code, "cdp_exposure_blocked")

    def test_ls_a11_forbidden_arguments_are_rejected(self):
        for index, forbidden in enumerate(
            (
                "--use-mock-keychain",
                "--password-store=basic",
                "--load-extension=/tmp/fixture",
            )
        ):
            with self.subTest(forbidden=forbidden):
                def listeners(_port, *, forbidden=forbidden):
                    original = next(iter(self.processes.values()))
                    return (
                        ChromeProcess(
                            original.pid,
                            original.birth,
                            original.executable,
                            original.owner_uid,
                            f"{original.command_line} {forbidden}",
                            original.birth_epoch_ns,
                        ),
                    )

                with self.assertRaises(XhsBrowserError) as caught:
                    self._manager(listener_processes=listeners).ensure_ready()
                self.assertEqual(caught.exception.code, "cdp_exposure_blocked")
                self.app_data = self.root / f"app-data-forbidden-{index}"
                self.registry = ProfileRegistry(self.app_data)
                self.processes.clear()
                self.launches.clear()

    def test_ls_a12_daily_chrome_baseline_is_not_selected_or_changed(self):
        daily = ChromeProcess(
            7001,
            "daily-browser",
            self.chrome,
            os.getuid(),
            (
                f"{self.chrome} "
                "--user-data-dir=/Users/fixture/Library/Application Support/"
                "Google/Chrome"
            ),
            self.clock_ns - 100,
        )
        self.processes[(daily.pid, daily.birth)] = daily

        def dedicated_listener(_port):
            return tuple(
                item
                for item in self.processes.values()
                if item.birth != daily.birth
            )

        manager = self._manager(listener_processes=dedicated_listener)
        identity = manager.ensure_ready()
        self.addCleanup(manager.release_controller)

        self.assertNotEqual(identity.chrome_pid, daily.pid)
        self.assertIs(self.processes[(daily.pid, daily.birth)], daily)

    def test_ls_a13_lease_and_feature_gate_block_before_submit(self):
        manager = self._manager(xhs_launchservices_enabled=False)
        with self.assertRaises(XhsBrowserError) as caught:
            manager.ensure_ready()
        self.assertEqual(caught.exception.code, "launchservices_submit_failed")
        self.assertEqual(self.launches, [])

    def test_ls_a14_kls_no_executable_is_environment_only_and_has_no_fallback(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="kLSNoExecutableErr (-10827)",
        )
        with patch.object(
            xhs_browser.os,
            "uname",
            return_value=SimpleNamespace(sysname="Darwin"),
        ), patch.object(
            xhs_browser.subprocess,
            "run",
            return_value=completed,
        ), patch.object(
            xhs_browser.subprocess,
            "Popen",
            side_effect=AssertionError("不得回退直接启动"),
        ) as popen:
            with self.assertRaises(XhsBrowserError) as caught:
                xhs_browser._launch_process(
                    self.installation,
                    XhsBrowserManager._launch_arguments(self.app_data),
                )
        self.assertEqual(
            caught.exception.code,
            "launchservices_environment_blocked",
        )
        popen.assert_not_called()

    def test_ls_a15_receipts_and_errors_do_not_expose_endpoint_or_secrets(self):
        manager = self._manager()
        identity = manager.ensure_ready()
        self.addCleanup(manager.release_controller)
        receipt_text = (
            self.app_data
            / "collection_profiles/.xhs_browser/runtime.json"
        ).read_text(encoding="utf-8")
        combined = f"{identity!r}\n{receipt_text}"
        self.assertNotIn(str(self.port), combined)
        self.assertNotIn(self.browser_path, combined)
        self.assertNotIn("Cookie", combined)
        self.assertNotIn("Keychain", combined)

    def test_ls_a16_fixture_regression_has_zero_external_entrypoints(self):
        with patch.object(
            xhs_browser.subprocess,
            "Popen",
            side_effect=AssertionError("不得启动真实进程"),
        ) as popen, patch.object(
            xhs_browser.subprocess,
            "run",
            side_effect=AssertionError("不得调用系统启动器"),
        ) as run, patch.object(
            xhs_browser.subprocess,
            "check_output",
            side_effect=AssertionError("不得调用真实系统进程探针"),
        ) as check_output, patch.object(
            socket,
            "socket",
            side_effect=AssertionError("不得打开真实 socket"),
        ) as sockets, patch.object(
            socket,
            "getaddrinfo",
            side_effect=AssertionError("不得访问系统 DNS"),
        ) as getaddrinfo, patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("不得建立真实连接"),
        ) as create_connection, patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("不得打开真实 URL"),
        ) as urlopen, patch.object(
            xhs_browser,
            "build_opener",
            side_effect=AssertionError("不得创建真实网络 opener"),
        ) as build_opener, patch.object(
            webbrowser,
            "open",
            side_effect=AssertionError("不得打开系统浏览器"),
        ) as browser_open, patch.object(
            xhs_browser,
            "_system_chrome",
            side_effect=AssertionError("不得读取真实 Chrome"),
        ) as system_chrome:
            first = self._manager(
                controller_identity=lambda: (6501, "first-lulu"),
            )
            launched = first.ensure_ready()
            first.release_controller()
            second = self._manager(
                controller_identity=lambda: (6502, "second-lulu"),
            )
            adopted = second.ensure_ready()
            second.close_explicit(timeout=0.2)

        self.assertEqual(adopted.chrome_pid, launched.chrome_pid)
        popen.assert_not_called()
        run.assert_not_called()
        check_output.assert_not_called()
        sockets.assert_not_called()
        getaddrinfo.assert_not_called()
        create_connection.assert_not_called()
        urlopen.assert_not_called()
        build_opener.assert_not_called()
        browser_open.assert_not_called()
        system_chrome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
