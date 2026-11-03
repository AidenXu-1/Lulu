"""Public operations over the same library, media and voice services as the GUI."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .. import library
from . import documents
from .store import encode, identity

_lock = threading.RLock()


def checked_voice_store():
    from ..synthesis import voices
    from ...config import AppConfig
    voices.configure_voice_library_dir(AppConfig.load().voice_library_dir)
    folder = voices.current_voice_library_dir()
    if any(p.is_symlink() for p in [folder, *folder.parents, voices.INDEX]):
        raise ValueError('音色库位置已变化，未修改资料')
    if voices.INDEX.exists():
        try:
            items = json.loads(voices.INDEX.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            raise ValueError('音色目录无法完整读取，未修改原音色') from None
        if type(items) is not list or any(type(i) is not dict for i in items):
            raise ValueError('音色目录格式异常，未修改原音色')


def actual_file(value, *, maximum=None):
    if type(value) is not str:
        raise ValueError('请选择完整文件位置')
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts
            or any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file()):
        raise ValueError('请选择实际存在的普通文件，暂不支持符号链接')
    if maximum and not 0 < path.stat().st_size <= maximum:
        raise ValueError('文件为空或超过本次操作的大小上限')
    return path


def publish_bytes(store, request_id, target, payload, intent):
    """A durable reservation recovers a committed file after a lost reply."""
    from ..link_extraction_jobs import open_destination, _check_destination
    identity(request_id)
    target = Path(target)
    if not target.is_absolute() or '..' in target.parts or not target.name:
        raise ValueError('请指定完整导出文件位置')
    parent, fd, folder_identity = open_destination(str(target.parent))
    key = 'file-export:' + request_id
    sha = hashlib.sha256(payload).hexdigest()
    intent = documents.digest(encode([intent, str(target), sha]))
    try:
        with _lock, library._transaction():
            old = store.preference(key)
            if old and old['intent'] != intent:
                raise library.RevisionConflict('同一导出编号已有不同内容或位置，未覆盖')
            if target.exists() or target.is_symlink():
                if (not old or target.is_symlink() or not target.is_file()
                        or hashlib.sha256(target.read_bytes()).hexdigest() != sha):
                    raise library.RevisionConflict('导出文件名已存在或内容已变化，请换一个位置')
                return {**old['result'], 'reused': True}
            result = dict(path=str(target), bytes=len(payload), sha256=sha)
            store.preference(key, dict(intent=intent, result=result, state='prepared'))
            temporary = '.lulu-export-' + os.urandom(16).hex()
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check_destination(parent, folder_identity)
                os.link(temporary, target.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.fsync(fd)
                _check_destination(parent, folder_identity)
            finally:
                os.unlink(temporary, dir_fd=fd)
            store.preference(key, dict(intent=intent, result=result, state='committed'))
            return {**result, 'reused': False}
    finally:
        os.close(fd)


class ExpansionOperations:
    def control_poll(self, nonce, reports, snapshot):
        if self.origin != 'gui':
            raise ValueError('仅限真实 Lulu 窗口回执')
        reply = self.runtime.expansion_control().poll(nonce, reports, snapshot)
        active = [j for j in self.runtime._job_control.list() if j.state.value in {'queued', 'running', 'cancel_requested'}]
        core_busy = bool(active)
        models = {j.frozen_policy.get('engine') for j in active if j.job_kind in {
                  'local_transcription', 'local_synthesis', 'workspace_transcription'}}
        batches_busy = any(s.workers for s in getattr(self.runtime, '_expansion_batches', {}).values())
        for service in getattr(self.runtime, '_expansion_batches', {}).values():
            for bid in list(service.workers):
                models.add(service._read(bid).get('engine'))
        return {**reply, 'core_busy': bool(core_busy or batches_busy), 'busy_models': sorted(m for m in models if m)}

    def control_show(self, request_id):
        """Read the actual GUI acknowledgement and final result of a management action."""
        return self.runtime.expansion_control().show(request_id)

    def settings_show(self):
        """Read only non-sensitive preferences and their conflict-detection hash."""
        from .control import SETTINGS
        from ...config import AppConfig
        cfg = AppConfig.load()
        settings = {key: getattr(cfg, key) for key in SETTINGS}
        return dict(settings=settings, settings_hash=documents.digest(encode(settings)))

    def settings_apply(self, request_id, changes, expected_hash):
        """Apply listed preferences in the running window so in-memory settings agree."""
        from .control import validate_settings
        validate_settings(changes)
        return self.runtime.expansion_control().submit(request_id, 'settings', 'apply',
            dict(changes=changes, expected_hash=expected_hash))

    def models_list(self):
        """Read ASR/TTS readiness, runtime state and current/partial disk usage."""
        from .. import models_manager as mm
        from ..local_transcription_models import model_status
        from ..engines.local_models import LOCAL_MODELS
        from ..synthesis import runtime_packages as rp
        models = [dict(model_status(mid), id=mid, kind='transcription', bytes=mm.on_disk_size(mid),
                       partial_bytes=mm.resumable_download_size(mid)) for mid in LOCAL_MODELS]
        models += [dict(id=mid, name=spec.display_name, kind='speech', state=rp.status(mid),
                        bytes=rp.on_disk_size(mid), partial_bytes=rp.partial_install_bytes(mid),
                        runtime_installed=rp.is_runtime_installed(mid), weights_installed=rp.is_weights_installed(mid))
                   for mid, spec in rp.TTS_RUNTIMES.items()]
        return dict(models=models, models_directory=str(mm.models_root()),
                    actions=['install', 'pause', 'resume', 'repair', 'remove', 'default', 'migrate'])

    def models_action(self, request_id, action, model_id=None, confirmed=False, destination=None, local_archive=None, feature=None):
        """Use the same GUI model operations, gates, trusted sources and resume cache."""
        from ..engines.local_models import LOCAL_MODELS
        from ..synthesis.runtime_packages import TTS_RUNTIMES
        if action not in {'install', 'pause', 'resume', 'repair', 'remove', 'default', 'migrate'}:
            raise ValueError('模型操作无效')
        if action == 'migrate':
            from ..live_recording.index import directory
            directory(destination)
            if model_id is not None or local_archive is not None:
                raise ValueError('搬移会同时处理现有转录和配音模型，请省略单模型参数')
        elif model_id not in LOCAL_MODELS and model_id not in TTS_RUNTIMES:
            raise ValueError('请选择现有本地模型编号')
        if feature is not None and (model_id != 'local_qwen3_tts' or feature not in {'clone', 'preset', 'design'}):
            raise ValueError('声音能力参数只适用于千问的 clone/preset/design')
        if model_id == 'local_qwen3_tts' and action != 'default' and feature is None:
            raise ValueError('千问请明确指定 clone/preset/design 声音能力，保留共用文件和其他能力')
        if action in {'install', 'resume', 'repair', 'remove', 'migrate'} and confirmed is not True:
            from ..transcription_job_application import LocalTranscriptionJobError
            raise LocalTranscriptionJobError('CONFIRMATION_REQUIRED', 'MODEL_ACTION_CONFIRMATION_REQUIRED')
        if local_archive:
            actual_file(local_archive)
        return self.runtime.expansion_control().submit(request_id, 'models', action,
            dict(model_id=model_id, destination=destination, local_archive=local_archive, feature=feature))

    def updates(self, action='status', request_id=None, confirmed=False):
        """Query/check/download/verify/stage/install via the existing GUI updater."""
        service = self.runtime.expansion_control()
        if action == 'status':
            return dict(service.snapshot.get('updates') or {},
                        gui_connected=bool(service.nonce and time.monotonic() < service.deadline))
        if action not in {'check', 'download', 'verify', 'stage', 'install', 'cancel'}:
            raise ValueError('软件更新操作无效')
        if action == 'install' and confirmed is not True:
            from ..transcription_job_application import LocalTranscriptionJobError
            raise LocalTranscriptionJobError('CONFIRMATION_REQUIRED', 'UPDATE_INSTALL_CONFIRMATION_REQUIRED')
        return service.submit(request_id, 'updates', action, {})

    def batch_prepare(self, request_id, items, engine='local_sensevoice_q8', folder_id=None,
                      save_media=False, media_directory=None, prefer_subtitles=False, cloud_concurrency=None, cloud_limits=None):
        """Prepare a single link or explicit ordered batch without starting downloads."""
        return self.runtime.expansion_batches().prepare(request_id, items, engine, folder_id,
            save_media, media_directory, prefer_subtitles, cloud_concurrency=cloud_concurrency,cloud_limits=cloud_limits)

    def batch_stage(self, request_id, items=None, engine='local_sensevoice_q8', folder_id=None,
                    name=None, group=False, media_directory=None, skipped=None,
                    items_file=None, items_sha256=None):
        """Stage per-work extraction choices without loading models or uploading."""
        if items_file is not None:
            if items is not None:
                raise ValueError('内联作品与清单文件只能选择一种')
            from .extraction_batches import read_item_file
            items=read_item_file(items_file,items_sha256)
        elif items_sha256 is not None:
            raise ValueError('清单摘要必须同时提供清单文件')
        return self.runtime.expansion_batches().stage_extraction(request_id, items, engine,
            folder_id, name, group, media_directory, skipped)

    def batch_configure(self, request_id, batch_id, expected_hash, changes=None, engine=None):
        """Edit only the explicit unstarted work versions, or an unstarted batch model."""
        return self.runtime.expansion_batches().configure_extraction(request_id, batch_id,
            expected_hash, changes, engine)

    def batch_control(self, request_id, batch_id, expected_hash, action, item_indices=None,
                      expected_versions=None, network_consent=False, cloud_upload_consent=False,
                      retry_unknown_confirmed=False, excluded_indices=None, dismissal_id=None,
                      expected_visibility=None):
        """Control selected works over the same local/parallel cloud scheduler."""
        return self.runtime.expansion_batches().control_extraction(request_id, batch_id,
            expected_hash, action, item_indices, expected_versions, network_consent,
            cloud_upload_consent, retry_unknown_confirmed, excluded_indices, dismissal_id, expected_visibility)

    def batch_start(self, batch_id, expected_hash, network_consent=False, resume=False, cloud_upload_consent=False,retry_unknown=False):
        """Start or explicitly resume a saved batch; local inference remains sequential."""
        return self.runtime.expansion_batches().start(batch_id, expected_hash, network_consent, resume, cloud_upload_consent,retry_unknown)

    def batch_download(self, request_id, items, destination, outputs='video'):
        """Prepare model-free batch downloads using the existing extraction service."""
        return self.runtime.expansion_batches().prepare_download(request_id, items, destination, outputs)

    def batch_show(self, batch_id,offset=0,limit=None,include_hidden=False,locate_index=None):
        """Read overall progress and complete per-item results, failures and saved documents."""
        return self.runtime.expansion_batches().show(batch_id,offset,limit,include_hidden,locate_index)

    def batch_list(self):
        """List persistent batches, including interrupted batches from an earlier run."""
        return dict(batches=self.runtime.expansion_batches().list())

    def batch_cancel(self, batch_id,force=False):
        """Stop new items and cancel the current item while preserving prior results."""
        return self.runtime.expansion_batches().cancel(batch_id,force)

    def rss_fetch(self, source, limit=300):
        """Fetch an RSS program using the GUI parser and freeze its episode choices."""
        from ..podcast import fetch_feed
        from ..link_transcription_application import canonicalize_public_url
        source = canonicalize_public_url(source)
        if type(limit) is not int or not 1 <= limit <= 300:
            raise ValueError('每次最多读取 300 集')
        feed = fetch_feed(source, limit=limit)
        if feed is None:
            raise ValueError('未读到可用的播客 RSS，请检查订阅链接或网络')
        value = dict(feed_id='feed-' + documents.digest(source), source=source, title=feed.title,
                     episodes=[dict(asdict(e), episode_id='episode-' + documents.digest(encode([source, e.audio_url]))) for e in feed.episodes])
        # Snapshot identity keeps selections stable if the feed changes later.
        value['snapshot_id'] = documents.digest(encode(value))
        self.store.preference('rss-snapshot:' + value['snapshot_id'], value)
        feeds = self.store.preference('rss-feeds') or {}
        feeds[value['feed_id']] = value
        self.store.preference('rss-feeds', feeds)
        return value

    def rss_feeds(self, feed_id=None):
        """List previously fetched programs or the saved episode list of one program."""
        feeds = self.store.preference('rss-feeds') or {}
        if feed_id is None:
            return dict(feeds=[dict(feed_id=f['feed_id'], title=f['title'], source=f['source'],
                                    episode_count=len(f['episodes']), snapshot_id=f['snapshot_id']) for f in feeds.values()])
        if feed_id not in feeds:
            raise FileNotFoundError(feed_id)
        return feeds[feed_id]

    def rss_select(self, snapshot_id, episode_ids, request_id, engine='local_sensevoice_q8', folder_id=None):
        """Prepare only explicitly selected episodes; no implicit select-all."""
        value = self.store.preference('rss-snapshot:' + snapshot_id)
        if not value:
            raise ValueError('RSS 节目快照不存在，请重新读取节目列表')
        if (type(episode_ids) is not list or not episode_ids or len(set(episode_ids)) != len(episode_ids)
                or any(type(e) is not str for e in episode_ids)):
            raise ValueError('请提供明确且不重复的单集编号')
        by_id = {e['episode_id']: e for e in value['episodes']}
        if any(e not in by_id for e in episode_ids):
            raise ValueError('所选单集不属于这一节目快照')
        return self.batch_prepare(request_id, [dict(source=by_id[e]['audio_url'], title=by_id[e]['title'])
            for e in episode_ids], engine, folder_id)

    def collect_prepare(self, request_id, source, mode='anonymous_latest', limit=20, browser_mode='efficiency'):
        """Prepare existing Douyin homepage collection, including its partial-result limits."""
        return self.runtime.expansion_batches().prepare_collection(request_id, source, mode, limit, browser_mode)

    def collect_history(self, record_id=None):
        """Read shared GUI/CLI collection history and full item identities."""
        from ..collect.history import CollectionHistoryRepository
        snapshot = CollectionHistoryRepository().load()
        records = snapshot.active_records
        if record_id is not None:
            record = next((r for r in records if r.record_id == record_id), None)
            if record is None:
                raise FileNotFoundError(record_id)
            return record.to_dict()
        return dict(records=[dict(record_id=r.record_id, name=r.name, completion=r.completion,
            reason_code=r.reason_code, count=len(r.items)) for r in records])

    def collect_select(self, record_id, item_keys, request_id, engine='local_sensevoice_q8',
                       folder_id=None, save_media=False, media_directory=None, operation='transcribe', outputs='video'):
        """Prepare selected collected videos; report image/article skips explicitly."""
        from ..collect.history import CollectionHistoryRepository
        record = next((r for r in CollectionHistoryRepository().load().active_records if r.record_id == record_id), None)
        if record is None:
            raise FileNotFoundError(record_id)
        if type(item_keys) is not list or not item_keys or any(type(k) is not str for k in item_keys):
            raise ValueError('请选择明确的作品编号')
        if len(set(item_keys)) != len(item_keys):
            raise ValueError('作品编号重复')
        by_key = {i.stable_key: i for i in record.items}
        if any(k not in by_key for k in item_keys):
            raise ValueError('所选作品不属于这一采集记录')
        selected, skipped = [], []
        from ..collect.repository import CollectionRepository
        repository = CollectionRepository()
        present = {i.stable_key for i in repository.load().items}
        missing = [by_key[k] for k in item_keys if k not in present]
        if missing:
            repository.upsert_collected(missing)
        from ..collect.models import DOUYIN_NON_VIDEO_TYPES
        for key in item_keys:
            item = by_key[key]
            if item.content_kind != 'video' or item.aweme_type in DOUYIN_NON_VIDEO_TYPES:
                skipped.append(dict(key=key, state='skipped', message='图文或文章不提取背景音乐'))
            else:
                selected.append(dict(source=item.work_url, title=item.title or item.work_id, stable_key=item.stable_key))
        if not selected:
            return dict(state='skipped', skipped=skipped, message='所选条目没有音视频作品')
        if operation == 'download':
            result = self.batch_download(request_id, selected, media_directory, outputs)
        elif operation == 'transcribe':
            result = self.batch_prepare(request_id, selected, engine, folder_id, save_media, media_directory)
        else:
            raise ValueError('请选择 transcribe 或 download')
        return {**result, 'skipped': skipped}

    def collect_export(self, record_id, format, path, request_id):
        """Export the selected history record to CSV/Excel with saved transcript text."""
        from ..collect.history import CollectionHistoryRepository
        from ..collect.export import write_csv, write_xlsx, format_transcript_for_export
        record = next((r for r in CollectionHistoryRepository().load().active_records if r.record_id == record_id), None)
        if record is None:
            raise FileNotFoundError(record_id)
        if format not in {'csv', 'xlsx'} or Path(path).suffix.lower() != '.' + format:
            raise ValueError('请选择 CSV 或 Excel 文件位置')
        transcripts = {}
        for entry in library.list_entries():
            transcript = library.load(entry['id'])
            if transcript is not None:
                for item in record.items:
                    if item.work_url == transcript.source:
                        transcripts[item.stable_key] = format_transcript_for_export(transcript)
        with tempfile.TemporaryDirectory(prefix='lulu-collection-export-') as folder:
            output = Path(folder) / ('collection.' + format)
            (write_csv if format == 'csv' else write_xlsx)(record.items, output, transcripts=transcripts)
            return publish_bytes(self.store, request_id, path, output.read_bytes(), [record.to_dict(), format])

    def order_folders(self, ordered_ids):
        """Save the explicit complete folder order; no document text is changed."""
        if type(ordered_ids) is not list or any(type(i) is not str for i in ordered_ids):
            raise ValueError('请提供文件夹编号列表')
        with library._transaction():
            existing = {f['id'] for f in library._load_folders()}
            if len(ordered_ids) != len(set(ordered_ids)) or not set(ordered_ids) <= existing:
                raise ValueError('文件夹编号重复或已不存在，请重新读取文件夹列表')
            library.set_folder_order(ordered_ids)
        return dict(ordered_ids=ordered_ids)

    def export_document(self, document_id, format, path, request_id):
        """Export complete current text to txt/md/srt; never invent subtitle times."""
        from ..output.writer import render
        if format not in {'txt', 'md', 'srt'} or Path(path).suffix.lower() != '.' + format:
            raise ValueError('导出格式与文件后缀应为 txt、md 或 srt')
        with library._transaction():
            doc = documents.snapshot(document_id)
            transcript = library.load(document_id)
            if transcript is None:
                raise FileNotFoundError(document_id)
            if format == 'srt' and (transcript.meta or {}).get('segments_stale'):
                raise ValueError('正文已有编辑，原时间信息已失效，请导出 TXT 或 Markdown')
            content = render(transcript, format) if format == 'srt' else (
                doc['text'].rstrip('\n') + '\n' if format == 'txt' else doc['text'])
        return publish_bytes(self.store, request_id, path, content.encode('utf-8'),
                             [document_id, doc['revision'], doc['content_hash'], format])

    def save_obsidian(self, document_id, request_id, folder=None):
        """Save full current document and source information without duplicate notes."""
        from ...config import AppConfig
        from ..obsidian import build_note, _safe_name
        folder = folder or AppConfig.load().obsidian_dir
        if not folder:
            raise ValueError('请指定 Obsidian 目标文件夹，或先在 Lulu 设置保存位置')
        with library._transaction():
            doc = documents.snapshot(document_id)
            transcript = library.load(document_id)
            if transcript is None:
                raise FileNotFoundError(document_id)
            content = build_note(transcript, doc['title'], text_override=doc['text'])
        name = _safe_name(doc['title']) + '-' + documents.digest(document_id)[:8] + '.md'
        # Identical document/version/destination reuses a durable export identity,
        # including when an external caller chooses a new request number.
        identity(request_id)
        legacy = 'obsidian-' + documents.digest(encode([document_id, folder, doc['content_hash']]))
        stable = 'obsidian-' + documents.digest(encode([document_id, str(Path(folder)),
            doc['revision'], doc['content_hash'], doc['title']]))
        intent = [document_id, doc['revision'], doc['content_hash'], 'obsidian']
        with _lock, library._transaction():
            for key in (legacy, stable):
                old = self.store.preference('file-export:' + key)
                if not old:
                    continue
                target = Path(old['result']['path'])
                if target.parent != Path(folder):
                    raise library.RevisionConflict('已有笔记的目标位置已变化，未覆盖')
                expected = documents.digest(encode([intent, str(target), old['result']['sha256']]))
                if old['intent'] != expected:
                    continue  # A legacy receipt may describe the previous revision.
                payload = content.encode('utf-8')
                if target.exists() or target.is_symlink():
                    # Reuse this version's actual note, including its original
                    # export date. Never replace a note edited outside Lulu.
                    payload = actual_file(str(target)).read_bytes()
                    if hashlib.sha256(payload).hexdigest() != old['result']['sha256']:
                        raise library.RevisionConflict('已有笔记已被修改，原文件保留，请选择其他位置')
                return publish_bytes(self.store, key, str(target), payload, intent)
            target = Path(folder) / name
            if target.exists() or target.is_symlink():
                target = target.with_name(target.stem + '-r' + str(doc['revision']) +
                                          '-' + doc['content_hash'][:12] + '.md')
            return publish_bytes(self.store, stable, str(target), content.encode('utf-8'), intent)

    def voice_list(self):
        """List local reference voices and their model identities."""
        from ..synthesis.voices import list_voices
        with _lock:
            checked_voice_store()
            return dict(voices=[asdict(v) for v in list_voices()])

    def voice_create(self, request_id, name, source_path, reference_text='', engine='local_voxcpm2'):
        """Copy an explicit reference recording into the existing voice library."""
        from ..synthesis import voices
        from ..synthesis.cli_options import MODELS
        identity(request_id)
        if engine not in MODELS or type(name) is not str or not 1 <= len(name.strip()) <= 100:
            raise ValueError('请填写有效音色名称和本地配音模型')
        if type(reference_text) is not str or len(reference_text) > 100000:
            raise ValueError('参考文字过长或无效')
        source = actual_file(source_path, maximum=100 * 1024**2)
        from ..live_recording.media import bundled_ffmpeg, probe
        info = probe(source, bundled_ffmpeg())
        if not info['audio']:
            raise ValueError('参考文件没有可用声音')
        intent = documents.digest(encode([name, info['sha256'], reference_text, engine]))
        with _lock:
            checked_voice_store()
            key = 'voice-create:' + request_id
            old = self.store.preference(key)
            if old:
                if old['intent'] != intent:
                    raise library.RevisionConflict('此音色创建编号已有不同输入')
                voice = voices.get_voice(old['voice_id'])
                if voice is None:
                    raise ValueError('已创建的音色后来被删除，未重复导入')
            else:
                # A marker in existing metadata allows recovery after an index
                # commit but before the request receipt could be saved.
                voice = next((v for v in voices.list_voices() if (v.meta or {}).get('cli_import') == request_id), None)
                if voice is None:
                    voice = voices.add_voice(name, source_path, reference_text, engine=engine,
                                             meta=dict(cli_import=request_id, cli_intent=intent))
                elif voice.meta.get('cli_intent') != intent:
                    raise library.RevisionConflict('此音色创建编号已有不同输入')
                self.store.preference(key, dict(intent=intent, voice_id=voice.id))
            return asdict(voice)

    def voice_update(self, voice_id, name=None, reference_text=None):
        """Rename a voice or correct reference words without changing its audio."""
        from ..synthesis.voices import update_voice
        if not re.fullmatch(r'[a-f0-9]{12}', voice_id):
            raise ValueError('音色编号无效')
        if name is not None and (type(name) is not str or not 1 <= len(name.strip()) <= 100):
            raise ValueError('音色名称无效')
        if reference_text is not None and (type(reference_text) is not str or len(reference_text) > 100000):
            raise ValueError('参考文字无效')
        with _lock:
            checked_voice_store()
            return asdict(update_voice(voice_id, name=name, reference_text=reference_text))

    def voice_delete(self, voice_id, confirmed=False):
        """Delete one confirmed local voice; preserve historical generated audio."""
        from ..synthesis import voices
        if confirmed is not True or not re.fullmatch(r'[a-f0-9]{12}', voice_id):
            raise ValueError('请明确确认删除的音色编号')
        with _lock:
            checked_voice_store()
            voice = voices.get_voice(voice_id)
            if voice is None:
                return dict(voice_id=voice_id, deleted=True, reused=True)
            folder = voices.current_voice_library_dir() / voice_id
            if any(p.is_symlink() for p in [folder, *folder.parents]):
                raise ValueError('音色文件夹已变化，未删除')
            if self.runtime and any(j.state.value in {'prepared', 'queued', 'running', 'cancel_requested'}
                    and j.job_kind == 'local_synthesis' for j in self.runtime._job_control.list()):
                raise ValueError('仍有配音任务正在使用音色，请结束后再删除')
            voices.delete_voice(voice_id)
        return dict(voice_id=voice_id, deleted=True, historical_audio_preserved=True)

    def live(self, action, arguments=None):
        """Control live recording inside the real running Lulu GUI session."""
        if self.runtime is None:
            raise ValueError('请通过正式 Lulu CLI 使用直播')
        service = self.runtime.live_recording()
        if not service._can_run(service.session):
            from ..transcription_job_application import LocalTranscriptionJobError
            raise LocalTranscriptionJobError('HUMAN_ACTION_REQUIRED', 'LULU_LIVE_SESSION_REQUIRED')
        return service.handle_cli(action, arguments or {})
