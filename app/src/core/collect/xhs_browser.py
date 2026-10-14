"""小红书专用常驻 Google Chrome 的 P0 生命周期与安全门。

浏览器可跨采集任务和 Lulu 重启保留。本模块只管理固定的
``collection_profiles/xiaohongshu/default``，动态 CDP 端点只存在内存中。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import ctypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time
from typing import Any, NoReturn
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .profiles import (
    ProfileError,
    ProfileRegistry,
    XhsBrowserProfile,
    XhsBrowserReceipt,
    XhsControllerLease,
)
from .adapters import homepage_collection_available

_CHROME_EXECUTABLE = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
_CHROME_APP = Path("/Applications/Google Chrome.app")
_CHROME_INFO = Path("/Applications/Google Chrome.app/Contents/Info.plist")
_OPEN_EXECUTABLE = Path("/usr/bin/open")
_GOOGLE_CHROME_TEAM_ID = "EQHXZ8M8AV"
_HOMEPAGE = "https://www.xiaohongshu.com/"
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9._-]{1,200}\Z")
_CDP_CHROME_IDENTITY = re.compile(
    r"(?:Google Chrome|Chrome)/"
    r"(?P<major>0|[1-9]\d*)"
    r"(?:\.(?:0|[1-9]\d*)){0,3}\Z"
)
_FORBIDDEN_ARGUMENTS = (
    "--load-extension",
    "--disable-extensions-except",
    "--use-mock-keychain",
    "--password-store=basic",
)


class XhsBrowserError(RuntimeError):
    """稳定、脱敏的小红书专用浏览器失败合同。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ChromeInstallation:
    executable: Path
    version: str
    bundle_identifier: str = "com.google.Chrome"
    app_bundle: Path = _CHROME_APP
    team_identifier: str = _GOOGLE_CHROME_TEAM_ID
    signature_valid: bool = True


@dataclass(frozen=True)
class ChromeProcess:
    pid: int
    birth: str
    executable: Path
    owner_uid: int
    command_line: str
    birth_epoch_ns: int = 0


@dataclass(frozen=True)
class LaunchServicesSubmission:
    """LaunchServices 只提交请求，不提供可受信任的 Chrome PID。"""

    state: str
    submitted_at_ns: int


@dataclass(frozen=True, repr=False)
class CdpEndpoint:
    """端点不得进入日志或普通异常；repr 固定脱敏。"""

    host: str = field(repr=False)
    port: int = field(repr=False)
    browser_path: str = field(repr=False)

    def __repr__(self) -> str:
        return "CdpEndpoint(<redacted>)"


@dataclass(frozen=True)
class XhsBrowserIdentity:
    state: str
    chrome_pid: int
    chrome_birth: str
    profile_identity_digest: str
    endpoint: CdpEndpoint = field(repr=False)


def _major_version(value: str) -> int | None:
    match = re.search(r"(?:^|/|\s)(\d+)(?:\.|\s|$)", str(value or ""))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _cdp_chrome_major(value: object) -> int | None:
    """只接受 /json/version Browser 的完整 Google Chrome/Chrome 身份。"""
    if not isinstance(value, str):
        return None
    match = _CDP_CHROME_IDENTITY.fullmatch(value)
    if match is None:
        return None
    try:
        return int(match.group("major"))
    except ValueError:
        return None


def _system_chrome() -> ChromeInstallation | None:
    """只读标准 Google Chrome bundle 元数据，不自动下载或启动。"""
    try:
        with _CHROME_INFO.open("rb") as stream:
            payload = plistlib.load(stream)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not _CHROME_EXECUTABLE.is_file():
        return None
    bundle_identifier = payload.get("CFBundleIdentifier")
    if not isinstance(bundle_identifier, str):
        bundle_identifier = ""
    version = payload.get("CFBundleShortVersionString")
    if not isinstance(version, str):
        return None
    try:
        verify = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                str(_CHROME_APP),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
        details = subprocess.run(
            [
                "/usr/bin/codesign",
                "-dv",
                "--verbose=4",
                str(_CHROME_APP),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?m)^TeamIdentifier=(\S+)\s*$", details.stderr or "")
    team_identifier = match.group(1) if match is not None else ""
    signature_valid = (
        verify.returncode == 0
        and details.returncode == 0
        and team_identifier == _GOOGLE_CHROME_TEAM_ID
    )
    return ChromeInstallation(
        _CHROME_EXECUTABLE,
        version,
        bundle_identifier=bundle_identifier,
        app_bundle=_CHROME_APP,
        team_identifier=team_identifier,
        signature_valid=signature_valid,
    )


def _process_birth(pid: int) -> str:
    try:
        raw = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return raw


class _ProcBsdInfo(ctypes.Structure):
    """Darwin ``proc_bsdinfo`` 的稳定前缀与高精度出生时间。"""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _process_birth_epoch_ns(pid: int) -> int:
    if os.uname().sysname != "Darwin":
        return 0
    info = _ProcBsdInfo()
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        size = ctypes.sizeof(info)
        result = libproc.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            size,
        )
    except (AttributeError, OSError):
        return 0
    if result != size or info.pbi_start_tvsec <= 0:
        return 0
    return (
        int(info.pbi_start_tvsec) * 1_000_000_000
        + int(info.pbi_start_tvusec) * 1_000
    )


