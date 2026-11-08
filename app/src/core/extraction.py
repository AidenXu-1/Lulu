"""V24 output acquisition using the existing download, cover and transcription services."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import tempfile
import shutil

from .models import Transcript, TranscriptCover, Segment
from . import pipeline
from .audio import extractor
from .collect.preferences import MaterialPreferenceSnapshot
from .updater.secure_io import atomic_write, read_nofollow


def outputs(policy):
    if policy and policy.requested_outputs is not None:
        return policy.requested_outputs
    return ('text',)


def _metadata_download(url, directory, progress, meta, *, link_media_mode='audio'):
    """Metadata only; owned subprocess can be stopped using the existing transport."""
    if extractor.is_douyin(url):
        play, title, stats, author, work_id, cover = extractor._douyin_play_url(url, progress, meta)
        extractor._apply_douyin_work_meta(meta, share_url=url, play_url=play, fallback_work_id=work_id)
        meta.update(title=title, stats=stats, author=author)
        meta.update(TranscriptCover(platform='douyin', work_id=work_id, url=cover,
            status='url_only').merge_meta(meta))
    else:
        import yt_dlp
        from .audio.download_runtime import public_download_options
        options = dict(quiet=True, no_warnings=True, noplaylist=True, skip_download=True,
                       socket_timeout=15, retries=1, extractor_retries=1)
        options = {**public_download_options(url), **options}
        with yt_dlp.YoutubeDL(options) as ydl:
            if 'xiaohongshu.com' in url or 'xhslink.com' in url:
                from .audio.xiaohongshu import LuluXiaoHongShuIE
                ydl.add_info_extractor(LuluXiaoHongShuIE())
            extractor._fill_meta(meta, ydl.extract_info(url, download=False), url)
    path = directory / 'metadata.json'
    path.write_text(json.dumps(meta, ensure_ascii=False))
    return path


def metadata(source, directory, progress, cancelled):
    from .audio.download_process import download_in_process
    value = {}
    download_in_process(extractor.extract_url(source), directory, progress, value,
        is_cancelled=cancelled, timeout=120, _download=_metadata_download)
    return value


def _video_download(url, directory, progress, meta, *, link_media_mode='video'):
    if extractor.is_douyin(url):
        return extractor._download_douyin(url, directory, progress, meta)
    path = extractor._download_via_ytdlp(url, directory, progress, meta, link_media_mode='video')
    if Path(path).suffix.lower() in {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.aiff'}:
        raise ValueError('这个链接仅提供音频，没有视频')
    return path


def download_video(source, directory, progress, meta, cancelled):
    from .audio.download_process import download_in_process
    return download_in_process(extractor.extract_url(source), directory, progress, meta,
        link_media_mode='video', is_cancelled=cancelled, _download=_video_download)


def publish_asset(path, destination):
    """Publish without replacing any existing user file, including under a race."""
    source = Path(path)
    if not source.is_file() or source.is_symlink() or source.stat().st_size <= 0:
        raise OSError('素材文件无效，原文件已保留')
    root = Path(destination).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.lulu-', suffix='.part', dir=root)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, out)
            out.flush(); os.fsync(out.fileno())
        count = 0
        while True:
            target = root / (source.name if count == 0 else f'{source.stem}_{count}{source.suffix}')
            try:
                os.link(staged, target)
                return str(target)
            except FileExistsError:
                count += 1
    finally:
        staged.unlink(missing_ok=True)


def acquire_source(source, *, execution_policy, engine_name, enable_link_download=True,
                   progress=lambda *_:None, is_cancelled=lambda:False,
                   cover_workflow=None, stage_changed=lambda *_:None,
                   bounded_cloud=False, engine_override=None, cloud_observer=None,
                   cloud_force_cancel=None, max_input_bytes=None,
                   asset_changed=None, result_checkpoint=None, stop_new_work=None,
                   stage_progress=None, **unused):
    policy = execution_policy
    chosen = outputs(policy)
    if not chosen:
        raise ValueError('请至少选择一项内容')
    # The cloud result may need saving after ordinary cancellation. Keep that
    # protection separate from admission of subsequent cover network work.
    stop_new_work = stop_new_work or (lambda:False)
    cancelled_cover = '已取消后续封面提取，已有内容保留'
    def cover_cancelled():
        return is_cancelled() or stop_new_work()
    def check():
        if is_cancelled(): raise pipeline._Cancelled()
    def report(message, fraction=-1):
        check(); progress(message, fraction)
    def stage(name):
        check(); stage_changed(name)
    def measured_report(message, fraction=-1):
        if stage_progress is not None:
            stage_progress(message, fraction)
        report(message, fraction)
    check()
    is_link = extractor.extract_url(source) is not None
    if not is_link:
        if chosen != ('text',): raise ValueError('本地文件只支持提取文案')
        return pipeline.transcribe_source(source, engine_name=engine_name,
            execution_policy=policy,
            progress=progress, is_cancelled=is_cancelled, stage_changed=stage_changed,
            stage_progress=stage_progress,
            bounded_cloud=bounded_cloud, engine_override=engine_override,
            cloud_observer=cloud_observer, cloud_force_cancel=cloud_force_cancel,
            max_input_bytes=max_input_bytes)
    if not enable_link_download: raise ValueError('链接下载功能已关闭')
    if ('video' in chosen or 'cover' in chosen) and not policy.media_directory:
        raise ValueError('请先选择素材保存位置')
    transcript = None
    meta = {}
    assets, failures = {}, []
    def asset(key, state, path=None):
        if asset_changed is not None:
            asset_changed(key, state, path)
    def checkpoint():
        if result_checkpoint is not None and transcript is not None:
            result_checkpoint(replace(transcript, source=source,
                meta={**meta, 'extraction_assets':dict(assets),
                      'extraction_outputs':list(chosen), 'extraction_errors':list(failures)}))
    with pipeline._transcription_tempdir() as directory:
        media = None
        if 'video' in chosen:
            asset('video', 'running')
            stage('download'); report('正在下载视频')
            try:
                media = download_video(source, directory, measured_report, meta, is_cancelled)
            except Exception as exc:
                check()
                failures.append(f'视频获取失败：{exc}')
                asset('video', 'failed')
            check()
        if 'text' in chosen:
            asset('text', 'running')
            # Subtitles and selected video are independent products. A subtitle hit
            # cannot short-circuit the video acquisition or its final publication.
            if media is not None and policy.prefer_subtitles:
                from .subtitles import fetch_from_link
                stage('subtitles'); report('读取平台字幕')
                try:
                    transcript = fetch_from_link(source, measured_report)
                except Exception:
                    check()
                    report('字幕暂不可用，继续转录音视频')
                check()
            if transcript is None:
                text_policy = replace(policy, requested_outputs=None,
                    material_snapshot=MaterialPreferenceSnapshot('audio', False, False),
                    media_destination='none', media_directory=None,
                    prefer_subtitles=bool(policy.prefer_subtitles and media is None))
                if policy.stable_key and media is None:
                    # Keep homepage identity, authenticated input recovery and
                    # already downloaded media reuse from the existing pipeline.
                    text_policy=replace(text_policy,origin='homepage_batch_transcribe',
                                        mode='transcription_only',media_destination='temporary_only')
                try:
                    transcript = pipeline.transcribe_source(str(media) if media else source,
                        engine_name=engine_name, execution_policy=text_policy,
                        progress=report, is_cancelled=is_cancelled, stage_changed=stage_changed,
                        stage_progress=stage_progress,
                        bounded_cloud=bounded_cloud, engine_override=engine_override,
                        cloud_observer=cloud_observer, cloud_force_cancel=cloud_force_cancel,
                        max_input_bytes=max_input_bytes)
                except Exception as exc:
                    check()
                    from .engines.cloud_http import CloudFailure
                    if isinstance(exc, CloudFailure):
                        meta['extraction_text_failure'] = exc.public()
                    failures.append(f'文字提取失败：{exc}')
            if transcript is None:
                transcript = Transcript(text='', source=source)

            meta = {**(transcript.meta or {}), **meta}
            if transcript.text.strip():
                asset('text', 'pending_save')
                checkpoint()
            else:
                # Other outputs can continue; report this failure before their
                # next phase so the UI never marks missing text as completed.
                asset('text', 'unknown' if meta.get('extraction_text_failure', {}).get('kind') == 'unknown' else 'failed')
        if transcript is None:
            transcript = Transcript(source=source, engine='', segments=[], text='')
        if 'cover' in chosen and stop_new_work():
            failures.append(cancelled_cover)
        elif 'cover' in chosen:
            asset('cover', 'running')
            stage('cover'); report('获取封面')
            metadata_failed = False
            if not TranscriptCover.from_meta(meta).url:
                try: meta.update(metadata(source, directory, report, cover_cancelled))
                except Exception:
                    check()
                    if not stop_new_work():
                        metadata_failed = True
                        failures.append('封面信息获取失败，请重试')
            check()
            cover = TranscriptCover.from_meta(meta)
            if stop_new_work():
                failures.append(cancelled_cover)
            elif metadata_failed:
                pass
            elif not cover.eligible:
                failures.append('此作品没有可获取的封面')
            elif cover_workflow is None:
                failures.append('封面保存服务暂不可用，请重试')
            else:
                try:
                    receipt = cover_workflow.localize(platform=cover.platform,
                        work_id=cover.work_id, url=cover.url, is_cancelled=cover_cancelled)
                except Exception:
                    check()
                    receipt = None
                check()
                if receipt is None or not pipeline._valid_cover_receipt(cover, receipt):
                    failures.append(cancelled_cover if stop_new_work() else '封面保存失败，请重试')
                else:
                    try:
                        local = Path(cover_workflow.root) / receipt.relative_path
                        assets['cover'] = publish_asset(local, policy.media_directory)
                        asset('cover', 'ready', assets['cover'])
                        meta = TranscriptCover(platform=cover.platform, work_id=cover.work_id,
                            url=cover.url, status='downloaded', local_path=receipt.relative_path).merge_meta(meta)
                    except OSError:
                        failures.append('封面无法保存到所选目录')
            checkpoint()
        if media is not None:
            stage('result'); report('保存视频')
            try:
                assets['video'] = publish_asset(media, policy.media_directory)
                asset('video', 'ready', assets['video'])
            except OSError: failures.append('视频无法保存到所选目录')
            checkpoint()
        check()
        transcript.source = source
        transcript.title = meta.get('title') or transcript.title
        transcript.saved_media = assets.get('video')
        transcript.meta = {**meta, 'extraction_assets': assets,
            'extraction_outputs': list(chosen), 'extraction_errors': failures}
        report('正在保存结果')
        return transcript


def _result_path(job_id, root=None):
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', job_id):
        raise ValueError('任务编号无效')
    if root is None:
        from .models_manager import app_data_dir
        root = app_data_dir() / 'extraction-results'
    return Path(root) / (job_id + '.json')


def save_result(job_id, transcript, root=None):
    path = _result_path(str(job_id), root)
    atomic_write(path, json.dumps({'schema':1, 'job_id':str(job_id),
        'transcript':asdict(transcript)}, ensure_ascii=False).encode())
    load_result(str(job_id), root)
    return 'lulu-extraction:' + str(job_id)


def load_result(job_id, root=None):
    value = json.loads(read_nofollow(_result_path(job_id, root), mode=0o600, maximum=16*1024*1024))
    if value.get('schema') != 1 or value.get('job_id') != job_id:
        raise ValueError('任务结果无法读取，原文件已保留')
    record = value['transcript']
    record['segments'] = [Segment(**s) for s in record['segments']]
    return Transcript(**record)


def has_unsaved_result(task):
    if task.transcript is None or task.library_id:
        return False
    ref = getattr(task, '_extraction_result_ref', None)
    return not (ref and (ref == getattr(task, '_core_result_ref', None)
                        or not hasattr(task, '_core_state')))
