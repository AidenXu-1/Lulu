"""Reliable, privacy-bounded support for local TTS model installation.

This module stays stdlib-only.  It owns resumable byte downloads, bounded
retry/cancellation, free-space preflight, and a small redacted diagnostics log.
Model-specific readiness and archive extraction remain in their model modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import IncompleteRead
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from .. import models_manager as mm


MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_DOWNLOAD_BYTES = 8 * GIB
LOG_MAX_BYTES = 1 * MIB
DOWNLOAD_ATTEMPTS = 3
_CHECKPOINT_BYTES = 8 * MIB
_TOKEN = re.compile(r"^[a-z0-9_.-]{0,80}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_META_FIELDS = frozenset({"schema", "url", "filename", "sha256", "size", "downloaded_bytes"})
_LOG_FIELDS = (
    "schema", "at", "model_id", "event", "phase", "source", "attempt",
    "downloaded_bytes", "expected_bytes", "error_code",
)


class CancelledError(Exception):
    """The user cancelled an install while preserving safe progress."""


class InstallError(RuntimeError):
    """Stable installation failure with a user-facing message and retry class."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadIdentity:
    url: str
    filename: str
    sha256: str
    size: int
    source: str
    require_sha256: bool = True

    def validate(self) -> None:
        if (
            not isinstance(self.url, str)
            or not self.url.startswith("https://")
            or Path(self.filename).name != self.filename
            or self.filename in {"", ".", ".."}
            or (self.require_sha256 and not _SHA256.fullmatch(self.sha256))
            or (not self.require_sha256 and self.sha256 and not _SHA256.fullmatch(self.sha256))
            or type(self.size) is not int
            or not 0 < self.size <= MAX_DOWNLOAD_BYTES
            or not _TOKEN.fullmatch(self.source)
        ):
            raise InstallError("manifest", "运行环境清单无效，已停止安装。")
        validate_source_url(self.url, self.source)


def validate_source_url(url: str, source: str) -> None:
    """Keep automatic redirects inside the explicitly selected official source."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise InstallError("unsafe_url", "模型下载地址不安全，已停止安装。")
    allowed = False
    if source == "github_release":
        allowed = host in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    elif source == "modelscope_g2pw":
        allowed = _is_domain_or_subdomain(host, "modelscope.cn") or host.endswith(".aliyuncs.com")
    elif source == "fasttext_lid":
        allowed = host == "dl.fbaipublicfiles.com"
    if not allowed:
        raise InstallError("unsafe_url", "模型下载跳转离开了允许来源，已停止安装。")


def _is_domain_or_subdomain(host: str, domain: str) -> bool:
    """Match one DNS boundary, not lookalike suffixes such as domain.evil.test."""
    return host == domain or host.endswith(f".{domain}")


class _SourceRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject an out-of-source redirect before urllib connects to its target."""

    def __init__(self, source: str):
        super().__init__()
        self._source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_source_url(str(newurl), self._source)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _safe_regular(path: Path, *, maximum: int | None = None) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise InstallError("unsafe_cache", "下载缓存身份异常，已停止安装。")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise InstallError("unsafe_cache", "下载缓存不属于当前用户，已停止安装。")
    if maximum is not None and info.st_size > maximum:
        raise InstallError("size", "下载缓存大小异常，已停止安装。")
    return info


