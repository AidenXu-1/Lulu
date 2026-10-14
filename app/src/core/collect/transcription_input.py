"""按采集作品身份衔接批量转录；不丢弃已经取得的公开媒体地址。"""
from pathlib import Path

from .repository import CollectionRepository
from ..audio.extractor import (
    download_douyin_video_cdn, is_douyin_video_cdn,
    _apply_douyin_work_meta,
)
from ..models import TranscriptCover


def download_collected_input(stable_key, work_dir: Path, progress, meta: dict, *, repository=None):
    if not str(stable_key).startswith("douyin:"):
        return None
    try:
        matches = [item for item in (repository or CollectionRepository()).load().items
                   if item.stable_key == stable_key]
    except (OSError, RuntimeError, ValueError):
        return None
    if len(matches) != 1:
        return None
    item = matches[0]
    if item.is_image or not is_douyin_video_cdn(item.play_url):
        return None
    progress("使用已采集的作品地址下载…", -1.0)
    downloaded = download_douyin_video_cdn(
        item.play_url, work_dir, progress, work_id=item.work_id,
    )
    _apply_douyin_work_meta(meta, share_url=item.work_url, play_url=item.play_url,
        facts={"work_id":item.work_id,"create_time":item.create_time,"duration_ms":item.duration_ms},
        fallback_work_id=item.work_id)
    meta.update(title=item.title, uploader=item.author_name,
        author={"name": item.author_name},
        stats={"like":item.like,"favorite":item.collect,"share":item.share,"comment":item.comment})
    meta.update(TranscriptCover(platform="douyin", work_id=item.work_id,
        url=item.cover_url, status="url_only" if item.cover_url else "not_requested").merge_meta(meta))
    return Path(downloaded)