def _pid_executable(pid: int) -> Path | None:
    if os.uname().sysname != "Darwin":
        return None
    buffer = ctypes.create_string_buffer(4096)
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        result = libproc.proc_pidpath(pid, buffer, len(buffer))
    except (AttributeError, OSError):
        return None
    if result <= 0:
        return None
    return Path(os.fsdecode(buffer.value))


def _inspect_process(pid: int, birth: str) -> ChromeProcess | None:
    current_birth = _process_birth(pid)
    if not current_birth or current_birth != birth:
        return None
    executable = _pid_executable(pid)
    if executable is None:
        return None
    try:
        uid_raw = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "uid="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
        command = subprocess.check_output(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
        uid = int(uid_raw)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return ChromeProcess(
        pid,
        birth,
        executable,
        uid,
        command,
        _process_birth_epoch_ns(pid),
    )


def _launch_process(
    installation: ChromeInstallation,
    arguments: tuple[str, ...],
) -> LaunchServicesSubmission:
    """经 LaunchServices 提交新实例；helper 结果绝不冒充 Chrome 身份。"""
    if os.uname().sysname != "Darwin" or not _OPEN_EXECUTABLE.is_file():
        raise XhsBrowserError(
            "launchservices_environment_blocked",
            "当前环境无法提交 macOS 浏览器启动请求。",
        )
    argv = [
        str(_OPEN_EXECUTABLE),
        "-n",
        "-a",
        str(installation.app_bundle),
        "--args",
        *arguments,
    ]
    try:
        result = subprocess.run(
            argv,
            check=False,
        stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XhsBrowserError(
            "launchservices_submit_failed",
            "专用浏览器启动请求提交失败。",
        ) from exc
    stderr = result.stderr or ""
    if result.returncode != 0:
        if "kLSNoExecutableErr" in stderr or "-10827" in stderr:
            raise XhsBrowserError(
                "launchservices_environment_blocked",
                "当前环境无法提交 macOS 浏览器启动请求。",
            )
        raise XhsBrowserError(
            "launchservices_submit_failed",
            "专用浏览器启动请求提交失败。",
        )
    return LaunchServicesSubmission(
        state="launch_pending",
        submitted_at_ns=time.time_ns(),
    )


def _chrome_process_snapshot(
    installation: ChromeInstallation,
) -> tuple[ChromeProcess, ...] | None:
    """数值枚举当前进程，只保留精确官方可执行路径；不按名称猜 PID。"""
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    expected = installation.executable.resolve(strict=False)
    result: list[ChromeProcess] = []
    for value in raw.split():
        try:
            pid = int(value)
        except ValueError:
            continue
        executable = _pid_executable(pid)
        if executable is None:
            continue
        try:
            if executable.resolve(strict=False) != expected:
                continue
        except (OSError, RuntimeError):
            continue
        birth = _process_birth(pid)
        process = _inspect_process(pid, birth) if birth else None
        if process is not None:
            result.append(process)
    return tuple(result)


def _listener_processes(port: int) -> tuple[ChromeProcess, ...]:
    """从新鲜动态端口反查所有监听 PID；零个或多个交由上层失败关闭。"""
    try:
        raw = subprocess.check_output(
            [
                "lsof",
                "-nP",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-F",
                "p",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    result: list[ChromeProcess] = []
    for value in dict.fromkeys(
        line[1:] for line in raw.splitlines() if line.startswith("p")
    ):
        try:
            pid = int(value)
        except ValueError:
            continue
        birth = _process_birth(pid)
        process = _inspect_process(pid, birth) if birth else None
        if process is not None:
            result.append(process)
    return tuple(result)


def _probe_cdp(port: int) -> Mapping[str, Any]:
    opener = build_opener(ProxyHandler({}))
    with opener.open(  # noqa: S310 - host is fixed loopback
        f"http://127.0.0.1:{port}/json/version",
        timeout=1.0,
    ) as response:
        raw = response.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("response too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid response")
    return payload


def _listener_hosts(pid: int, port: int) -> set[str]:
    """用系统 lsof 枚举该 PID 的指定监听口；空集合也会失败关闭。"""
    try:
        raw = subprocess.check_output(
            [
                "lsof",
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-F",
                "n",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    hosts: set[str] = set()
    for line in raw.splitlines():
        if not line.startswith("n"):
            continue
        address = line[1:].split("->", 1)[0]
        if address.startswith("["):
            hosts.add(address[1:].split("]", 1)[0])
        elif ":" in address:
            hosts.add(address.rsplit(":", 1)[0])
    return hosts


def _normal_close(_identity: XhsBrowserIdentity) -> bool:
    """P0 不猜测关闭协议；后续接线必须注入已验证的 browser-level CDP close。"""
    return False


class XhsBrowserManager:
    """专用 Chrome 的启动、严格接管和显式正常关闭管理器。"""

    def __init__(
        self,
        registry: ProfileRegistry,
        *,
        chrome_probe: Callable[[], ChromeInstallation | None] = _system_chrome,
        launch_chrome: Callable[
            [ChromeInstallation, tuple[str, ...]], LaunchServicesSubmission
        ] = _launch_process,
        process_probe: Callable[[int, str], ChromeProcess | None] = _inspect_process,
        process_snapshot: Callable[
            [ChromeInstallation], tuple[ChromeProcess, ...] | None
        ] = _chrome_process_snapshot,
        listener_processes: Callable[[int], tuple[ChromeProcess, ...]] = (
            _listener_processes
        ),
        cdp_probe: Callable[[int], Mapping[str, Any]] = _probe_cdp,
        listener_hosts: Callable[[int, int], set[str]] = _listener_hosts,
        normal_close: Callable[[XhsBrowserIdentity], bool] = _normal_close,
        controller_identity: Callable[[], tuple[int, str]] | None = None,
        daily_chrome_roots: tuple[Path | str, ...] | None = None,
        profile_occupied: Callable[[Path], bool] | None = None,
        now_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        discovery_timeout: float = 5.0,
        xhs_launchservices_enabled: bool = True,
    ) -> None:
        self.registry = registry
        self.chrome_probe = chrome_probe
        self.launch_chrome = launch_chrome
        self.process_probe = process_probe
        self.process_snapshot = process_snapshot
        self.listener_processes = listener_processes
        self.cdp_probe = cdp_probe
        self.listener_hosts = listener_hosts
        self.normal_close = normal_close
        self.controller_identity = controller_identity or (
            lambda: (os.getpid(), _process_birth(os.getpid()))
        )
        self.daily_chrome_roots = daily_chrome_roots or (
            Path.home() / "Library/Application Support/Google/Chrome",
        )
        self.profile_occupied = profile_occupied or self._default_profile_occupied
        self.now_ns = now_ns
        self.monotonic = monotonic
        self.sleep = sleep
        self.xhs_launchservices_enabled = bool(xhs_launchservices_enabled)
        if not 0.1 <= discovery_timeout <= 30.0:
            raise ValueError("discovery_timeout out of range")
        self.discovery_timeout = discovery_timeout
        self._controller: XhsControllerLease | None = None
        self._identity: XhsBrowserIdentity | None = None

    @staticmethod
    def _fail(code: str, message: str) -> NoReturn:
        raise XhsBrowserError(code, message)

    @staticmethod
    def _default_profile_occupied(profile: Path) -> bool:
        return any(
            (profile / name).exists() or (profile / name).is_symlink()
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
        )

    @staticmethod
    def _launch_arguments(profile: Path) -> tuple[str, ...]:
        return (
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            _HOMEPAGE,
        )

    def _installation(self) -> ChromeInstallation:
        installation = self.chrome_probe()
        if installation is None:
            self._fail("chrome_missing", "未发现系统 Google Chrome。")
        major = _major_version(installation.version)
        if major is None or major < 136:
            self._fail(
                "chrome_version_unsupported",
                "需要 Google Chrome 136 或更高版本。",
            )
        if (
            not installation.executable.is_absolute()
            or not installation.app_bundle.is_absolute()
        ):
            self._fail("chrome_missing", "Google Chrome 路径无法验证。")
        if (
            installation.bundle_identifier != "com.google.Chrome"
            or not installation.signature_valid
            or installation.team_identifier != _GOOGLE_CHROME_TEAM_ID
        ):
            self._fail(
                "chrome_identity_untrusted",
                "Google Chrome 官方身份验证失败。",
            )
        return installation

    def _acquire_controller(self) -> None:
        if self._controller is not None:
            return
        pid, birth = self.controller_identity()
        if not birth:
            self._fail("controller_busy", "当前 Lulu 控制器身份无法验证。")
        try:
            self._controller = self.registry.acquire_xhs_controller(
                owner_pid=pid,
                owner_birth=birth,
                identity_is_alive=lambda old_pid, old_birth: (
                    self.process_probe(old_pid, old_birth) is not None
                ),
            )
        except ProfileError as exc:
            if exc.code in {
                "symlink_rejected",
                "path_escape",
                "unsafe_app_data",
                "invalid_profile_path",
            }:
                raise XhsBrowserError(
                    "profile_identity_blocked",
                    "小红书专用 Profile 身份验证失败。",
                ) from exc
            raise XhsBrowserError(exc.code, str(exc)) from exc

    @staticmethod
    def _validate_process(
        process: ChromeProcess,
        installation: ChromeInstallation,
        profile: Path,
    ) -> None:
        current_uid = getattr(os, "getuid", lambda: process.owner_uid)()
        try:
            executable_matches = (
                process.executable.resolve(strict=False)
                == installation.executable.resolve(strict=False)
            )
        except (OSError, RuntimeError):
            executable_matches = False
        required = (
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            _HOMEPAGE,
        )
        if (
            isinstance(process.pid, bool)
            or process.pid <= 1
            or not process.birth
            or process.owner_uid != current_uid
            or not executable_matches
            or any(process.command_line.count(value) != 1 for value in required)
            or any(value in process.command_line for value in _FORBIDDEN_ARGUMENTS)
        ):
            raise XhsBrowserError(
                "cdp_exposure_blocked",
                "专用浏览器进程身份验证失败。",
            )
        user_data_flags = re.findall(r"--user-data-dir=", process.command_line)
        if len(user_data_flags) != 1:
            raise XhsBrowserError(
                "cdp_exposure_blocked",
                "专用浏览器 Profile 参数身份不唯一。",
            )

    @staticmethod
    def _read_discovery(
        profile: Path,
        *,
        launch_epoch_ns: int | None,
    ) -> CdpEndpoint:
        path = profile / "DevToolsActivePort"
        try:
            before = path.lstat()
        except OSError as exc:
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点尚未可信就绪。",
            ) from exc
        uid = getattr(os, "getuid", lambda: before.st_uid)()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_size > 4096
            or (
                launch_epoch_ns is not None
                and before.st_mtime_ns < launch_epoch_ns
            )
        ):
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点发现文件不可信。",
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点发现文件不可信。",
            ) from exc
        try:
            opened = os.fstat(fd)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_uid != before.st_uid
            ):
                raise XhsBrowserError(
                    "cdp_discovery_invalid",
                    "动态调试端点发现文件发生漂移。",
                )
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点发现文件过大。",
            )
        try:
            lines = raw.decode("utf-8").splitlines()
            port = int(lines[0])
            browser_path = lines[1]
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点发现文件格式错误。",
            ) from exc
        if (
            len(lines) != 2
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or _BROWSER_PATH.fullmatch(browser_path) is None
        ):
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "动态调试端点发现文件格式错误。",
            )
        return CdpEndpoint("127.0.0.1", port, browser_path)

    def _wait_for_discovery(
        self,
        profile: Path,
        launch_epoch_ns: int,
    ) -> CdpEndpoint:
        deadline = self.monotonic() + self.discovery_timeout
        last_error: XhsBrowserError | None = None
        while self.monotonic() < deadline:
            try:
                return self._read_discovery(
                    profile,
                    launch_epoch_ns=launch_epoch_ns,
                )
            except XhsBrowserError as exc:
                last_error = exc
                self.sleep(0.05)
        if last_error is not None:
            raise last_error
        self._fail("cdp_discovery_invalid", "动态调试端点未在时限内就绪。")

    def _verify_endpoint(
        self,
        process: ChromeProcess,
        endpoint: CdpEndpoint,
    ) -> None:
        hosts = self.listener_hosts(process.pid, endpoint.port)
        if not hosts or not hosts.issubset({"127.0.0.1"}):
            self._fail("cdp_exposure_blocked", "调试端点未被证明仅监听本机回环。")
        try:
            payload = self.cdp_probe(endpoint.port)
        except Exception as exc:  # noqa: BLE001 - 翻译为脱敏稳定错误
            raise XhsBrowserError(
                "cdp_exposure_blocked",
                "调试服务身份验证失败。",
            ) from exc
        browser = payload.get("Browser")
        ws_url = payload.get("webSocketDebuggerUrl")
        if not isinstance(browser, str) or not isinstance(ws_url, str):
            self._fail("cdp_exposure_blocked", "调试服务身份验证失败。")
        parsed = urlsplit(ws_url)
        try:
            ws_port = parsed.port
        except ValueError:
            ws_port = None
        browser_major = _cdp_chrome_major(browser)
        if (
            browser_major is None
            or browser_major < 136
            or parsed.scheme != "ws"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or ws_port != endpoint.port
            or parsed.path != endpoint.browser_path
            or parsed.query
            or parsed.fragment
        ):
            self._fail("cdp_exposure_blocked", "调试服务身份验证失败。")
        refreshed = self.process_probe(process.pid, process.birth)
        if (
            refreshed is None
            or refreshed.birth_epoch_ns != process.birth_epoch_ns
        ):
            self._fail("browser_lost", "小红书专用浏览器已退出。")

    @staticmethod
    def _remove_discovery_after_death(profile: Path) -> None:
        path = profile / "DevToolsActivePort"
        if not path.exists() and not path.is_symlink():
            return
        try:
            value = path.lstat()
        except OSError as exc:
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "无法清理陈旧的动态端点发现文件。",
            ) from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise XhsBrowserError(
                "cdp_discovery_invalid",
                "陈旧的动态端点发现文件不可信。",
            )
        path.unlink()
        ProfileRegistry._fsync_directory(profile)

    def _ready(
        self,
        *,
        state: str,
        process: ChromeProcess,
        profile: XhsBrowserProfile,
        launched_at_ns: int,
        endpoint: CdpEndpoint,
    ) -> XhsBrowserIdentity:
        verified_at_ns = max(self.now_ns(), launched_at_ns)
        receipt = XhsBrowserReceipt(
            profile_identity_digest=profile.identity_digest,
            chrome_pid=process.pid,
            chrome_birth=process.birth,
            launched_at_ns=launched_at_ns,
            verified_at_ns=verified_at_ns,
        )
        self.registry.save_xhs_browser_receipt(receipt)
        identity = XhsBrowserIdentity(
            state=state,
            chrome_pid=process.pid,
            chrome_birth=process.birth,
            profile_identity_digest=profile.identity_digest,
            endpoint=endpoint,
        )
        self._identity = identity
        return identity

    def ensure_ready(self) -> XhsBrowserIdentity:
        """取得 controller 后严格接管现存 Chrome，或用动态端口新启动。"""
        if not homepage_collection_available("xiaohongshu"):
            raise XhsBrowserError(
                "release_disabled",
                "小红书主页采集本版暂缓开放，不会启动浏览器。",
            )
        self._acquire_controller()
        try:
            installation = self._installation()
            try:
                profile = self.registry.validate_xhs_browser_profile(
                    daily_chrome_roots=self.daily_chrome_roots,
                    create=True,
                )
            except ProfileError as exc:
                raise XhsBrowserError(
                    "profile_identity_blocked",
                    "小红书专用 Profile 身份验证失败。",
                ) from exc
            profile_path = profile.resolution.path
            try:
                receipt = self.registry.load_xhs_browser_receipt()
            except ProfileError as exc:
                raise XhsBrowserError("profile_in_use_unknown", str(exc)) from exc
            allow_stale_singleton = False
            if receipt is not None:
                if receipt.profile_identity_digest != profile.identity_digest:
                    self._fail(
                        "profile_identity_blocked",
                        "专用浏览器收据与 Profile 身份不一致。",
                    )
                process = self.process_probe(receipt.chrome_pid, receipt.chrome_birth)
                if process is not None:
                    self._validate_process(process, installation, profile_path)
                    endpoint = self._read_discovery(
                        profile_path,
                        launch_epoch_ns=None,
                    )
                    self._verify_endpoint(process, endpoint)
                    return self._ready(
                        state="adopted_ready",
                        process=process,
                        profile=profile,
                        launched_at_ns=receipt.launched_at_ns,
                        endpoint=endpoint,
                    )
                if not self.registry.delete_xhs_browser_receipt(receipt):
                    self._fail(
                        "profile_in_use_unknown",
                        "专用浏览器运行收据发生并发漂移。",
                    )
                self._remove_discovery_after_death(profile_path)
                allow_stale_singleton = True

            discovery = profile_path / "DevToolsActivePort"
            if (
                not allow_stale_singleton
                and (
                    self.profile_occupied(profile_path)
                    or discovery.exists()
                    or discovery.is_symlink()
                )
            ):
                self._fail(
                    "profile_in_use_unknown",
                    "专用 Profile 可能正被未知浏览器占用。",
                )

            if not self.xhs_launchservices_enabled:
                self._fail(
                    "launchservices_submit_failed",
                    "专用浏览器新启动当前已暂停。",
                )
            snapshot = self.process_snapshot(installation)
            if snapshot is None:
                self._fail(
                    "launch_identity_ambiguous",
                    "无法建立现有 Chrome 进程安全基线。",
                )
            baseline = {
                (item.pid, item.birth)
                for item in snapshot
            }
            launch_epoch_ns = self.now_ns()
            arguments = self._launch_arguments(profile_path)
            submission = self.launch_chrome(installation, arguments)
            if (
                not isinstance(submission, LaunchServicesSubmission)
                or submission.state != "launch_pending"
                or submission.submitted_at_ns < launch_epoch_ns
            ):
                self._fail(
                    "launchservices_submit_failed",
                    "专用浏览器启动请求收据无效。",
                )
            endpoint = self._wait_for_discovery(profile_path, launch_epoch_ns)
            candidates = self.listener_processes(endpoint.port)
            if len(candidates) != 1:
                self._fail(
                    "launch_identity_ambiguous",
                    "无法唯一绑定小红书专用浏览器进程。",
                )
            process = candidates[0]
            if (
                (process.pid, process.birth) in baseline
                or process.birth_epoch_ns <= 0
                or process.birth_epoch_ns < launch_epoch_ns
            ):
                self._fail(
                    "launch_identity_ambiguous",
                    "小红书专用浏览器出生身份不可信。",
                )
            self._validate_process(process, installation, profile_path)
            self._verify_endpoint(process, endpoint)
            return self._ready(
                state="launched_ready",
                process=process,
                profile=profile,
                launched_at_ns=launch_epoch_ns,
                endpoint=endpoint,
            )
        except ProfileError as exc:
            self.release_controller()
            raise XhsBrowserError(exc.code, str(exc)) from exc
        except Exception:
            self.release_controller()
            raise

    def release_controller(self) -> None:
        """释放 Lulu 控制权但保留 Chrome、Profile 与脱敏运行收据。"""
        if self._controller is not None:
            self._controller.release()
            self._controller = None
        self._identity = None

    def close_explicit(self, *, timeout: float = 5.0) -> None:
        """只走正常关闭；超时绝不发送 SIGTERM/SIGKILL。"""
        if not homepage_collection_available("xiaohongshu"):
            self._fail(
                "release_disabled",
                "小红书主页采集本版暂缓开放，不会启动浏览器。",
            )
        identity = self._identity
        if identity is None:
            self._fail("browser_lost", "没有已验证的小红书专用浏览器。")
        if not 0.1 <= timeout <= 30.0:
            raise ValueError("timeout out of range")
        close_requested = self.normal_close(identity)
        deadline = self.monotonic() + timeout
        while close_requested and self.monotonic() < deadline:
            if self.process_probe(identity.chrome_pid, identity.chrome_birth) is None:
                receipt = self.registry.load_xhs_browser_receipt()
                if receipt is not None:
                    self.registry.delete_xhs_browser_receipt(receipt)
                profile = self.registry.resolve("xiaohongshu", "default")
                self._remove_discovery_after_death(profile.path)
                self.release_controller()
                return
            self.sleep(0.05)
        self._fail(
            "close_failed_browser_alive",
            "专用浏览器正常关闭失败，浏览器仍保持运行。",
        )

    def __enter__(self) -> XhsBrowserManager:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release_controller()


__all__ = [
    "CdpEndpoint",
    "ChromeInstallation",
    "ChromeProcess",
    "LaunchServicesSubmission",
    "XhsBrowserError",
    "XhsBrowserIdentity",
    "XhsBrowserManager",
]