def _atomic_json(path: Path, payload: dict) -> None:
    _private_dir(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


def _read_meta(path: Path) -> dict:
    info = _safe_regular(path, maximum=64 * 1024)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise InstallError("unsafe_cache", "下载缓存读取时发生变化，已停止安装。")
        raw = os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 64 * 1024:
        raise InstallError("unsafe_cache", "下载缓存元数据过大，已停止安装。")
    value = json.loads(raw.decode("utf-8"))
    if type(value) is not dict or set(value) != _META_FIELDS:
        raise InstallError("unsafe_cache", "下载缓存元数据无效，已停止安装。")
    return value


def _meta_payload(identity: DownloadIdentity, downloaded: int) -> dict:
    return {
        "schema": 1,
        "url": identity.url,
        "filename": identity.filename,
        "sha256": identity.sha256,
        "size": identity.size,
        "downloaded_bytes": downloaded,
    }


def _matches_meta(value: dict, identity: DownloadIdentity) -> bool:
    return (
        value.get("schema") == 1
        and value.get("url") == identity.url
        and value.get("filename") == identity.filename
        and value.get("sha256") == identity.sha256
        and value.get("size") == identity.size
        and type(value.get("downloaded_bytes")) is int
        and 0 <= value["downloaded_bytes"] <= identity.size
    )


def _discard_download(part: Path, meta: Path, final: Path | None = None) -> None:
    for candidate in (part, meta, final):
        if candidate is None:
            continue
        try:
            if candidate.exists() and not candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _load_resume(identity: DownloadIdentity, part: Path, meta: Path) -> int:
    if not part.exists() and not meta.exists():
        return 0
    if not part.exists() or not meta.exists() or part.is_symlink() or meta.is_symlink():
        _discard_download(part, meta)
        return 0
    try:
        part_info = _safe_regular(part, maximum=identity.size)
        value = _read_meta(meta)
        if not _matches_meta(value, identity):
            raise InstallError("resume", "下载缓存已过期。")
        checkpoint = int(value["downloaded_bytes"])
        if part_info.st_size < checkpoint:
            raise InstallError("resume", "下载缓存不完整。")
        if part_info.st_size > checkpoint:
            with open(part, "r+b") as handle:
                handle.truncate(checkpoint)
        return checkpoint
    except Exception:
        _discard_download(part, meta)
        return 0


def _status(response) -> int:
    value = getattr(response, "status", None)
    if value is None and hasattr(response, "getcode"):
        value = response.getcode()
    return int(value or 200)


def _header(response, name: str) -> str:
    headers = getattr(response, "headers", {})
    return str(headers.get(name) or headers.get(name.lower()) or "")


def _checkpoint(handle, meta: Path, identity: DownloadIdentity, downloaded: int) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    _atomic_json(meta, _meta_payload(identity, downloaded))


def _normalise_error(exc: BaseException) -> str:
    if isinstance(exc, InstallError):
        return exc.code
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {408, 429}:
            return "rate_limited"
        return "http_5xx" if exc.code >= 500 else "http_4xx"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, IncompleteRead):
        return "incomplete"
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return "network"
    name = type(exc).__name__.lower()
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {408, 429}:
            return "rate_limited"
        return "http_5xx" if status_code >= 500 else "http_4xx"
    if "timeout" in name:
        return "timeout"
    if any(token in name for token in ("connection", "network", "proxy")):
        return "network"
    return "internal"


def normalise_error(exc: BaseException) -> str:
    """Public redacted error classifier for model-source wrappers."""
    return _normalise_error(exc)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, InstallError):
        return exc.retryable
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 429} or exc.code >= 500
    if isinstance(
        exc,
        (TimeoutError, socket.timeout, IncompleteRead, urllib.error.URLError, ConnectionError, OSError),
    ):
        return True
    return _normalise_error(exc) in {"timeout", "network", "http_5xx", "incomplete", "rate_limited"}


def _wait_cancelable(seconds: float, is_cancelled: Callable[[], bool], sleep: Callable[[float], None]) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0:
        if is_cancelled():
            raise CancelledError()
        step = min(0.1, remaining)
        sleep(step)
        remaining -= step


