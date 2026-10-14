from __future__ import annotations

from dataclasses import dataclass
import http.client
import time
import socket
from typing import BinaryIO, Callable
import urllib.error
import urllib.parse
import urllib.request

from .constants import (
    ALLOWED_GITHUB_RELEASE_PREFIXES,
    ALLOWED_INITIAL_HOSTS,
    ALLOWED_REDIRECT_HOSTS,
    MAX_REDIRECTS,
    MAX_URL_BYTES,
)
from .models import UpdateError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    stream: BinaryIO


def validate_url(url: str, *, initial: bool = False) -> urllib.parse.ParseResult:
    if not isinstance(url, str) or len(url.encode("utf-8")) > MAX_URL_BYTES:
        raise UpdateError("unsafe_url", "更新地址不安全")
    parsed = urllib.parse.urlparse(url)
    allowed = ALLOWED_INITIAL_HOSTS if initial else ALLOWED_REDIRECT_HOSTS
    if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password:
        raise UpdateError("unsafe_url", "更新地址不安全")
    if parsed.port not in (None, 443) or parsed.fragment:
        raise UpdateError("unsafe_url", "更新地址不安全")
    if parsed.hostname == "github.com":
        prefix = next(
            (
                candidate
                for candidate in ALLOWED_GITHUB_RELEASE_PREFIXES
                if parsed.path.startswith(candidate)
            ),
            None,
        )
        if parsed.query or prefix is None:
            raise UpdateError("unsafe_url", "更新地址不安全")
        tail = parsed.path[len(prefix):]
        latest = tail in {"latest/download/version.json", "latest/download/version.json.sig"}
        # GitHub 会把 latest 清单先跳到同仓库的具体版本路径。
        # 只允许这两个固定清单文件作为跳转目标，不将它们扩大为初始请求入口。
        tagged_manifest = (
            not initial
            and bool(__import__("re").fullmatch(
                r"download/v[0-9]+\.[0-9]+\.[0-9]+/version\.json(?:\.sig)?", tail
            ))
        )
        asset = bool(__import__("re").fullmatch(r"download/v[0-9]+\.[0-9]+\.[0-9]+/Lulu-[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}\.[0-9]+-arm64\.(zip|dmg)", tail))
        if not (latest or tagged_manifest or asset):
            raise UpdateError("unsafe_url", "更新地址不安全")
    return parsed


class SecureHttpClient:
    def __init__(self, *, opener=None, clock: Callable[[], float] = time.monotonic):
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.clock = clock

    def open(self, url: str, *, headers: dict[str, str], connect_timeout: int, total_timeout: int) -> Response:
        validate_url(url, initial=True)
        started = self.clock()
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if self.clock() - started > total_timeout:
                raise UpdateError("timeout", "更新请求超时")
            request = urllib.request.Request(current, headers=headers, method="GET")
            try:
                raw = self.opener.open(request, timeout=connect_timeout)
                status = int(getattr(raw, "status", 200))
            except urllib.error.HTTPError as exc:
                raw = exc
                status = int(exc.code)
            except (TimeoutError, socket.timeout) as exc:
                raise UpdateError("timeout", "更新请求超时") from exc
            except Exception as exc:
                raise UpdateError("network", "暂时无法连接更新服务器") from exc
            if status in (301, 302, 303, 307, 308):
                location = raw.headers.get("Location")
                raw.close()
                if hop >= MAX_REDIRECTS or not location:
                    raise UpdateError("unsafe_redirect", "更新跳转不安全")
                current = urllib.parse.urljoin(current, location)
                validate_url(current)
                continue
            if status not in (200, 206):
                raw.close()
                raise UpdateError("http", "更新服务器响应异常")
            _set_read_timeout(raw)
            return Response(current, status, {k.lower(): v for k, v in raw.headers.items()}, raw)
        raise UpdateError("unsafe_redirect", "更新跳转不安全")

    def read_limited(self, response: Response, *, limit: int, total_timeout: int, chunk_size: int = 65536) -> bytes:
        started = self.clock()
        chunks: list[bytes] = []
        total = 0
        try:
            expected_size = None
            # Bounded HTTPResponse.read(n) may return early EOF without raising.
            # Reject incomplete transport before passing bytes to signature/JSON
            # validation, so a dropped connection cannot invalidate resume state.
            length = response.headers.get("content-length")
            if length is not None and response.headers.get("transfer-encoding", "").lower() != "chunked":
                length = length.strip()
                if not length.isascii() or not length.isdecimal():
                    raise UpdateError("http", "更新响应长度异常")
                try:
                    expected_size = int(length)
                except ValueError as exc:
                    raise UpdateError("http", "更新响应长度异常") from exc
                if expected_size > limit:
                    raise UpdateError("too_large", "更新响应过大")
            while True:
                if self.clock() - started > total_timeout:
                    raise UpdateError("timeout", "更新请求超时")
                part = response.stream.read(min(chunk_size, limit - total + 1))
                if not part:
                    break
                total += len(part)
                if total > limit:
                    raise UpdateError("too_large", "更新响应过大")
                chunks.append(part)
            if expected_size is not None and total != expected_size:
                raise UpdateError("network", "更新信息下载中断，请重试")
        except (TimeoutError, socket.timeout) as exc:
            raise UpdateError("timeout", "更新请求超时") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise UpdateError("network", "更新信息下载中断，请重试") from exc
        finally:
            response.stream.close()
        return b"".join(chunks)


def _set_read_timeout(raw) -> None:
    """Bind blocking response reads to the frozen idle timeout when a socket exists."""
    from .constants import READ_IDLE_TIMEOUT_SECONDS
    candidates = (
        getattr(getattr(getattr(raw, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(raw, "fp", None), "_sock", None),
    )
    for sock in candidates:
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(READ_IDLE_TIMEOUT_SECONDS)
            return
