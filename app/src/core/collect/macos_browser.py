"""Launch owned browser sessions through macOS, then attach the driver.

Directly spawning Chrome attributes its app-signature clone writes to Lulu.
LaunchServices preserves the normal application boundary without weakening
Chrome features or asking Lulu for permission to modify other applications.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import re
import time

from . import xhs_browser as system


# The download supervisor records ownership before LaunchServices is called,
# because an OS-launched app is outside the worker process group.
launch_observer = None


class ChromeLaunchError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)



def _notify(event, ticket):
    if launch_observer is not None:
        launch_observer(event, ticket)


def reap_launch(ticket):
    installation = system.ChromeInstallation(Path(ticket["executable"]), "")
    # A killed worker may have submitted a launch that macOS has not finished.
    remaining = max(0.0, 10.0 - (time.time_ns() - ticket["started"]) / 1e9)
    deadline = time.monotonic() + min(10.0, remaining)
    while True:
        snapshot = system._chrome_process_snapshot(installation)
        if snapshot is None:
            raise RuntimeError("无法核实专用浏览器是否已结束。")
        found = False
        for process in snapshot:
            if _owned(process, installation, Path(ticket["profile"]), ticket["port"], ticket["started"]):
                _stop_owned(process)
                found = True
        if found or time.monotonic() >= deadline:
            return
        time.sleep(.1)


def _owned(process, installation, profile, port, started):
    required = (f"--user-data-dir={profile}", f"--remote-debugging-port={port}")
    return (
        process.owner_uid == os.getuid()
        and process.birth_epoch_ns >= started
        and process.executable.resolve() == installation.executable.resolve()
        and all(len(re.findall(r"(?<!\S)" + re.escape(arg) + r"(?= --|$)", process.command_line)) == 1 for arg in required)
        and process.command_line.count("--user-data-dir=") == 1
    )


def _same_process(process):
    current = system._inspect_process(process.pid, process.birth)
    return (current is not None
            and current.birth_epoch_ns == process.birth_epoch_ns
            and current.executable == process.executable
            and current.owner_uid == process.owner_uid)


def _stop_owned(process):
    # Revalidate birth before every signal; never kill a recycled PID.
    for sig, wait in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        if not _same_process(process):
            return
        try:
            os.kill(process.pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _same_process(process):
                return
            time.sleep(.05)
    if _same_process(process):
        raise RuntimeError("专用浏览器未能结束，请关闭该浏览器后重试。")


class _OwnedPage:
    def __init__(self, page, process, ticket=None):
        self._page = page
        self._process = process
        self._ticket = ticket

    def __getattr__(self, name):
        return getattr(self._page, name)

    def quit(self, *args, **kwargs):
        try:
            return self._page.quit(*args, **kwargs)
        finally:
            _stop_owned(self._process)
            if self._ticket is not None:
                _notify("browser_released", self._ticket)


def create_page(options, page_factory):
    """Submit a new, isolated app instance. Never fall back to direct spawn."""
    from DrissionPage._functions.browser import get_launch_args, set_prefs, set_flags

    executable = Path(options.browser_path).expanduser()
    if not executable.is_absolute():
        installation = system._system_chrome()
        if installation is None:
            raise ChromeLaunchError('browser_missing', '请先安装 Google Chrome，或手动填写 Cookie。')
        if not installation.signature_valid:
            raise ChromeLaunchError('browser_unverified', 'Chrome 当前无法用于登录，请更新或修复 Chrome，或手动填写 Cookie。')
        executable = installation.executable
    else:
        # Accept the existing explicitly selected browser, but only app bundles.
        if executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
            raise RuntimeError("请选择应用程序内的浏览器，无法通过 macOS 启动此路径。")
        installation = system.ChromeInstallation(executable, "", app_bundle=executable.parents[2])
    if not executable.is_file() or not installation.app_bundle.is_dir():
        raise RuntimeError("浏览器应用不存在。")
    profile = Path(options.user_data_path).absolute()
    host, raw_port = options.address.rsplit(":", 1)
    if host not in ("localhost", "127.0.0.1"):
        raise RuntimeError("专用浏览器仅允许本机连接。")
    port = int(raw_port)
    args, _ = get_launch_args(options)
    args = [arg for arg in args if not arg.startswith("--remote-debugging-address=")]
    args += [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", "about:blank"]
    set_prefs(options)
    set_flags(options)
    started = time.time_ns()
    process = None
    submitted = False
    ticket = {"executable": str(executable), "profile": str(profile), "port": port, "started": started}
    try:
        # A timeout may still mean the OS accepted the submission.
        _notify("browser_launch", ticket)
        submitted = True
        system._launch_process(installation, tuple(args))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            listeners = system._listener_processes(port)
            if listeners:
                if len(listeners) != 1 or not _owned(listeners[0], installation, profile, port, started):
                    raise RuntimeError("专用浏览器身份不匹配，已停止连接。")
                process = listeners[0]
                hosts = system._listener_hosts(process.pid, port)
                if not hosts or not hosts <= {"127.0.0.1", "::1"}:
                    raise RuntimeError("专用浏览器监听范围不安全，已停止连接。")
                try:
                    ready = bool(system._probe_cdp(port))
                except (OSError, ValueError):
                    ready = False
                if ready:
                    options.existing_only(True)
                    return _OwnedPage(page_factory(options), process, ticket)
            time.sleep(.1)
        raise RuntimeError("专用浏览器启动超时。")
    except BaseException:
        if process is not None:
            _stop_owned(process)
            _notify("browser_released", ticket)
        elif submitted:
            reap_launch(ticket)
            _notify("browser_released", ticket)
        raise
