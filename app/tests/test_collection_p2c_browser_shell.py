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


if __name__ == "__main__":
    unittest.main()
