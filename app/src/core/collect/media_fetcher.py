"""Shared managed-media downloads for explicit GUI and CLI collection actions."""
from pathlib import Path
from .media import ManagedMediaFetchError, reliable_media_sources

def media_fetch_error(exc):
    import errno
    from http.client import IncompleteRead
    from urllib.error import HTTPError, URLError
    chain = []
    while exc is not None and len(chain) < 8 and all(exc is not item for item in chain):
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    code = "media_download_failed"
    for error in chain:
        if isinstance(error, ManagedMediaFetchError) and error.reason_code == "media_invalid_file":
            code = "media_invalid_file"
        if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
            code = "media_disk_full"
            break
        if isinstance(error, PermissionError):
            code = "media_permission_denied"
            break
        typed = getattr(error, "code", "")
        if typed == "DOUYIN_NETWORK_ERROR" or isinstance(error, (TimeoutError, IncompleteRead)):
            code = "media_network_error"
        elif isinstance(error, HTTPError):
            if error.code in (401, 403, 404, 410):
                code = "media_source_refresh_failed"
            elif error.code == 429:
                code = "media_rate_limited"
            else:
                code = "media_service_unavailable"
        elif isinstance(error, URLError):
            code = "media_network_error"
        elif typed in {"DOUYIN_ACCESS_RESTRICTED", "DOUYIN_EMPTY_DETAIL"}:
            code = "media_source_refresh_failed"
        elif typed == "MEDIA_REMOTE_CONTENT_INVALID":
            code = "media_source_refresh_failed"
        elif typed == "DOUYIN_RATE_LIMITED":
            code = "media_rate_limited"
    message = {
        "media_disk_full": "磁盘空间不足，请腾出空间后重试下载。",
        "media_permission_denied": "素材目录无法写入，请检查目录权限后重试。",
        "media_network_error": "素材下载连接失败，请检查网络后重试。",
        "media_rate_limited": "平台限制了访问频率，请稍后重试下载。",
        "media_service_unavailable": "平台暂时无法提供素材，请稍后重试。",
        "media_source_refresh_failed": "播放地址不可用，作品页也未能刷新。请重新采集主页后再试。",
        "media_download_failed": "素材下载发生异常，请重试；已有文件保留。",
        "media_invalid_file": "下载文件无法确认，请重试；已有文件保留。",
    }[code]
    return ManagedMediaFetchError(code, message)


def fetch_managed_media(item, download_mode, destination, is_cancelled, *, on_progress=None):
    """生产网络取材边界；只由用户显式素材动作的后台任务调用。"""
    from ..audio.extractor import (
        download_douyin_video_cdn,
        is_douyin_video_cdn,
        resolve_source,
    )
    from .douyin_fetch import fetch_item

    def progress(_message, _fraction):
        if is_cancelled():
            raise InterruptedError("素材下载已取消")
        if on_progress is not None:
            on_progress(_message, _fraction)

    # 主页的「音视频文件」始终代表完整视频。保留入参仅为与
    # ManagedMediaFetcher 协议兼容，不让内容提取页的「仅音频」偏好泄漏到主页。
    _ = download_mode
    root = Path(destination)
    sources = reliable_media_sources(item)
    if not sources:
        raise ManagedMediaFetchError("media_source_unavailable", "这条作品暂时没有可用的音视频来源。")

    video_extensions = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
    last_failure = None
    for index, source in enumerate(sources, start=1):
        if is_cancelled():
            raise InterruptedError("素材下载已取消")
        attempt = root / f"attempt-{index}"
        try:
            attempt.mkdir(mode=0o700, parents=True, exist_ok=False)
            is_douyin_direct = (
                getattr(item, "platform", "") == "douyin"
                and source == str(getattr(item, "play_url", "") or "").strip()
            )
            if is_douyin_direct:
                if not is_douyin_video_cdn(source):
                    continue
                fetched = Path(
                    download_douyin_video_cdn(
                        source,
                        attempt,
                        progress,
                        work_id=str(getattr(item, "work_id", "") or ""),
                    )
                )
            elif (
                getattr(item, "platform", "") == "douyin"
                and source
                == str(getattr(item, "work_url", "") or "").strip()
            ):
                refreshed = fetch_item(str(getattr(item, "work_id", "") or ""), is_cancelled=is_cancelled)
                refreshed_url = str(refreshed.play_url or "").strip()
                if not is_douyin_video_cdn(refreshed_url):
                    continue
                fetched = Path(download_douyin_video_cdn(
                    refreshed_url,
                    attempt,
                    progress,
                    work_id=str(getattr(item, "work_id", "") or ""),
                ))
            else:
                fetched = Path(resolve_source(
                    source,
                    attempt,
                    True,
                    progress,
                    {},
                    link_media_mode="video",
                ))
            if not fetched.is_absolute() or fetched.is_symlink():
                raise ManagedMediaFetchError("media_invalid_file", "下载文件路径无法确认。")
            try:
                fetched.resolve(strict=True).relative_to(attempt.resolve(strict=True))
            except (FileNotFoundError, ValueError) as exc:
                raise ManagedMediaFetchError("media_invalid_file", "下载文件路径无法确认。") from exc
            valid = (
                fetched.is_file()
                and fetched.stat().st_size > 0
                and fetched.suffix.casefold() in video_extensions
            )
            if valid:
                return fetched
            raise ManagedMediaFetchError("media_invalid_file", "下载文件不完整或格式无法确认。")
        except InterruptedError:
            raise
        except Exception as exc:
            failure = media_fetch_error(exc)
            # Keep the existing single refresh for remote failures, including
            # transient connectivity. Local I/O and unknown bugs stop at once.
            if failure.reason_code not in {"media_source_refresh_failed", "media_network_error"}:
                raise failure from exc
            last_failure = failure
            continue
    if last_failure is not None:
        raise last_failure
    raise ManagedMediaFetchError(
        "media_source_refresh_failed",
        "播放地址已过期或不可用，作品页也未能刷新。请重新采集主页后再试。",
    )