def retry_call(
    call: Callable[[int], object],
    *,
    is_cancelled: Callable[[], bool],
    on_retry: Callable[[int, str], None] | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Run a model-source operation with bounded, cancellation-aware retry."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if is_cancelled():
            raise CancelledError()
        try:
            return call(attempt)
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= attempts or not _retryable(exc):
                raise
            if on_retry:
                on_retry(attempt + 1, _normalise_error(exc))
            _wait_cancelable((0.5, 1.5, 3.0)[min(attempt - 1, 2)], is_cancelled, sleep)
    assert last is not None
    raise last


def resumable_download(
    identity: DownloadIdentity,
    directory: Path,
    progress: Callable[[str, float], None],
    is_cancelled: Callable[[], bool],
    *,
    label: str,
    model_id: str,
    phase: str = "runtime_download",
    urlopen=None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Download and SHA-verify one immutable asset, preserving safe progress."""
    identity.validate()
    _private_dir(directory)
    final = directory / identity.filename
    part = directory / f"{identity.filename}.part"
    meta = directory / f"{identity.filename}.part.json"
    opener = urlopen or urllib.request.build_opener(_SourceRedirectHandler(identity.source)).open

    if final.exists():
        try:
            info = _safe_regular(final, maximum=identity.size)
            digest_ok = not identity.require_sha256 or sha256_file(final) == identity.sha256
            if info.st_size == identity.size and digest_ok:
                progress(f"{label}已校验，可继续安装", 0.6)
                return final
        except Exception:
            pass
        _discard_download(part, meta, final)

    offset = _load_resume(identity, part, meta)
    if offset:
        progress(f"{label}从 {format_bytes(offset)} 继续", 0.05 + 0.55 * offset / identity.size)

    def attempt_download(attempt: int) -> Path:
        nonlocal offset
        if is_cancelled():
            raise CancelledError()
        headers = {"User-Agent": "Lulu TTS Installer", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(identity.url, headers=headers)
        log_install_event(
            model_id=model_id, event="attempt", phase=phase, source=identity.source,
            attempt=attempt, downloaded_bytes=offset, expected_bytes=identity.size,
        )
        try:
            response = opener(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset == identity.size:
                if not identity.require_sha256 or sha256_file(part) == identity.sha256:
                    os.replace(part, final)
                    meta.unlink(missing_ok=True)
                    return final
                _discard_download(part, meta)
                offset = 0
                raise InstallError("sha256", "下载文件校验失败，已丢弃不可信进度。") from exc
            raise

        with response:
            final_url = response.geturl() if hasattr(response, "geturl") else identity.url
            validate_source_url(str(final_url), identity.source)
            response_status = _status(response)
            if offset:
                content_range = _header(response, "Content-Range")
                match = _CONTENT_RANGE.fullmatch(content_range)
                valid_range = (
                    response_status == 206
                    and match is not None
                    and int(match.group(1)) == offset
                    and int(match.group(2)) < identity.size
                    and int(match.group(3)) == identity.size
                )
                if not valid_range:
                    _discard_download(part, meta)
                    offset = 0
                    if response_status == 200:
                        raise InstallError("range_restart", "下载源未接受续传，正在从头重新连接。", retryable=True)
                    raise InstallError("range", "下载续传响应不一致，已安全重置。", retryable=True)
            elif response_status != 200:
                raise InstallError("http", "下载服务器响应异常。", retryable=response_status >= 500)

            length_text = _header(response, "Content-Length")
            if length_text:
                if not length_text.isdigit():
                    raise InstallError("size", "下载响应大小无效。")
                expected_response = identity.size - offset
                if int(length_text) != expected_response:
                    raise InstallError("size", "下载响应大小与发布清单不一致。")

            flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if offset else os.O_TRUNC)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(part, flags, 0o600)
            downloaded = offset
            last_checkpoint = offset
            try:
                with os.fdopen(fd, "ab" if offset else "wb") as handle:
                    while True:
                        if is_cancelled():
                            _checkpoint(handle, meta, identity, downloaded)
                            offset = downloaded
                            raise CancelledError()
                        chunk = response.read(MIB)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > identity.size:
                            raise InstallError("size", "下载内容超过发布清单大小。")
                        handle.write(chunk)
                        if downloaded - last_checkpoint >= _CHECKPOINT_BYTES:
                            _checkpoint(handle, meta, identity, downloaded)
                            last_checkpoint = downloaded
                        progress(
                            f"{label} {downloaded * 100 // identity.size}%",
                            0.05 + 0.55 * downloaded / identity.size,
                        )
                    _checkpoint(handle, meta, identity, downloaded)
            except Exception:
                offset = part.stat().st_size if part.exists() else 0
                if part.exists():
                    try:
                        _atomic_json(meta, _meta_payload(identity, offset))
                    except OSError:
                        pass
                raise

        offset = downloaded
        if downloaded != identity.size:
            raise InstallError("incomplete", "下载中断，已保留当前进度。", retryable=True)
        progress("校验运行环境完整性…", 0.62)
        if identity.require_sha256 and sha256_file(part) != identity.sha256:
            _discard_download(part, meta)
            offset = 0
            raise InstallError("sha256", "运行环境校验失败，已丢弃不可信文件。")
        os.replace(part, final)
        os.chmod(final, 0o600)
        meta.unlink(missing_ok=True)
        return final

    try:
        return retry_call(
            attempt_download,
            is_cancelled=is_cancelled,
            on_retry=lambda nxt, code: (
                progress(f"{label}连接中断，正在第 {nxt} 次重试…", -1.0),
                log_install_event(
                    model_id=model_id, event="retry", phase=phase, source=identity.source,
                    attempt=nxt, downloaded_bytes=offset, expected_bytes=identity.size, error_code=code,
                ),
            ),
            sleep=sleep,
        )
    except CancelledError:
        log_install_event(
            model_id=model_id, event="cancelled", phase=phase, source=identity.source,
            downloaded_bytes=offset, expected_bytes=identity.size, error_code="cancelled",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        code = _normalise_error(exc)
        log_install_event(
            model_id=model_id, event="failed", phase=phase, source=identity.source,
            downloaded_bytes=offset, expected_bytes=identity.size, error_code=code,
        )
        if isinstance(exc, InstallError):
            raise
        if code in {"network", "timeout", "http_5xx", "incomplete", "rate_limited"}:
            raise InstallError(
                code,
                "下载中断，已保留当前进度。请检查网络后点继续安装。",
            ) from exc
        if code == "http_4xx":
            raise InstallError(
                code,
                "下载源暂时不可用，请稍后点继续安装。已下载的有效进度会保留。",
            ) from exc
        raise InstallError("internal", "下载没有完成，已保留可复用的进度。请稍后继续安装。") from exc


def sha256_file(path: Path) -> str:
    info = _safe_regular(path, maximum=MAX_DOWNLOAD_BYTES)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise InstallError("unsafe_cache", "下载文件校验时发生变化。")
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(MIB), b""):
                digest.update(chunk)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return digest.hexdigest()


def nearest_existing_parent(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def require_free_space(target: Path, required: int) -> tuple[int, int]:
    """Fail before networking when the target volume lacks conservative headroom."""
    if required <= 0:
        return 0, shutil.disk_usage(nearest_existing_parent(target)).free
    free = shutil.disk_usage(nearest_existing_parent(target)).free
    if free < required:
        raise InstallError(
            "disk_space",
            f"磁盘空间不足：本阶段约需 {format_bytes(required)}，当前可用 {format_bytes(free)}。"
            "已下载进度会保留，请清理空间或更改模型保存位置后继续安装。",
        )
    return required, free


def format_bytes(value: int) -> str:
    from ..model_transfer import format_bytes as format_download_bytes
    return format_download_bytes(value)


def diagnostics_path() -> Path:
    return mm.app_data_dir() / "tts-installs" / "logs" / "install.jsonl"


def _validate_log_token(value: object) -> str:
    text = str(value or "")
    return text if _TOKEN.fullmatch(text) else "invalid"


def _rotate_log(path: Path) -> None:
    if not path.exists():
        return
    info = _safe_regular(path, maximum=LOG_MAX_BYTES * 4)
    if info.st_size < LOG_MAX_BYTES:
        return
    backup = path.with_name("install.1.jsonl")
    if backup.exists():
        _safe_regular(backup, maximum=LOG_MAX_BYTES * 4)
        backup.unlink()
    os.replace(path, backup)
    os.chmod(backup, 0o600)


def log_install_event(
    *,
    model_id: str,
    event: str,
    phase: str = "",
    source: str = "",
    attempt: int = 0,
    downloaded_bytes: int = 0,
    expected_bytes: int = 0,
    error_code: str = "",
) -> None:
    """Append one redacted event; diagnostics must never block installation."""
    payload = {
        "schema": 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "model_id": _validate_log_token(model_id),
        "event": _validate_log_token(event),
        "phase": _validate_log_token(phase),
        "source": _validate_log_token(source),
        "attempt": max(0, int(attempt or 0)),
        "downloaded_bytes": max(0, int(downloaded_bytes or 0)),
        "expected_bytes": max(0, int(expected_bytes or 0)),
        "error_code": _validate_log_token(error_code),
    }
    assert tuple(payload) == _LOG_FIELDS
    try:
        path = diagnostics_path()
        _private_dir(path.parent)
        _rotate_log(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("unsafe diagnostics target")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise OSError("foreign diagnostics target")
            os.fchmod(fd, 0o600)
            raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short diagnostics write")
                view = view[written:]
        finally:
            os.close(fd)
    except Exception:
        return


def partial_download_bytes(directory: Path) -> int:
    total = 0
    try:
        for path in directory.glob("*.part"):
            try:
                total += _safe_regular(path, maximum=MAX_DOWNLOAD_BYTES).st_size
            except Exception:
                continue
    except OSError:
        return 0
    return total
