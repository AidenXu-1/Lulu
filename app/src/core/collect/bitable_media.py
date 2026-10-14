"""飞书多维表格音视频附件的本地冻结与复核。

这里只读取 Lulu 受管 ``collection/media``，不访问作品链接、不补下载，也不
读取用户自选素材目录。网络上传前后都从同一个只读文件描述符复核身份与哈希。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zlib

from .models import CollectedItem

BITABLE_MEDIA_UPLOAD_ALL_MAX = 20_971_520
BITABLE_MEDIA_MAX_BYTES = 2_000_000_000
BITABLE_MEDIA_BLOCK_SIZE = 4_194_304

_STABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}:[^:\s/\\]{1,256}$")
_AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
_VIDEO_EXTENSIONS = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_MEDIA_EXTENSIONS = _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS
_SNIFF_BYTES = 1024 * 1024


def _managed_media_root() -> Path:
    from ..models_manager import app_data_dir

    return app_data_dir() / "collection" / "media"


class BitableMediaFileError(RuntimeError):
    """只携带可机械判断的原因码，不包含本地路径。"""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "media_file_rejected")
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class BitableMediaCandidate:
    record_index: int
    stable_key: str
    title: str
    relative_path: str = field(repr=False)
    file_name: str
    mime_type: str
    size: int
    sha256: str = field(repr=False)
    adler32: str = field(repr=False)
    device: int = field(repr=False)
    inode: int = field(repr=False)
    mtime_ns: int = field(repr=False)
    managed_root: Path = field(repr=False, compare=False)


@dataclass(frozen=True)
class BitableMediaIssue:
    record_index: int
    stable_key: str
    title: str
    reason_code: str
    retry_hint: str


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_chain(path: Path) -> tuple[int, list[int]]:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute == Path(absolute.anchor):
        raise BitableMediaFileError("media_file_rejected")
    trusted_candidates = (Path(tempfile.gettempdir()), Path.home())
    applicable: list[Path] = []
    for candidate in trusted_candidates:
        try:
            absolute.relative_to(candidate)
        except ValueError:
            continue
        applicable.append(candidate)
    if not applicable:
        raise BitableMediaFileError("media_file_rejected")
    trusted = max(applicable, key=lambda value: len(value.parts))
    relative = absolute.relative_to(trusted)
    descriptors: list[int] = []
    try:
        current = os.open(trusted, _directory_flags())
        descriptors.append(current)
        for component in relative.parts:
            before = os.stat(component, dir_fd=current, follow_symlinks=False)
            child = os.open(component, _directory_flags(), dir_fd=current)
            descriptors.append(child)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise BitableMediaFileError("media_path_escape")
            current = child
        return current, descriptors
    except BaseException as exc:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, BitableMediaFileError):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise BitableMediaFileError("media_file_rejected") from exc
        raise


def _open_media_file(
    root: Path,
    relative_path: str,
) -> tuple[int, int, list[int], PurePosixPath]:
    value = str(relative_path or "")
    if not value or "\\" in value:
        raise BitableMediaFileError("media_file_rejected")
    raw_parts = value.split("/")
    if len(raw_parts) != 2 or any(part in {"", ".", ".."} for part in raw_parts):
        raise BitableMediaFileError("media_file_rejected")
    relative = PurePosixPath(*raw_parts)
    if relative.is_absolute():
        raise BitableMediaFileError("media_file_rejected")
    root_fd, descriptors = _open_directory_chain(root)
    try:
        platform_before = os.stat(
            relative.parts[0], dir_fd=root_fd, follow_symlinks=False
        )
        platform_fd = os.open(relative.parts[0], _directory_flags(), dir_fd=root_fd)
        descriptors.append(platform_fd)
        platform_opened = os.fstat(platform_fd)
        if (
            not stat.S_ISDIR(platform_before.st_mode)
            or (platform_before.st_dev, platform_before.st_ino)
            != (platform_opened.st_dev, platform_opened.st_ino)
        ):
            raise BitableMediaFileError("media_path_escape")
        before = os.stat(relative.parts[1], dir_fd=platform_fd, follow_symlinks=False)
        file_fd = os.open(
            relative.parts[1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=platform_fd,
        )
        descriptors.append(file_fd)
        opened = os.fstat(file_fd)
        after = os.stat(relative.parts[1], dir_fd=platform_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or before.st_nlink != 1
            or opened.st_nlink != 1
            or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise BitableMediaFileError("media_file_rejected")
        return file_fd, platform_fd, descriptors, relative
    except BaseException as exc:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, BitableMediaFileError):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise BitableMediaFileError("media_file_rejected") from exc
        raise


def _hash_stream(file_fd: int) -> tuple[str, str, bytes]:
    digest = hashlib.sha256()
    checksum = 1
    head = bytearray()
    tail = bytearray()
    os.lseek(file_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        checksum = zlib.adler32(chunk, checksum)
        if len(head) < _SNIFF_BYTES:
            head.extend(chunk[: _SNIFF_BYTES - len(head)])
        tail.extend(chunk)
        if len(tail) > _SNIFF_BYTES:
            del tail[:-_SNIFF_BYTES]
    os.lseek(file_fd, 0, os.SEEK_SET)
    sample = bytes(head) if len(head) == len(tail) and head == tail else bytes(head + tail)
    return digest.hexdigest(), str(checksum & 0xFFFFFFFF), sample


def _freeze_media_snapshot(file_fd: int, snapshot: object) -> tuple[str, str, bytes, int]:
    """把已打开的受管文件冻结到私有对象，并同时计算实际上传摘要。"""

    digest = hashlib.sha256()
    checksum = 1
    head = bytearray()
    tail = bytearray()
    total = 0
    os.lseek(file_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        if snapshot.write(chunk) != len(chunk):
            raise BitableMediaFileError("media_file_changed")
        digest.update(chunk)
        checksum = zlib.adler32(chunk, checksum)
        total += len(chunk)
        if len(head) < _SNIFF_BYTES:
            head.extend(chunk[: _SNIFF_BYTES - len(head)])
        tail.extend(chunk)
        if len(tail) > _SNIFF_BYTES:
            del tail[:-_SNIFF_BYTES]
    snapshot.flush()
    snapshot.seek(0)
    sample = bytes(head) if len(head) == len(tail) and head == tail else bytes(head + tail)
    return digest.hexdigest(), str(checksum & 0xFFFFFFFF), sample, total


def _detected_media_mime(suffix: str, sample: bytes) -> str:
    """对受支持容器做失败关闭的轻量内容/媒体流探测。"""

    if suffix == ".wav":
        return (
            "audio/wav"
            if len(sample) >= 12
            and sample[:4] == b"RIFF"
            and sample[8:12] == b"WAVE"
            and b"fmt " in sample
            and b"data" in sample
            else ""
        )
    if suffix == ".flac":
        return "audio/flac" if sample.startswith(b"fLaC") else ""
    if suffix == ".mp3":
        frame = len(sample) >= 2 and sample[0] == 0xFF and sample[1] & 0xE0 == 0xE0
        return "audio/mpeg" if sample.startswith(b"ID3") or frame else ""
    if suffix == ".aac":
        return (
            "audio/aac"
            if len(sample) >= 2 and sample[0] == 0xFF and sample[1] & 0xF6 == 0xF0
            else ""
        )
    if suffix in {".ogg", ".opus"}:
        if not sample.startswith(b"OggS"):
            return ""
        return "audio/ogg" if any(
            marker in sample for marker in (b"OpusHead", b"vorbis", b"fLaC")
        ) else ""
    if suffix in {".m4a", ".m4v", ".mp4", ".mov"}:
        if len(sample) < 12 or sample[4:8] != b"ftyp":
            return ""
        has_audio = b"soun" in sample
        has_video = b"vide" in sample
        if suffix == ".m4a":
            return "audio/mp4" if has_audio and not has_video else ""
        if suffix in {".m4v", ".mp4", ".mov"}:
            return (
                "video/quicktime" if suffix == ".mov" else "video/mp4"
            ) if has_video else ""
    if suffix in {".webm", ".mkv"}:
        if not sample.startswith(b"\x1aE\xdf\xa3"):
            return ""
        has_video = any(marker in sample for marker in (b"V_VP8", b"V_VP9", b"V_AV1", b"V_MPEG"))
        if not has_video:
            return ""
        return "video/webm" if suffix == ".webm" else "video/x-matroska"
    return ""


def _media_issue(
    item: CollectedItem,
    record_index: int,
    reason_code: str,
) -> BitableMediaIssue:
    hints = {
        "media_path_missing": "重新下载所选素材后再导出",
        "media_file_rejected": "确认素材仍在 Lulu 管理位置且未被替换后再试",
        "media_type_mismatch": "重新下载原始音频或视频后再导出",
        "media_file_too_large": "换用较小文件或仅保留飞书直链",
    }
    return BitableMediaIssue(
        record_index=record_index,
        stable_key=item.stable_key or f"row-{record_index + 1}",
        title=str(item.title or "")[:120],
        reason_code=reason_code,
        retry_hint=hints.get(reason_code, hints["media_file_rejected"]),
    )


def prepare_bitable_media(
    items: tuple[CollectedItem, ...],
    *,
    root: Path | str | None = None,
) -> tuple[tuple[BitableMediaCandidate, ...], tuple[BitableMediaIssue, ...]]:
    """冻结合格受管音视频；每个失败只影响自己的附件。"""

    configured_root = Path(root) if root is not None else _managed_media_root()
    candidates: list[BitableMediaCandidate] = []
    issues: list[BitableMediaIssue] = []
    for record_index, item in enumerate(items):
        if item.media_status != "downloaded":
            continue
        if not _STABLE_KEY_RE.fullmatch(item.stable_key or "") or not item.media_local_path:
            issues.append(_media_issue(item, record_index, "media_path_missing"))
            continue
        descriptors: list[int] = []
        try:
            file_fd, _platform_fd, descriptors, relative = _open_media_file(
                configured_root, item.media_local_path
            )
            info = os.fstat(file_fd)
            if info.st_size <= 0:
                raise BitableMediaFileError("media_file_rejected")
            if info.st_size > BITABLE_MEDIA_MAX_BYTES:
                raise BitableMediaFileError("media_file_too_large")
            suffix = Path(relative.parts[1]).suffix.casefold()
            if suffix not in _MEDIA_EXTENSIONS:
                raise BitableMediaFileError("media_type_mismatch")
            digest, checksum, sample = _hash_stream(file_fd)
            mime_type = _detected_media_mime(suffix, sample)
            if not mime_type or mime_type.split("/", 1)[0] not in {"audio", "video"}:
                raise BitableMediaFileError("media_type_mismatch")
            identity = hashlib.sha256(item.stable_key.encode("utf-8")).hexdigest()[:24]
            if (
                relative.parts[0] != item.stable_key.split(":", 1)[0]
                or not Path(relative.parts[1]).stem.startswith(f"{identity}-")
                or not Path(relative.parts[1]).stem.endswith(digest[:20])
            ):
                raise BitableMediaFileError("media_file_rejected")
            final = os.stat(relative.parts[1], dir_fd=_platform_fd, follow_symlinks=False)
            if (
                (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                or final.st_nlink != 1
            ):
                raise BitableMediaFileError("media_file_rejected")
            candidates.append(
                BitableMediaCandidate(
                    record_index=record_index,
                    stable_key=item.stable_key,
                    title=str(item.title or "")[:120],
                    relative_path=str(relative),
                    file_name=f"media-{identity}-{digest[:12]}{suffix}",
                    mime_type=mime_type,
                    size=info.st_size,
                    sha256=digest,
                    adler32=checksum,
                    device=info.st_dev,
                    inode=info.st_ino,
                    mtime_ns=info.st_mtime_ns,
                    managed_root=Path(os.path.abspath(configured_root)),
                )
            )
        except BitableMediaFileError as exc:
            issues.append(_media_issue(item, record_index, exc.reason_code))
        except (OSError, RuntimeError, ValueError):
            issues.append(_media_issue(item, record_index, "media_file_rejected"))
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return tuple(candidates), tuple(issues)


@contextmanager
def verified_bitable_media_stream(
    candidate: BitableMediaCandidate,
) -> Iterator[object]:
    """上传前后复核来源，并只暴露已冻结的私有上传对象。"""

    descriptors: list[int] = []
    stream = None
    try:
        file_fd, platform_fd, descriptors, relative = _open_media_file(
            candidate.managed_root, candidate.relative_path
        )
        info = os.fstat(file_fd)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        expected = (candidate.device, candidate.inode, candidate.size, candidate.mtime_ns)
        if identity != expected or info.st_nlink != 1:
            raise BitableMediaFileError("media_file_changed")
        source_ctime_ns = info.st_ctime_ns
        stream = tempfile.TemporaryFile(mode="w+b")
        os.fchmod(stream.fileno(), 0o600)
        digest, checksum, sample, frozen_size = _freeze_media_snapshot(file_fd, stream)
        suffix = Path(relative.parts[1]).suffix.casefold()
        after_freeze = os.fstat(file_fd)
        linked_after_freeze = os.stat(
            relative.parts[1], dir_fd=platform_fd, follow_symlinks=False
        )
        if (
            digest != candidate.sha256
            or checksum != candidate.adler32
            or frozen_size != candidate.size
            or _detected_media_mime(suffix, sample) != candidate.mime_type
            or (
                after_freeze.st_dev,
                after_freeze.st_ino,
                after_freeze.st_size,
                after_freeze.st_mtime_ns,
            ) != expected
            or (
                linked_after_freeze.st_dev,
                linked_after_freeze.st_ino,
                linked_after_freeze.st_size,
                linked_after_freeze.st_mtime_ns,
            ) != expected
            or after_freeze.st_ctime_ns != source_ctime_ns
            or linked_after_freeze.st_ctime_ns != source_ctime_ns
            or after_freeze.st_nlink != 1
            or linked_after_freeze.st_nlink != 1
        ):
            raise BitableMediaFileError("media_file_changed")

        def verify_after() -> None:
            stream.seek(0)
            frozen_digest = hashlib.sha256()
            frozen_total = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                frozen_digest.update(chunk)
                frozen_total += len(chunk)
            source_digest, source_checksum, source_sample = _hash_stream(file_fd)
            final = os.stat(relative.parts[1], dir_fd=platform_fd, follow_symlinks=False)
            opened = os.fstat(file_fd)
            if (
                (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != expected
                or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != expected
                or final.st_ctime_ns != source_ctime_ns
                or opened.st_ctime_ns != source_ctime_ns
                or final.st_nlink != 1
                or opened.st_nlink != 1
                or source_digest != candidate.sha256
                or source_checksum != candidate.adler32
                or _detected_media_mime(suffix, source_sample) != candidate.mime_type
                or frozen_total != candidate.size
                or frozen_digest.hexdigest() != candidate.sha256
            ):
                raise BitableMediaFileError("media_file_changed")

        try:
            yield stream
        except BaseException:
            try:
                verify_after()
            except BitableMediaFileError:
                # 已发送请求的异常语义优先，避免把远端不确定误报成本地确定失败。
                pass
            raise
        else:
            verify_after()
    except BitableMediaFileError:
        raise
    except (OSError, ValueError) as exc:
        raise BitableMediaFileError("media_file_changed") from exc
    finally:
        if stream is not None:
            stream.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
