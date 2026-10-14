"""Acquire independent public-link products using Lulu's existing transports."""
import json
from pathlib import Path
import subprocess
import time

from .audio.download_runtime import DownloadFailure
from .models import TranscriptCover

OUTPUTS = ('video', 'audio', 'cover', 'data')
CAPABILITIES = {f'EXTRACT-{i:02d}': kind for i, kind in enumerate(OUTPUTS, 1)}
ERROR_MESSAGES = {
    'LINK_EXTRACTION_RUNTIME_UNAVAILABLE': '当前安装的 Lulu 尚未接通 CLI 作品链接提取。',
    'EXTRACT_OPTIONS_INVALID': '请指定作品链接、所需内容和保存目录；执行时须带计划摘要及请求编号。',
    'EXTRACT_OUTPUTS_INVALID': '可选择 video、audio、cover、data，逗号组合时不要重复。',
    'EXTRACT_DESTINATION_INVALID': '请选择当前用户可写的现有绝对目录，不能使用软链接目录。',
    'EXTRACT_PLAN_CHANGED': '链接、所需内容或保存目录已改变，请重新生成提取计划。',
    'EXTRACT_REQUEST_CHANGED': '该请求编号已用于其他提取计划，请先核对原任务。',
    'EXTRACT_REQUEST_ID_INVALID': '请求编号须为 8–128 位字母、数字、下划线或短横线。',
    'EXTRACT_DESTINATION_CHANGED': '保存目录已被移动或替换，请核对原任务和目录。',
    'EXTRACT_WORKER_START_FAILED': '提取任务未能启动，请先检查任务状态。',
    'EXTRACT_RESULT_NOT_READY': '提取结果尚未就绪，请查询原任务进展。',
    'EXTRACT_OFFSET_INVALID': '结果读取位置无效，请按上次返回的实际字节数继续读取。',
}


def _audio_download(url, directory, progress, meta, *, link_media_mode='audio'):
    from .audio import extractor
    if extractor.is_douyin(url):
        return extractor._download_douyin(url, directory, progress, meta)
    return extractor._download_via_ytdlp(url, directory, progress, meta, link_media_mode='audio')


def separate_audio(source, target, cancelled):
    """Produce an actual audio-only M4A without reducing it to ASR quality."""
    from .audio.converter import _ffmpeg_exe
    command = [_ffmpeg_exe(), '-nostdin', '-v', 'error', '-y', '-i', str(source),
               '-map', '0:a:0', '-vn', '-c:a', 'aac', '-b:a', '192k', str(target)]
    started = time.monotonic()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            while process.poll() is None:
                if cancelled():
                    raise InterruptedError('音频提取已取消')
                if time.monotonic() - started > 1800:
                    raise DownloadFailure('CORE_DOWNLOAD_NETWORK', '音频提取超时，请重试。')
                time.sleep(.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if process.returncode or not target.is_file() or not target.stat().st_size:
            raise DownloadFailure('CORE_DOWNLOAD_NO_AUDIO', '没有取得可用音轨。')
    return target


def work_data(meta, source, observed_at):
    """Only allow public facts; never export downloader cookies or raw info."""
    cover = TranscriptCover.from_meta(meta)
    stats = meta.get('stats') if isinstance(meta.get('stats'), dict) else {}
    author = meta.get('author') if isinstance(meta.get('author'), dict) else {}
    fields = dict(title=meta.get('title'), description=meta.get('description'),
        author={key: author.get(key) for key in ('name', 'fans')},
        published_at=meta.get('upload_date') or meta.get('create_time'), duration_seconds=meta.get('duration'),
        platform=cover.platform or None, work_id=cover.work_id or None,
        stats={key: stats.get(key) for key in ('view', 'like', 'comment', 'favorite', 'share', 'coin')})
    if not fields['author']['name']:
        fields['author']['name'] = meta.get('uploader')
    missing = [key for key, value in fields.items() if value is None]
    missing += [f'{group}.{key}' for group in ('author', 'stats')
                for key, value in fields[group].items() if value is None]
    return dict(schema_id='lulu.public-work/v1', source_url=source, observed_at=observed_at,
                **fields, missing_fields=missing, data_scope='anonymous_public_snapshot')


def acquire(source, chosen, root, progress, cancelled, observed_at):
    """Return staged paths and per-item failures; caller owns publication."""
    from . import extraction
    from .audio.download_process import download_in_process
    files, errors, meta = {}, {}, {}
    def check():
        if cancelled():
            raise InterruptedError('作品提取已取消')
    def attempt(kind, action):
        check()
        progress(kind)
        try:
            files[kind] = action()
        except Exception as exc:
            check()
            failure = DownloadFailure.from_exception(exc)
            errors[kind] = dict(code=failure.code, message=str(failure))
        check()
    report = lambda *_: check()
    if 'video' in chosen:
        attempt('video', lambda: extraction.download_video(source, root / 'video', report, meta, cancelled))
    if 'audio' in chosen:
        def audio():
            media = files.get('video')
            if media is None:
                media = download_in_process(source, root / 'audio', report, meta,
                    is_cancelled=cancelled, _download=_audio_download)
            return separate_audio(media, root / 'audio.m4a', cancelled)
        attempt('audio', audio)
    if 'cover' in chosen or 'data' in chosen:
        # A successful media download normally already populated the facts.
        metadata_error = None
        if not meta or ('cover' in chosen and not TranscriptCover.from_meta(meta).url):
            try:
                meta.update(extraction.metadata(source, root / 'metadata', report, cancelled))
            except Exception as exc:
                check()
                metadata_error = DownloadFailure.from_exception(exc)
        if 'cover' in chosen:
            def cover_file():
                from .collect.cover import CoverLocalizationService
                from .pipeline import _valid_cover_receipt
                cover = TranscriptCover.from_meta(meta)
                if not cover.eligible:
                    raise metadata_error or DownloadFailure('EXTRACT_COVER_UNAVAILABLE', '此作品没有可获取的封面。')
                service = CoverLocalizationService(root / 'covers')
                receipt = service.localize(platform=cover.platform, work_id=cover.work_id,
                    url=cover.url, is_cancelled=cancelled)
                if not _valid_cover_receipt(cover, receipt):
                    raise DownloadFailure('EXTRACT_COVER_FAILED', '封面获取失败，请稍后重试。')
                return service.root / receipt.relative_path
            attempt('cover', cover_file)
        if 'data' in chosen:
            def data_file():
                if not meta:
                    raise metadata_error or DownloadFailure('EXTRACT_DATA_UNAVAILABLE', '没有取得作品数据。')
                path = root / 'data.json'
                path.write_text(json.dumps(work_data(meta, source, observed_at), ensure_ascii=False, indent=2), encoding='utf-8')
                return path
            attempt('data', data_file)
    return files, errors
