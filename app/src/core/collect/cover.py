"""封面安全本地化内核。

网络、DNS、时钟和取消均可注入。默认传输把已批准 IP 直接绑定到 TLS 连接，
同时保留原主机名的 SNI 与证书校验；文件提交只使用受管目录描述符相对操作。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import errno
import hashlib
import ipaddress
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
import zlib

from ..models import COVER_CDN_SUFFIXES


MAX_COVER_BYTES = 20 * 1024 * 1024
COVER_FAILURE_CODES = frozenset(
    {
        "cover_invalid_url",
        "cover_scheme_blocked",
        "cover_credentials_blocked",
        "cover_port_blocked",
        "cover_dns_failed",
        "cover_address_blocked",
        "cover_redirect_blocked",
        "cover_redirect_loop",
        "cover_redirect_limit",
        "cover_http_status",
        "cover_compression_blocked",
        "cover_too_large",
        "cover_truncated",
        "cover_type_mismatch",
        "cover_malformed",
        "cover_timeout",
        "cover_cancelled",
        "cover_path_blocked",
        "cover_busy",
        "cover_io_failed",
        "cover_commit_failed",
        "cover_commit_uncertain",
    }
)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_EXTENSIONS = ("jpg", "png", "webp")
_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ROLLBACK_RE = re.compile(
    r"^\.(?P<target>(?P<digest>[0-9a-f]{32})\.(?P<extension>jpg|png|webp))"
    r"\.(?P<sha256>[0-9a-f]{64})\.rollback$"
)
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_PROXY_CDN_SUFFIXES = COVER_CDN_SUFFIXES
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class _CoverFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CoverReceipt:
    status: str
    reason_code: str = ""
    relative_path: str = ""
    size: int = 0
    sha256: str = ""
    mime_type: str = ""

    @classmethod
    def failed(cls, code: str) -> "CoverReceipt":
        return cls(status="failed", reason_code=code)


@dataclass(frozen=True)
class CoverTarget:
    url: str
    host: str
    port: int
    request_target: str
    approved_ips: tuple[str, ...]
    proxy: "CoverProxyEndpoint | None" = None
    transparent_fake_ip: bool = False


@dataclass(frozen=True)
class CoverProxyEndpoint:
    host: str
    port: int


class CoverTransportResponse:
    """传输层稳定公共响应；只暴露原始字节、头和真实对端 IP。"""

    def __init__(
        self,
        *,
        status_code: int,
        headers: Sequence[tuple[str, str]],
        peer_ip: str,
        reader: Callable[[int], bytes],
        closer: Callable[[], None],
        timeout_setter: Callable[[float], None] | None = None,
        via_proxy: bool = False,
    ) -> None:
        self.status_code = int(status_code)
        self.headers = tuple((str(key), str(value)) for key, value in headers)
        self.peer_ip = str(peer_ip)
        self._reader = reader
        self._closer = closer
        self._timeout_setter = timeout_setter
        self.via_proxy = bool(via_proxy)

    def read(self, size: int) -> bytes:
        return self._reader(size)

    def set_read_timeout(self, timeout: float) -> None:
        if self._timeout_setter is not None:
            self._timeout_setter(timeout)

    def close(self) -> None:
        self._closer()


class PinnedHttpsTransport:
    """每次请求使用新连接；直连绑定批准 IP，受限代理绑定回环端点。"""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.context = context or ssl.create_default_context()

    def request(
        self,
        target: CoverTarget,
        *,
        connect_timeout: float,
        read_timeout: float,
        headers: Mapping[str, str],
    ) -> CoverTransportResponse:
        last_timeout = False
        request_deadline = time.monotonic() + max(connect_timeout, read_timeout)

        def stage_timeout(limit: float) -> float:
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise _CoverFailure("cover_timeout")
            return min(limit, remaining)

        addresses = (
            (target.proxy.host,)
            if target.proxy is not None
            else target.approved_ips
        )
        for address in addresses:
            raw: socket.socket | None = None
            tls: ssl.SSLSocket | None = None
            stream: Any | None = None
            try:
                raw = socket.create_connection(
                    (
                        address,
                        target.proxy.port if target.proxy is not None else target.port,
                    ),
                    stage_timeout(connect_timeout),
                )
                if target.proxy is not None:
                    peer = _loopback_proxy_address(str(raw.getpeername()[0]))
                    if peer != target.proxy.host:
                        raise _CoverFailure("cover_address_blocked")
                    raw.settimeout(stage_timeout(connect_timeout))
                    tunnel = raw.makefile("rb")
                    try:
                        authority = (
                            f"[{target.host}]:{target.port}"
                            if ":" in target.host
                            else f"{target.host}:{target.port}"
                        )
                        raw.sendall(
                            (
                                f"CONNECT {authority} HTTP/1.1\r\n"
                                f"Host: {authority}\r\n"
                                "Proxy-Connection: keep-alive\r\n\r\n"
                            ).encode("ascii")
                        )
                        status_line = tunnel.readline(8193)
                        if (
                            len(status_line) > 8192
                            or not re.fullmatch(
                                rb"HTTP/1\.[01] 200(?: [^\r\n]*)?\r\n",
                                status_line,
                            )
                        ):
                            raise _CoverFailure("cover_io_failed")
                        header_bytes = 0
                        header_count = 0
                        while True:
                            line = tunnel.readline(8193)
                            header_bytes += len(line)
                            if (
                                not line
                                or len(line) > 8192
                                or header_bytes > 64 * 1024
                                or header_count > 100
                            ):
                                raise _CoverFailure("cover_io_failed")
                            if line == b"\r\n":
                                break
                            if (
                                line[:1] in {b" ", b"\t"}
                                or not line.endswith(b"\r\n")
                                or b":" not in line
                            ):
                                raise _CoverFailure("cover_io_failed")
                            header_count += 1
                    finally:
                        tunnel.close()
                tls = self.context.wrap_socket(raw, server_hostname=target.host)
                raw = None
                peer_ip = str(tls.getpeername()[0])
                if target.proxy is not None:
                    if _loopback_proxy_address(peer_ip) != target.proxy.host:
                        raise _CoverFailure("cover_address_blocked")
                elif target.transparent_fake_ip:
                    if _fake_ip_address(peer_ip) not in target.approved_ips:
                        raise _CoverFailure("cover_address_blocked")
                elif _classify_address(peer_ip) not in target.approved_ips:
                    raise _CoverFailure("cover_address_blocked")
                tls.settimeout(stage_timeout(read_timeout))
                request_lines = [
                    f"GET {target.request_target} HTTP/1.1",
                    f"Host: {'[' + target.host + ']' if ':' in target.host else target.host}",
                ]
                request_lines.extend(f"{key}: {value}" for key, value in headers.items())
                payload = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")
                tls.sendall(payload)
                stream = tls.makefile("rb")
                tls.settimeout(stage_timeout(read_timeout))
                status_line = stream.readline(8193)
                if len(status_line) > 8192 or not status_line.endswith(b"\r\n"):
                    raise _CoverFailure("cover_io_failed")
                match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?\r\n", status_line)
                if match is None:
                    raise _CoverFailure("cover_io_failed")
                response_headers: list[tuple[str, str]] = []
                header_bytes = 0
                while True:
                    tls.settimeout(stage_timeout(read_timeout))
                    line = stream.readline(8193)
                    header_bytes += len(line)
                    if (
                        not line
                        or len(line) > 8192
                        or header_bytes > 64 * 1024
                        or len(response_headers) > 100
                    ):
                        raise _CoverFailure("cover_io_failed")
                    if line == b"\r\n":
                        break
                    if line[:1] in {b" ", b"\t"} or not line.endswith(b"\r\n") or b":" not in line:
                        raise _CoverFailure("cover_io_failed")
                    raw_name, raw_value = line[:-2].split(b":", 1)
                    try:
                        name = raw_name.decode("ascii")
                        value = raw_value.strip().decode("latin-1")
                    except UnicodeError as exc:
                        raise _CoverFailure("cover_io_failed") from exc
                    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(
                        ord(char) < 32 or ord(char) == 127 for char in value
                    ):
                        raise _CoverFailure("cover_io_failed")
                    response_headers.append((name, value))

                transfer_values = [
                    value.casefold()
                    for key, value in response_headers
                    if key.casefold() == "transfer-encoding"
                ]
                length_values = [
                    value
                    for key, value in response_headers
                    if key.casefold() == "content-length"
                ]
                if transfer_values and length_values:
                    raise _CoverFailure("cover_truncated")
                if len(transfer_values) > 1 or (
                    transfer_values and transfer_values[0] != "chunked"
                ):
                    raise _CoverFailure("cover_truncated")
                parsed_length: int | None = None
                if len(length_values) == 1:
                    raw_length = length_values[0]
                    if not re.fullmatch(r"0|[1-9][0-9]*", raw_length):
                        raise _CoverFailure("cover_truncated")
                    if len(raw_length) > 20:
                        raise _CoverFailure("cover_too_large")
                    parsed_length = int(raw_length)
                reader = _BoundBodyReader(
                    stream,
                    tls,
                    chunked=bool(transfer_values),
                    content_length=parsed_length,
                    content_length_present=bool(length_values),
                )
                return CoverTransportResponse(
                    status_code=int(match.group(1)),
                    headers=response_headers,
                    peer_ip=peer_ip,
                    reader=reader.read,
                    closer=reader.close,
                    timeout_setter=reader.set_timeout,
                    via_proxy=target.proxy is not None,
                )
            except (socket.timeout, TimeoutError):
                last_timeout = True
            except _CoverFailure:
                if stream is not None:
                    stream.close()
                if tls is not None:
                    tls.close()
                if raw is not None:
                    raw.close()
                raise
            except BaseException:
                pass
            if stream is not None:
                stream.close()
            if tls is not None:
                tls.close()
            if raw is not None:
                raw.close()
        raise _CoverFailure("cover_timeout" if last_timeout else "cover_io_failed")


class _BoundBodyReader:
    def __init__(
        self,
        stream: Any,
        tls: ssl.SSLSocket,
        *,
        chunked: bool,
        content_length: int | None,
        content_length_present: bool,
    ) -> None:
        if content_length_present and content_length is None:
            raise _CoverFailure("cover_truncated")
        self.stream = stream
        self.tls = tls
        self.chunked = chunked
        self.remaining = content_length
        self.chunk_remaining = 0
        self.finished = False
        self.operation_deadline: float | None = None

    def set_timeout(self, timeout: float) -> None:
        if timeout <= 0:
            raise _CoverFailure("cover_timeout")
        self.operation_deadline = time.monotonic() + timeout
        self.tls.settimeout(timeout)

    def _arm_timeout(self) -> None:
        if self.operation_deadline is None:
            return
        remaining = self.operation_deadline - time.monotonic()
        if remaining <= 0:
            raise _CoverFailure("cover_timeout")
        self.tls.settimeout(remaining)

    def _read_exact(self, size: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            self._arm_timeout()
            chunk = self.stream.read(size - len(output))
            if not chunk:
                raise _CoverFailure("cover_truncated")
            output.extend(chunk)
        return bytes(output)

    def read(self, size: int) -> bytes:
        if self.finished:
            return b""
        if self.chunked:
            while self.chunk_remaining == 0:
                self._arm_timeout()
                line = self.stream.readline(129)
                if not line or len(line) > 128 or not line.endswith(b"\r\n"):
                    raise _CoverFailure("cover_truncated")
                token = line[:-2].split(b";", 1)[0]
                if not token or not re.fullmatch(rb"[0-9A-Fa-f]+", token):
                    raise _CoverFailure("cover_truncated")
                self.chunk_remaining = int(token, 16)
                if self.chunk_remaining == 0:
                    trailer_bytes = 0
                    trailer_count = 0
                    while True:
                        self._arm_timeout()
                        trailer = self.stream.readline(8193)
                        trailer_bytes += len(trailer)
                        trailer_count += 1
                        if (
                            not trailer
                            or len(trailer) > 8192
                            or trailer_bytes > 64 * 1024
                            or trailer_count > 100
                        ):
                            raise _CoverFailure("cover_truncated")
                        if trailer == b"\r\n":
                            break
                        if b":" not in trailer or trailer[:1] in {b" ", b"\t"}:
                            raise _CoverFailure("cover_truncated")
                    self.finished = True
                    return b""
            amount = min(size, self.chunk_remaining)
            output = self._read_exact(amount)
            self.chunk_remaining -= amount
            if self.chunk_remaining == 0 and self._read_exact(2) != b"\r\n":
                raise _CoverFailure("cover_truncated")
            return output
        if self.remaining is not None:
            if self.remaining == 0:
                self._arm_timeout()
                extra = self.stream.read(1)
                self.finished = True
                return extra
            self._arm_timeout()
            output = self.stream.read(min(size, self.remaining))
            if not output:
                raise _CoverFailure("cover_truncated")
            self.remaining -= len(output)
            return output
        self._arm_timeout()
        output = self.stream.read(size)
        if not output:
            self.finished = True
        return output

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self.tls.close()


@dataclass
class _ManagedDirectory:
    root_path: Path
    root_parent_fd: int
    root_fd: int
    root_name: str
    platform_fd: int
    platform: str
    root_identity: tuple[int, int]
    platform_identity: tuple[int, int]

    def close(self) -> None:
        for descriptor in (self.platform_fd, self.root_fd, self.root_parent_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True)
class _OwnedFile:
    name: str
    identity: tuple[int, int]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _raise_path_error(exc: BaseException) -> None:
    if isinstance(exc, OSError) and exc.errno in {
        errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.EEXIST,
    }:
        raise _CoverFailure("cover_path_blocked") from exc
    raise exc


def _open_or_create_dir(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            # 另一个受控下载可能刚创建了同一目录。仍用 O_NOFOLLOW |
            # O_DIRECTORY 重新打开并在上层核对 inode；符号链接或替换会失败关闭。
            pass
        except BaseException as exc:
            _raise_path_error(exc)
            raise
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except BaseException as exc:
            _raise_path_error(exc)
            raise
    except BaseException as exc:
        _raise_path_error(exc)
        raise


def _open_managed_directory(root: Path, platform: str) -> _ManagedDirectory:
    absolute = root.absolute()
    if absolute == Path(absolute.anchor):
        raise _CoverFailure("cover_path_blocked")
    parts = absolute.parts[1:]
    current_fd = os.open(absolute.anchor, _DIR_FLAGS)
    root_parent_fd = -1
    root_fd = -1
    try:
        for index, component in enumerate(parts):
            child_fd = _open_or_create_dir(current_fd, component)
            if index == len(parts) - 1:
                root_parent_fd = current_fd
                root_fd = child_fd
                break
            os.close(current_fd)
            current_fd = child_fd
        if root_fd < 0:
            raise _CoverFailure("cover_path_blocked")
        platform_fd = _open_or_create_dir(root_fd, platform)
        root_info = os.fstat(root_fd)
        platform_info = os.fstat(platform_fd)
        if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(platform_info.st_mode):
            raise _CoverFailure("cover_path_blocked")
        managed = _ManagedDirectory(
            absolute,
            root_parent_fd,
            root_fd,
            parts[-1],
            platform_fd,
            platform,
            _identity(root_info),
            _identity(platform_info),
        )
        _verify_directory(managed)
        return managed
    except BaseException:
        for descriptor in (root_fd, root_parent_fd, current_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _open_existing_managed_directory(
    root: Path, platform: str
) -> _ManagedDirectory | None:
    """Open an already-existing managed directory without creating anything."""

    absolute = root.absolute()
    if absolute == Path(absolute.anchor):
        raise _CoverFailure("cover_path_blocked")
    current_fd = os.open(absolute.anchor, _DIR_FLAGS)
    root_parent_fd = -1
    root_fd = -1
    try:
        for index, component in enumerate(absolute.parts[1:]):
            try:
                child_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                os.close(current_fd)
                return None
            except BaseException as exc:
                _raise_path_error(exc)
                raise
            if index == len(absolute.parts[1:]) - 1:
                root_parent_fd = current_fd
                root_fd = child_fd
                break
            os.close(current_fd)
            current_fd = child_fd
        if root_fd < 0:
            raise _CoverFailure("cover_path_blocked")
        try:
            platform_fd = os.open(platform, _DIR_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            os.close(root_fd)
            os.close(root_parent_fd)
            return None
        except BaseException as exc:
            _raise_path_error(exc)
            raise
        root_info = os.fstat(root_fd)
        platform_info = os.fstat(platform_fd)
        managed = _ManagedDirectory(
            absolute,
            root_parent_fd,
            root_fd,
            absolute.parts[-1],
            platform_fd,
            platform,
            _identity(root_info),
            _identity(platform_info),
        )
        _verify_directory(managed)
        return managed
    except BaseException:
        for descriptor in (root_fd, root_parent_fd, current_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _preflight_existing_path(root: Path, platform: str, lock_name: str) -> None:
    absolute = root.absolute()
    if absolute == Path(absolute.anchor):
        raise _CoverFailure("cover_path_blocked")
    current_fd = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                return
            except BaseException as exc:
                _raise_path_error(exc)
                raise
            os.close(current_fd)
            current_fd = child_fd
        try:
            platform_fd = os.open(platform, _DIR_FLAGS, dir_fd=current_fd)
        except FileNotFoundError:
            return
        except BaseException as exc:
            _raise_path_error(exc)
            raise
        try:
            info = os.stat(lock_name, dir_fd=platform_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        finally:
            os.close(platform_fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            raise _CoverFailure("cover_busy")
        raise _CoverFailure("cover_path_blocked")
    finally:
        os.close(current_fd)


def _list_existing_platforms(root: Path) -> tuple[str, ...]:
    """List only valid managed platform directories without creating paths."""

    absolute = root.absolute()
    if absolute == Path(absolute.anchor):
        raise _CoverFailure("cover_path_blocked")
    current_fd = os.open(absolute.anchor, _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                return ()
            except BaseException as exc:
                _raise_path_error(exc)
                raise
            os.close(current_fd)
            current_fd = child_fd
        platforms: list[str] = []
        for name in os.listdir(current_fd):
            if not _PLATFORM_RE.fullmatch(name):
                continue
            try:
                info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except BaseException as exc:
                raise _CoverFailure("cover_path_blocked") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise _CoverFailure("cover_path_blocked")
            platforms.append(name)
        return tuple(sorted(platforms))
    finally:
        os.close(current_fd)


def _verify_directory(managed: _ManagedDirectory) -> None:
    try:
        root_entry = os.stat(
            managed.root_name, dir_fd=managed.root_parent_fd, follow_symlinks=False
        )
        platform_entry = os.stat(
            managed.platform, dir_fd=managed.root_fd, follow_symlinks=False
        )
    except BaseException as exc:
        raise _CoverFailure("cover_path_blocked") from exc
    if (
        not stat.S_ISDIR(root_entry.st_mode)
        or not stat.S_ISDIR(platform_entry.st_mode)
        or _identity(root_entry) != managed.root_identity
        or _identity(platform_entry) != managed.platform_identity
        or _identity(os.fstat(managed.root_fd)) != managed.root_identity
        or _identity(os.fstat(managed.platform_fd)) != managed.platform_identity
    ):
        raise _CoverFailure("cover_path_blocked")


def _create_owned(directory_fd: int, prefix: str, suffix: str) -> tuple[_OwnedFile, int]:
    for _attempt in range(32):
        name = f"{prefix}{secrets.token_hex(8)}{suffix}"
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise _CoverFailure("cover_path_blocked")
        return _OwnedFile(name, _identity(info)), descriptor
    raise _CoverFailure("cover_busy")


def _create_named_owned(directory_fd: int, name: str) -> tuple[_OwnedFile, int]:
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
    except FileExistsError as exc:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as stat_exc:
            raise _CoverFailure("cover_path_blocked") from stat_exc
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            raise _CoverFailure("cover_busy") from exc
        raise _CoverFailure("cover_path_blocked") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise _CoverFailure("cover_path_blocked")
    return _OwnedFile(name, _identity(info)), descriptor


def _verify_owned(directory_fd: int, owned: _OwnedFile) -> None:
    try:
        info = os.stat(owned.name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise _CoverFailure("cover_path_blocked") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or _identity(info) != owned.identity
    ):
        raise _CoverFailure("cover_path_blocked")


def _unlink_owned(directory_fd: int, owned: _OwnedFile) -> None:
    try:
        _verify_owned(directory_fd, owned)
        os.unlink(owned.name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _unlink_owned_and_sync(
    managed: _ManagedDirectory,
    owned: _OwnedFile,
) -> None:
    """Delete one owned sidecar and durably close its directory entry."""

    root_info = os.fstat(managed.root_fd)
    platform_info = os.fstat(managed.platform_fd)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or not stat.S_ISDIR(platform_info.st_mode)
        or _identity(root_info) != managed.root_identity
        or _identity(platform_info) != managed.platform_identity
    ):
        raise _CoverFailure("cover_path_blocked")
    _verify_owned(managed.platform_fd, owned)
    os.unlink(owned.name, dir_fd=managed.platform_fd)
    os.fsync(managed.platform_fd)
    root_info = os.fstat(managed.root_fd)
    platform_info = os.fstat(managed.platform_fd)
    if (
        _identity(root_info) != managed.root_identity
        or _identity(platform_info) != managed.platform_identity
    ):
        raise _CoverFailure("cover_path_blocked")


def _entry_info(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _CoverFailure("cover_path_blocked")
    return info


def _read_entry(directory_fd: int, name: str, max_bytes: int) -> bytes:
    before = _entry_info(directory_fd, name)
    if before is None:
        raise FileNotFoundError(name)
    descriptor = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before) or opened.st_nlink != 1:
            raise _CoverFailure("cover_path_blocked")
        output = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_bytes:
                raise _CoverFailure("cover_path_blocked")
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise _CoverFailure("cover_path_blocked")
        return bytes(output)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "write failed")
        view = view[written:]


def _rewrite_or_create_entry(directory_fd: int, name: str, payload: bytes) -> None:
    """Restore one target without consuming the last rollback copy.

    Existing entries are opened without O_TRUNC, checked by inode first, and
    only then rewritten.  Missing entries are created exclusively.  The caller
    keeps the independent rollback sidecar until the restored path is durable.
    """

    before = _entry_info(directory_fd, name)
    flags = os.O_RDWR | _FILE_NOFOLLOW
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except BaseException as exc:
        _raise_path_error(exc)
        raise
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before is not None and _identity(opened) != _identity(before))
        ):
            raise _CoverFailure("cover_path_blocked")
        owned = _OwnedFile(name, _identity(opened))
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if _identity(os.fstat(descriptor)) != owned.identity:
            raise _CoverFailure("cover_path_blocked")
    finally:
        os.close(descriptor)
    _verify_owned(directory_fd, owned)
    if _read_entry(directory_fd, name, len(payload)) != payload:
        raise _CoverFailure("cover_commit_failed")


def _rollback_name(target_name: str, payload: bytes) -> str:
    return f".{target_name}.{hashlib.sha256(payload).hexdigest()}.rollback"


def _read_valid_rollback(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
) -> tuple[str, str, str, bytes, str]:
    match = _ROLLBACK_RE.fullmatch(name)
    if match is None:
        raise _CoverFailure("cover_commit_uncertain")
    info = _entry_info(directory_fd, name)
    if info is None or stat.S_IMODE(info.st_mode) != 0o600:
        raise _CoverFailure("cover_commit_uncertain")
    payload = _read_entry(directory_fd, name, max_bytes)
    expected_hash = match.group("sha256")
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise _CoverFailure("cover_commit_uncertain")
    mime, extension = _validate_image(payload)
    if extension != match.group("extension"):
        raise _CoverFailure("cover_commit_uncertain")
    return (
        match.group("target"),
        match.group("digest"),
        expected_hash,
        payload,
        mime,
    )


def _sync_and_verify_entry(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mime: str,
) -> None:
    before = _entry_info(directory_fd, name)
    if before is None:
        raise _CoverFailure("cover_commit_uncertain")
    descriptor = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before) or opened.st_nlink != 1:
            raise _CoverFailure("cover_path_blocked")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    current = _read_entry(directory_fd, name, len(payload))
    current_mime, _extension = _validate_image(current)
    if current != payload or current_mime != mime:
        raise _CoverFailure("cover_commit_uncertain")


def _ensure_exact_rollback(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    max_bytes: int,
) -> None:
    """Ensure the one deterministic rollback exists with its exact old bytes."""

    info = _entry_info(directory_fd, name)
    if info is not None:
        _read_valid_rollback(directory_fd, name, max_bytes=max_bytes)
        return
    owned, descriptor = _create_named_owned(directory_fd, name)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _verify_owned(directory_fd, owned)
    _read_valid_rollback(directory_fd, name, max_bytes=max_bytes)
    os.fsync(directory_fd)


def _single_header(
    headers: Sequence[tuple[str, str]], name: str, duplicate_code: str
) -> str:
    values = [value.strip() for key, value in headers if key.casefold() == name.casefold()]
    if len(values) > 1:
        raise _CoverFailure(duplicate_code)
    return values[0] if values else ""


def _normalize_url(raw: str) -> tuple[str, str, int, str]:
    value = str(raw or "")
    if not value or "\\" in value or _BAD_PERCENT.search(value):
        raise _CoverFailure("cover_invalid_url")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise _CoverFailure("cover_invalid_url")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise _CoverFailure("cover_invalid_url") from exc
    if parts.scheme.casefold() != "https":
        raise _CoverFailure("cover_scheme_blocked")
    if parts.username is not None or parts.password is not None:
        raise _CoverFailure("cover_credentials_blocked")
    if not host:
        raise _CoverFailure("cover_invalid_url")
    if port not in (None, 443):
        raise _CoverFailure("cover_port_blocked")
    try:
        normalized_host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise _CoverFailure("cover_invalid_url") from exc
    labels = normalized_host.split(".")
    if (
        not normalized_host
        or any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" for label in labels)
        or len(normalized_host) > 253
        or "%" in normalized_host
        or normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
    ):
        raise _CoverFailure("cover_invalid_url")
    netloc = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    path = parts.path or "/"
    normalized = urlunsplit(("https", netloc, path, parts.query, ""))
    request_target = path + (f"?{parts.query}" if parts.query else "")
    try:
        request_target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _CoverFailure("cover_invalid_url") from exc
    return normalized, normalized_host, 443, request_target


def _classify_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _CoverFailure("cover_dns_failed") from exc
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise _CoverFailure("cover_address_blocked")
    return str(address)


def _loopback_proxy_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _CoverFailure("cover_address_blocked") from exc
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if not address.is_loopback:
        raise _CoverFailure("cover_address_blocked")
    return str(address)


def _is_fake_ip_answer(value: str) -> bool:
    try:
        _fake_ip_address(value)
    except _CoverFailure:
        return False
    return True


def _fake_ip_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _CoverFailure("cover_address_blocked") from exc
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if not (
        isinstance(address, ipaddress.IPv4Address)
        and address in _FAKE_IP_NETWORK
    ):
        raise _CoverFailure("cover_address_blocked")
    return str(address)


def _proxy_host_allowed(platform: str, host: str) -> bool:
    normalized = str(host or "").casefold().rstrip(".")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in _PROXY_CDN_SUFFIXES.get(platform, ())
    )


def _parse_macos_proxy(output: str) -> CoverProxyEndpoint | None:
    values: dict[str, str] = {}
    for line in str(output or "").splitlines():
        match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*?)\s*", line)
        if match is not None:
            values[match.group(1)] = match.group(2)
    for prefix in ("HTTPS", "HTTP"):
        if values.get(f"{prefix}Enable") != "1":
            continue
        host = values.get(f"{prefix}Proxy", "")
        port_text = values.get(f"{prefix}Port", "")
        try:
            normalized_host = _loopback_proxy_address(host)
            port = int(port_text)
        except (TypeError, ValueError, _CoverFailure):
            continue
        if 1 <= port <= 65535:
            return CoverProxyEndpoint(normalized_host, port)
    return None


def _macos_system_https_proxy() -> CoverProxyEndpoint | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ("/usr/sbin/scutil", "--proxy"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.5,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_macos_proxy(result.stdout)


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


def _resolve_with_deadline(
    resolver: Callable[[str, int], Iterable[str]],
    host: str,
    port: int,
    *,
    remaining: Callable[[], float],
    cancel: Callable[[], bool],
) -> tuple[str, ...]:
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, tuple(resolver(host, port))))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=run, daemon=True).start()
    while True:
        if cancel():
            raise _CoverFailure("cover_cancelled")
        timeout = remaining()
        if timeout <= 0:
            raise _CoverFailure("cover_timeout")
        try:
            ok, value = result.get(timeout=min(0.05, timeout))
        except queue.Empty:
            continue
        if not ok:
            raise _CoverFailure("cover_dns_failed") from value
        if not value:
            raise _CoverFailure("cover_dns_failed")
        return tuple(str(address) for address in value)


def _validate_image(payload: bytes) -> tuple[str, str]:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        saw_ihdr = saw_idat = saw_iend = False
        idat_ended = False
        while position < len(payload):
            if position + 12 > len(payload):
                raise _CoverFailure("cover_truncated")
            length = int.from_bytes(payload[position:position + 4], "big")
            chunk_type = payload[position + 4:position + 8]
            end = position + 12 + length
            if end > len(payload):
                raise _CoverFailure("cover_truncated")
            data = payload[position + 8:position + 8 + length]
            if not re.fullmatch(rb"[A-Za-z]{4}", chunk_type):
                raise _CoverFailure("cover_malformed")
            expected_crc = int.from_bytes(payload[position + 8 + length:end], "big")
            actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                raise _CoverFailure("cover_malformed")
            if position == 8:
                if chunk_type != b"IHDR" or length != 13:
                    raise _CoverFailure("cover_malformed")
                width = int.from_bytes(data[0:4], "big")
                height = int.from_bytes(data[4:8], "big")
                bit_depth = data[8]
                color_type = data[9]
                valid_depths = {
                    0: {1, 2, 4, 8, 16},
                    2: {8, 16},
                    3: {1, 2, 4, 8},
                    4: {8, 16},
                    6: {8, 16},
                }
                if (
                    width == 0
                    or height == 0
                    or bit_depth not in valid_depths.get(color_type, set())
                    or data[10] != 0
                    or data[11] != 0
                    or data[12] not in {0, 1}
                ):
                    raise _CoverFailure("cover_malformed")
                saw_ihdr = True
            elif chunk_type == b"IHDR":
                raise _CoverFailure("cover_malformed")
            if chunk_type in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
                pass
            elif chunk_type[:1].isupper():
                raise _CoverFailure("cover_malformed")
            if chunk_type == b"PLTE" and (saw_idat or length == 0 or length % 3):
                raise _CoverFailure("cover_malformed")
            if chunk_type == b"IDAT":
                if idat_ended:
                    raise _CoverFailure("cover_malformed")
                saw_idat = True
            elif saw_idat and chunk_type != b"IEND":
                idat_ended = True
            if chunk_type == b"IEND":
                if length != 0 or end != len(payload):
                    raise _CoverFailure("cover_malformed")
                saw_iend = True
            position = end
        if not (saw_ihdr and saw_idat and saw_iend):
            raise _CoverFailure("cover_malformed")
        return "image/png", "png"

    if payload.startswith(b"RIFF") and len(payload) >= 12 and payload[8:12] == b"WEBP":
        if int.from_bytes(payload[4:8], "little") + 8 != len(payload):
            raise _CoverFailure("cover_truncated")
        position = 12
        saw_extended = False
        image_chunks = 0
        while position < len(payload):
            if position + 8 > len(payload):
                raise _CoverFailure("cover_truncated")
            kind = payload[position:position + 4]
            length = int.from_bytes(payload[position + 4:position + 8], "little")
            end = position + 8 + length + (length & 1)
            if end > len(payload):
                raise _CoverFailure("cover_truncated")
            data = payload[position + 8:position + 8 + length]
            if kind == b"VP8X":
                if position != 12 or saw_extended or length != 10:
                    raise _CoverFailure("cover_malformed")
                saw_extended = True
            elif kind == b"VP8 ":
                if length < 10 or data[3:6] != b"\x9d\x01\x2a":
                    raise _CoverFailure("cover_malformed")
                image_chunks += 1
            elif kind == b"VP8L":
                if length < 5 or data[:1] != b"\x2f":
                    raise _CoverFailure("cover_malformed")
                image_chunks += 1
            elif kind in {b"ANIM", b"ANMF"}:
                # 封面内核首版仅接纳静态 WebP，避免未完整解析帧内容。
                raise _CoverFailure("cover_malformed")
            position = end
        if position != len(payload) or image_chunks != 1:
            raise _CoverFailure("cover_malformed")
        return "image/webp", "webp"

    if payload.startswith(b"\xff\xd8"):
        position = 2
        saw_frame = saw_scan = False
        in_scan = False
        while position < len(payload):
            if in_scan:
                marker_at = payload.find(b"\xff", position)
                if marker_at < 0 or marker_at + 1 >= len(payload):
                    raise _CoverFailure("cover_truncated")
                marker = payload[marker_at + 1]
                if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                    position = marker_at + 2
                    continue
                position = marker_at
                in_scan = False
                continue
            if position + 2 > len(payload) or payload[position] != 0xFF:
                raise _CoverFailure("cover_malformed")
            while position < len(payload) and payload[position] == 0xFF:
                position += 1
            if position >= len(payload):
                raise _CoverFailure("cover_truncated")
            marker = payload[position]
            position += 1
            if marker == 0xD9:
                if position != len(payload) or not (saw_frame and saw_scan):
                    raise _CoverFailure("cover_malformed")
                return "image/jpeg", "jpg"
            if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(payload):
                raise _CoverFailure("cover_truncated")
            length = int.from_bytes(payload[position:position + 2], "big")
            if length < 2 or position + length > len(payload):
                raise _CoverFailure("cover_truncated")
            if marker in {0xC0, 0xC1, 0xC2}:
                saw_frame = True
            if marker == 0xDA:
                saw_scan = True
                in_scan = True
            position += length
        raise _CoverFailure("cover_truncated")
    raise _CoverFailure("cover_type_mismatch")


def _error_code(exc: BaseException, fallback: str = "cover_io_failed") -> str:
    if isinstance(exc, _CoverFailure):
        return exc.code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "cover_timeout"
    if isinstance(exc, OSError) and exc.errno in {
        errno.ELOOP, errno.ENOTDIR, errno.EISDIR,
    }:
        return "cover_path_blocked"
    return fallback


class CoverLocalizationService:
    """将封面写入构造时固定的受管根；失败只返回稳定、脱敏收据。"""

    def __init__(
        self,
        managed_root: Path | str,
        *,
        transport: Any | None = None,
        resolver: Callable[[str, int], Iterable[str]] | None = None,
        proxy_provider: Callable[[], CoverProxyEndpoint | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_bytes: int = MAX_COVER_BYTES,
        max_redirects: int = 3,
    ) -> None:
        self.root = Path(managed_root).expanduser().absolute()
        self.transport = transport or PinnedHttpsTransport()
        self.resolver = resolver or _system_resolver
        self.proxy_provider = proxy_provider or _macos_system_https_proxy
        self.clock = clock
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.total_timeout = float(total_timeout)
        requested_max_bytes = int(max_bytes)
        self.max_bytes = min(requested_max_bytes, MAX_COVER_BYTES)
        self.max_redirects = int(max_redirects)
        if min(self.connect_timeout, self.read_timeout, self.total_timeout) <= 0:
            raise ValueError("timeout must be positive")
        if requested_max_bytes <= 0 or self.max_redirects < 0:
            raise ValueError("download limits are invalid")
        self._recent_recoveries: dict[tuple[str, str], CoverReceipt] = {}
        self.startup_recovery_receipts = self.recover_pending()

    def recover_pending(self) -> tuple[CoverReceipt, ...]:
        """Recover every recognized rollback without DNS or network access."""

        recovered: list[CoverReceipt] = []
        try:
            platforms = _list_existing_platforms(self.root)
        except BaseException:
            return (CoverReceipt.failed("cover_commit_uncertain"),)
        for platform in platforms:
            for digest, receipt in self._recover_platform(platform):
                recovered.append(receipt)
                if digest is not None:
                    self._recent_recoveries[(platform, digest)] = receipt
        return tuple(recovered)

    def _recover_platform(
        self,
        platform: str,
        *,
        only_digest: str | None = None,
    ) -> tuple[tuple[str | None, CoverReceipt], ...]:
        managed: _ManagedDirectory | None = None
        try:
            managed = _open_existing_managed_directory(self.root, platform)
            if managed is None:
                return ()
            rollback_names = sorted(
                name for name in os.listdir(managed.platform_fd)
                if name.endswith(".rollback")
            )
            relevant: list[str] = []
            malformed: list[str] = []
            for name in rollback_names:
                match = _ROLLBACK_RE.fullmatch(name)
                if match is None:
                    if only_digest is None or name.startswith(f".{only_digest}."):
                        malformed.append(name)
                    continue
                if only_digest is None or match.group("digest") == only_digest:
                    relevant.append(name)
            results: list[tuple[str | None, CoverReceipt]] = []
            if malformed:
                results.append(
                    (only_digest, CoverReceipt.failed("cover_commit_uncertain"))
                )
                if only_digest is not None:
                    return tuple(results)

            grouped: dict[str, list[str]] = {}
            for name in relevant:
                match = _ROLLBACK_RE.fullmatch(name)
                if match is not None:
                    grouped.setdefault(match.group("digest"), []).append(name)
            for digest, names in sorted(grouped.items()):
                if len(names) != 1:
                    results.append(
                        (digest, CoverReceipt.failed("cover_commit_uncertain"))
                    )
                    continue
                results.append(
                    (digest, self._recover_one(managed, names[0], digest))
                )
            return tuple(results)
        except BaseException as exc:
            if (
                only_digest is not None
                and isinstance(exc, _CoverFailure)
                and exc.code == "cover_path_blocked"
            ):
                return ()
            return (
                (only_digest, CoverReceipt.failed("cover_commit_uncertain")),
            )
        finally:
            if managed is not None:
                managed.close()

    def _recover_one(
        self,
        managed: _ManagedDirectory,
        rollback_name: str,
        digest: str,
    ) -> CoverReceipt:
        lock: _OwnedFile | None = None
        candidate: _OwnedFile | None = None
        rollback_payload: bytes | None = None
        rollback_target = ""
        rollback_mime = ""
        try:
            (
                rollback_target,
                parsed_digest,
                rollback_hash,
                rollback_payload,
                rollback_mime,
            ) = _read_valid_rollback(
                managed.platform_fd, rollback_name, max_bytes=self.max_bytes
            )
            if parsed_digest != digest:
                raise _CoverFailure("cover_commit_uncertain")
            lock, lock_fd = _create_named_owned(
                managed.platform_fd, f".{digest}.lock"
            )
            os.close(lock_fd)
            _verify_directory(managed)
            _verify_owned(managed.platform_fd, lock)
            (
                checked_target,
                checked_digest,
                checked_hash,
                checked_payload,
                checked_mime,
            ) = _read_valid_rollback(
                managed.platform_fd, rollback_name, max_bytes=self.max_bytes
            )
            if (
                checked_target != rollback_target
                or checked_digest != digest
                or checked_hash != rollback_hash
                or checked_payload != rollback_payload
                or checked_mime != rollback_mime
            ):
                raise _CoverFailure("cover_commit_uncertain")

            current = None
            if _entry_info(managed.platform_fd, rollback_target) is not None:
                current = _read_entry(
                    managed.platform_fd, rollback_target, self.max_bytes
                )
            if current != rollback_payload:
                candidate, descriptor = _create_owned(
                    managed.platform_fd, f".{digest}.", ".restore"
                )
                try:
                    _write_all(descriptor, rollback_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _verify_owned(managed.platform_fd, candidate)
                os.replace(
                    candidate.name,
                    rollback_target,
                    src_dir_fd=managed.platform_fd,
                    dst_dir_fd=managed.platform_fd,
                )
                candidate = None
            _sync_and_verify_entry(
                managed.platform_fd,
                rollback_target,
                rollback_payload,
                mime=rollback_mime,
            )
            os.fsync(managed.platform_fd)
            _verify_directory(managed)
            rollback_info = _entry_info(managed.platform_fd, rollback_name)
            if rollback_info is None:
                raise _CoverFailure("cover_commit_uncertain")
            rollback_owned = _OwnedFile(rollback_name, _identity(rollback_info))
            _unlink_owned(managed.platform_fd, rollback_owned)
            try:
                os.fsync(managed.platform_fd)
            except BaseException:
                _ensure_exact_rollback(
                    managed.platform_fd,
                    rollback_name,
                    rollback_payload,
                    max_bytes=self.max_bytes,
                )
                raise
            _verify_directory(managed)
            _unlink_owned(managed.platform_fd, lock)
            lock = None
            try:
                os.fsync(managed.platform_fd)
            except BaseException:
                _ensure_exact_rollback(
                    managed.platform_fd,
                    rollback_name,
                    rollback_payload,
                    max_bytes=self.max_bytes,
                )
                raise
            _verify_directory(managed)
            return CoverReceipt(
                "recovered_previous",
                relative_path=f"{managed.platform}/{rollback_target}",
                size=len(rollback_payload),
                sha256=rollback_hash,
                mime_type=rollback_mime,
            )
        except BaseException as exc:
            if rollback_payload is not None and rollback_target:
                try:
                    _ensure_exact_rollback(
                        managed.platform_fd,
                        rollback_name,
                        rollback_payload,
                        max_bytes=self.max_bytes,
                    )
                except BaseException:
                    pass
            return CoverReceipt.failed(
                "cover_busy"
                if isinstance(exc, _CoverFailure) and exc.code == "cover_busy"
                else "cover_commit_uncertain"
            )
        finally:
            if candidate is not None:
                try:
                    _unlink_owned(managed.platform_fd, candidate)
                except BaseException:
                    pass
            if lock is not None:
                try:
                    _unlink_owned(managed.platform_fd, lock)
                    os.fsync(managed.platform_fd)
                except BaseException:
                    pass

    def localize(
        self,
        *,
        platform: str,
        work_id: str,
        url: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CoverReceipt:
        cancel = is_cancelled or (lambda: False)
        deadline = self.clock() + self.total_timeout
        managed: _ManagedDirectory | None = None
        lock: _OwnedFile | None = None
        response: CoverTransportResponse | None = None
        temp: _OwnedFile | None = None
        primary_receipt: CoverReceipt | None = None

        def remaining() -> float:
            return deadline - self.clock()

        def check_stop() -> None:
            if cancel():
                raise _CoverFailure("cover_cancelled")
            if remaining() <= 0:
                raise _CoverFailure("cover_timeout")

        try:
            check_stop()
            platform_key = str(platform or "").casefold()
            work_key = str(work_id or "").strip()
            if not _PLATFORM_RE.fullmatch(platform_key) or not work_key:
                raise _CoverFailure("cover_invalid_url")
            digest = hashlib.sha256(
                f"{platform_key}:{work_key}".encode("utf-8")
            ).hexdigest()[:32]
            startup_receipt = self._recent_recoveries.pop(
                (platform_key, digest), None
            )
            if startup_receipt is not None:
                return startup_receipt
            recovery = self._recover_platform(
                platform_key, only_digest=digest
            )
            if recovery:
                return recovery[0][1]
            _preflight_existing_path(
                self.root, platform_key, f".{digest}.lock"
            )
            current = str(url)
            visited: set[str] = set()
            restricted_route_bound = False
            for redirect_count in range(self.max_redirects + 1):
                check_stop()
                normalized, host, port, request_target = _normalize_url(current)
                if normalized in visited:
                    raise _CoverFailure("cover_redirect_loop")
                visited.add(normalized)
                if (
                    restricted_route_bound
                    and not _proxy_host_allowed(platform_key, host)
                ):
                    raise _CoverFailure("cover_redirect_blocked")
                try:
                    literal = ipaddress.ip_address(host)
                except ValueError:
                    addresses = _resolve_with_deadline(
                        self.resolver, host, port, remaining=remaining, cancel=cancel
                    )
                else:
                    addresses = (str(literal),)
                proxy: CoverProxyEndpoint | None = None
                transparent_fake_ip = False
                if addresses and all(_is_fake_ip_answer(value) for value in addresses):
                    if not _proxy_host_allowed(platform_key, host):
                        raise _CoverFailure("cover_address_blocked")
                    proxy = self.proxy_provider()
                    restricted_route_bound = True
                    if proxy is not None:
                        approved: tuple[str, ...] = ()
                    else:
                        transparent_fake_ip = True
                        approved = tuple(
                            dict.fromkeys(
                                _fake_ip_address(value) for value in addresses
                            )
                        )
                else:
                    approved = tuple(
                        dict.fromkeys(_classify_address(value) for value in addresses)
                    )
                target = CoverTarget(
                    normalized,
                    host,
                    port,
                    request_target,
                    approved,
                    proxy,
                    transparent_fake_ip,
                )
                check_stop()
                response = self.transport.request(
                    target,
                    connect_timeout=min(self.connect_timeout, remaining()),
                    read_timeout=min(self.read_timeout, remaining()),
                    headers={
                        "Accept": "image/jpeg,image/png,image/webp",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                if proxy is not None:
                    if (
                        not response.via_proxy
                        or _loopback_proxy_address(response.peer_ip) != proxy.host
                    ):
                        raise _CoverFailure("cover_address_blocked")
                else:
                    peer = (
                        _fake_ip_address(response.peer_ip)
                        if transparent_fake_ip
                        else _classify_address(response.peer_ip)
                    )
                    if response.via_proxy or peer not in approved:
                        raise _CoverFailure("cover_address_blocked")
                status = response.status_code
                if status in _REDIRECTS:
                    location = _single_header(
                        response.headers, "Location", "cover_redirect_blocked"
                    )
                    response.close()
                    response = None
                    if redirect_count >= self.max_redirects:
                        raise _CoverFailure("cover_redirect_limit")
                    if not location:
                        raise _CoverFailure("cover_redirect_blocked")
                    try:
                        current = urljoin(normalized, location)
                        _normalize_url(current)
                    except _CoverFailure as exc:
                        raise _CoverFailure("cover_redirect_blocked") from exc
                    continue
                break
            if response is None:
                raise _CoverFailure("cover_redirect_blocked")
            if response.status_code != 200:
                raise _CoverFailure("cover_http_status")
            encoding = _single_header(
                response.headers, "Content-Encoding", "cover_compression_blocked"
            ).casefold()
            if encoding not in {"", "identity"}:
                raise _CoverFailure("cover_compression_blocked")
            mime = _single_header(
                response.headers, "Content-Type", "cover_type_mismatch"
            )
            mime = mime.split(";", 1)[0].strip().casefold()
            if mime not in _MIMES:
                raise _CoverFailure("cover_type_mismatch")
            length_header = _single_header(
                response.headers, "Content-Length", "cover_truncated"
            )
            announced: int | None = None
            if length_header:
                if not re.fullmatch(r"0|[1-9][0-9]*", length_header):
                    raise _CoverFailure("cover_truncated")
                if len(length_header) > 20:
                    raise _CoverFailure("cover_too_large")
                announced = int(length_header)
                if announced > self.max_bytes:
                    raise _CoverFailure("cover_too_large")

            check_stop()
            managed = _open_managed_directory(self.root, platform_key)
            lock, lock_fd = _create_named_owned(
                managed.platform_fd, f".{digest}.lock"
            )
            os.close(lock_fd)
            temp, temp_fd = _create_owned(
                managed.platform_fd, f".{digest}.", ".part"
            )
            written = 0
            try:
                while True:
                    check_stop()
                    response.set_read_timeout(min(self.read_timeout, remaining()))
                    chunk = response.read(min(64 * 1024, self.max_bytes + 1 - written))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_bytes:
                        raise _CoverFailure("cover_too_large")
                    _write_all(temp_fd, chunk)
                if written == 0 or (announced is not None and announced != written):
                    raise _CoverFailure("cover_truncated")
                check_stop()
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            _verify_owned(managed.platform_fd, temp)
            payload = _read_entry(managed.platform_fd, temp.name, self.max_bytes)
            detected_mime, extension = _validate_image(payload)
            if detected_mime != mime:
                raise _CoverFailure("cover_type_mismatch")
            file_hash = hashlib.sha256(payload).hexdigest()
            target_name = f"{digest}.{extension}"
            existing = _entry_info(managed.platform_fd, target_name)
            if existing is not None:
                old = _read_entry(managed.platform_fd, target_name, self.max_bytes)
                if old == payload:
                    _verify_directory(managed)
                    _verify_owned(managed.platform_fd, lock)
                    _unlink_owned_and_sync(managed, temp)
                    temp = None
                    primary_receipt = CoverReceipt(
                        "downloaded",
                        relative_path=f"{platform_key}/{target_name}",
                        size=written,
                        sha256=file_hash,
                        mime_type=mime,
                    )
                    return primary_receipt
            check_stop()
            self._commit(
                managed,
                temp,
                target_name,
                payload,
                detected_mime,
                digest,
                lock,
            )
            temp = None
            primary_receipt = CoverReceipt(
                "downloaded",
                relative_path=f"{platform_key}/{target_name}",
                size=written,
                sha256=file_hash,
                mime_type=mime,
            )
            return primary_receipt
        except BaseException as exc:
            primary_receipt = CoverReceipt.failed(_error_code(exc))
            return primary_receipt
        finally:
            cleanup_reason_code = ""
            if response is not None:
                try:
                    response.close()
                except BaseException:
                    pass
            if managed is not None:
                if temp is not None:
                    try:
                        if _entry_info(
                            managed.platform_fd, temp.name
                        ) is not None:
                            _unlink_owned_and_sync(managed, temp)
                    except BaseException as cleanup_exc:
                        cleanup_reason_code = (
                            "cover_path_blocked"
                            if _error_code(
                                cleanup_exc, "cover_commit_uncertain"
                            ) == "cover_path_blocked"
                            else "cover_commit_uncertain"
                        )
                if lock is not None:
                    try:
                        _unlink_owned_and_sync(managed, lock)
                    except BaseException as cleanup_exc:
                        current_code = _error_code(
                            cleanup_exc, "cover_commit_uncertain"
                        )
                        if current_code == "cover_path_blocked":
                            cleanup_reason_code = "cover_path_blocked"
                        elif not cleanup_reason_code:
                            cleanup_reason_code = "cover_commit_uncertain"
                managed.close()
            if cleanup_reason_code:
                if (
                    primary_receipt is not None
                    and primary_receipt.reason_code == "cover_path_blocked"
                ):
                    return primary_receipt
                return CoverReceipt.failed(cleanup_reason_code)

    def _commit(
        self,
        managed: _ManagedDirectory,
        temp: _OwnedFile,
        target_name: str,
        payload: bytes,
        mime: str,
        digest: str,
        lock: _OwnedFile,
    ) -> None:
        backups: dict[str, _OwnedFile] = {}
        old_payloads: dict[str, bytes] = {}
        restore_candidates: list[_OwnedFile] = []
        preserve_backups = False
        rollback_degraded = False
        replaced = False
        commit_started = False
        commit_durable = False
        old_names = [
            f"{digest}.{extension}"
            for extension in _EXTENSIONS
            if _entry_info(managed.platform_fd, f"{digest}.{extension}") is not None
        ]
        if len(old_names) > 1:
            raise _CoverFailure("cover_commit_uncertain")
        try:
            _verify_directory(managed)
            _verify_owned(managed.platform_fd, temp)
            _verify_owned(managed.platform_fd, lock)
            commit_started = True
            for old_name in old_names:
                old_payload = _read_entry(managed.platform_fd, old_name, self.max_bytes)
                backup, descriptor = _create_named_owned(
                    managed.platform_fd, _rollback_name(old_name, old_payload)
                )
                backups[old_name] = backup
                old_payloads[old_name] = old_payload
                try:
                    _write_all(descriptor, old_payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _read_valid_rollback(
                    managed.platform_fd, backup.name, max_bytes=self.max_bytes
                )
            if backups:
                os.fsync(managed.platform_fd)
            _verify_directory(managed)
            _verify_owned(managed.platform_fd, temp)
            _verify_owned(managed.platform_fd, lock)
            os.replace(
                temp.name,
                target_name,
                src_dir_fd=managed.platform_fd,
                dst_dir_fd=managed.platform_fd,
            )
            replaced = True
            os.fsync(managed.platform_fd)
            _verify_owned(managed.platform_fd, lock)
            _verify_directory(managed)
            committed = _read_entry(managed.platform_fd, target_name, self.max_bytes)
            committed_mime, _extension = _validate_image(committed)
            if committed != payload or committed_mime != mime:
                raise _CoverFailure("cover_commit_failed")
            for old_name in old_names:
                if old_name != target_name:
                    _entry_info(managed.platform_fd, old_name)
                    os.unlink(old_name, dir_fd=managed.platform_fd)
            os.fsync(managed.platform_fd)
            _verify_owned(managed.platform_fd, lock)
            _verify_directory(managed)
            committed = _read_entry(managed.platform_fd, target_name, self.max_bytes)
            committed_mime, _extension = _validate_image(committed)
            if committed != payload or committed_mime != mime:
                raise _CoverFailure("cover_commit_failed")
            commit_durable = True
            for backup in tuple(backups.values()):
                _unlink_owned(managed.platform_fd, backup)
            backups.clear()
            os.fsync(managed.platform_fd)
            _verify_owned(managed.platform_fd, lock)
            _verify_directory(managed)
        except BaseException as exc:
            if commit_durable:
                raise _CoverFailure("cover_commit_uncertain") from exc
            if replaced:
                try:
                    for old_name, backup in tuple(backups.items()):
                        _verify_owned(managed.platform_fd, backup)
                        old_payload = old_payloads[old_name]
                        if (
                            _read_entry(
                                managed.platform_fd, backup.name, self.max_bytes
                            )
                            != old_payload
                        ):
                            raise _CoverFailure("cover_commit_uncertain")
                        candidate, descriptor = _create_owned(
                            managed.platform_fd, f".{digest}.", ".restore"
                        )
                        restore_candidates.append(candidate)
                        try:
                            _write_all(descriptor, old_payload)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        try:
                            os.replace(
                                candidate.name,
                                old_name,
                                src_dir_fd=managed.platform_fd,
                                dst_dir_fd=managed.platform_fd,
                            )
                        except BaseException:
                            rollback_degraded = True
                            _verify_owned(managed.platform_fd, candidate)
                            _rewrite_or_create_entry(
                                managed.platform_fd, old_name, old_payload
                            )
                            _unlink_owned(managed.platform_fd, candidate)
                        restore_candidates.remove(candidate)
                        if (
                            _read_entry(
                                managed.platform_fd, old_name, self.max_bytes
                            )
                            != old_payload
                        ):
                            raise _CoverFailure("cover_commit_uncertain")
                    os.fsync(managed.platform_fd)
                    _verify_directory(managed)
                    for old_name, old_payload in old_payloads.items():
                        if (
                            _read_entry(
                                managed.platform_fd, old_name, self.max_bytes
                            )
                            != old_payload
                        ):
                            raise _CoverFailure("cover_commit_uncertain")
                    if target_name not in old_names:
                        if _entry_info(managed.platform_fd, target_name) is not None:
                            os.unlink(target_name, dir_fd=managed.platform_fd)
                        os.fsync(managed.platform_fd)
                        _verify_directory(managed)
                    for backup in tuple(backups.values()):
                        _unlink_owned(managed.platform_fd, backup)
                    backups.clear()
                    os.fsync(managed.platform_fd)
                    _verify_directory(managed)
                except BaseException as rollback_exc:
                    preserve_backups = True
                    raise _CoverFailure("cover_commit_uncertain") from rollback_exc
                if rollback_degraded:
                    raise _CoverFailure("cover_commit_uncertain") from exc
                raise _CoverFailure("cover_commit_failed") from exc
            if backups:
                try:
                    for backup in tuple(backups.values()):
                        _unlink_owned(managed.platform_fd, backup)
                    backups.clear()
                    os.fsync(managed.platform_fd)
                    _verify_directory(managed)
                except BaseException as cleanup_exc:
                    preserve_backups = True
                    raise _CoverFailure("cover_commit_uncertain") from cleanup_exc
            if commit_started:
                raise _CoverFailure("cover_commit_failed") from exc
            if isinstance(exc, _CoverFailure):
                raise
            raise _CoverFailure("cover_io_failed") from exc
        finally:
            for candidate in restore_candidates:
                try:
                    _unlink_owned(managed.platform_fd, candidate)
                except BaseException:
                    pass
            if not preserve_backups:
                for backup in backups.values():
                    try:
                        _unlink_owned(managed.platform_fd, backup)
                    except BaseException:
                        pass
