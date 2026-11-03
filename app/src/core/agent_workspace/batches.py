"""Durable sequential batches using the existing download and ASR pipeline."""
from dataclasses import asdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time

from .. import library
from ..models import Transcript, Segment
from ..local_transcription_models import normalize_engine
from . import documents
from .operations import actual_file
from .store import encode, identity, workspace_root

from ..engines.cloud_requests import cloud_ids
from ..engines.cloud_http import CloudFailure
from ..engines.cloud_limits import RESOURCES, policy_for
from .extraction_batches import ExtractionBatchMixin

_FIXED_ITEM_FIELDS = frozenset({'index','source','title','stable_key','local_source','duplicate_of'})


class BatchService(ExtractionBatchMixin):
    def __init__(self, runtime, runner=None, collector=None):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.workers, self.cancels, self.forces, self.halts = {}, {}, {}, {}
        self._active_extractions, self._extraction_cancel_events = {}, {}
        self._progress_times = {}
        self.default_runner = runner is None
        self.runner = self._transcribe if runner is None else runner
        self.collector = collector
        # Batches remain attached to their original library, even after a switch.
        self.library_root = str(library._dir())
        self.root = workspace_root() / 'batches'
        if self.root.is_symlink():
            raise ValueError('批次目录不安全')
        self.root.mkdir(mode=0o700, exist_ok=True)

    def _same_library(self):
        if str(library._dir()) != self.library_root:
            raise library.RevisionConflict('资料目录已切换，请回原目录查询或恢复本批次')

    def _path(self, bid):
        identity(bid)
        if not bid.startswith(('batch-', 'extract-')):
            raise ValueError('批次编号无效')
        path = self.root / (bid + '.json')
        if path.is_symlink():
            raise ValueError('批次记录路径不安全')
        return path

    def _state_path(self, bid, index):
        path = self.root / (bid+'-'+str(index)+'.state.json')
        if path.is_symlink():
            raise ValueError('批次进度位置不安全，未覆盖')
        return path

    def _save(self, batch, item=None):
        # Per-item progress is journaled separately: 30/3000 updates do not
        # repeatedly rewrite all source descriptions. Start/end compact once.
        bid = batch['batch_id']
        if item is not None:
            value = {k:v for k,v in item.items() if k not in _FIXED_ITEM_FIELDS}
            library._write_atomic_text(self._state_path(bid,item['index']),
                encode(dict(plan_hash=batch['plan_hash'], value=value)))
            return
        paths = [self._state_path(bid,i['index']) for i in batch['items']]
        library._write_atomic_text(self._path(bid), encode(batch))
        for path in paths:
            if path.exists():
                path.unlink()

    def _read(self, bid):
        batch = json.loads(self._path(bid).read_text(encoding='utf-8'))
        if type(batch) is not dict or batch.get('batch_id') != bid or batch.get('library_root') != self.library_root:
            raise ValueError('批次记录已变化，原资料保留')
        frozen = batch.get('plan')
        if type(frozen) is not dict or documents.digest(encode(frozen)) != batch.get('plan_hash'):
            raise ValueError('批次计划校验失败，未启动或恢复')
        if any(batch.get(k) != v for k, v in frozen.items() if k != 'items'):
            raise ValueError('批次输入已变化，请重新准备')
        if batch['kind'] == 'transcription' and (len(batch['items']) != len(frozen['items']) or any(
                any(actual.get(k) != planned.get(k) for k in ('index', 'source', 'title', 'stable_key', 'local_source', 'duplicate_of'))
                for actual, planned in zip(batch['items'], frozen['items']))):
            raise ValueError('批次成员已变化，请重新准备')
        for item in batch['items']:
            path = self._state_path(bid,item['index'])
            if path.exists():
                saved = json.loads(path.read_text(encoding='utf-8'))
                if (saved.get('plan_hash') != batch['plan_hash'] or type(saved.get('value')) is not dict
                        or set(saved['value']) & _FIXED_ITEM_FIELDS):
                    raise ValueError('批次进度与原计划不一致，未恢复')
                item.update(saved['value'])
        if (batch.get('engine') in cloud_ids() and not frozen.get('cloud_tracking_version')
                and batch['state'] != 'prepared'):
            for item in batch['items']:
                if (item['state'] in {'running','failed','cancelled','recovery_required'}
                        and not item.get('cloud_attempt') and not self._result_path(bid,item['index']).exists()):
                    item.update(cloud_attempt={'state':'unknown','legacy':True},phase='unconfirmed',
                        message='旧版未记录请求是否受理，可能已计费；普通恢复不重新上传')
        return batch

    def prepare(self, request_id, items, engine, folder_id=None, save_media=False, media_directory=None,
                prefer_subtitles=False, operation='transcribe', outputs='video', cloud_concurrency=None, cloud_limits=None,
                extraction_options=None):
        """Freeze explicit ordered sources and engine. Preparing performs no download."""
        self._same_library()
        identity(request_id)
        if len(request_id) > 100 or type(items) is not list or not items:
            raise ValueError('请明确选择本批音视频')
        from ..engines.providers import PRESETS
        if getattr(PRESETS.get(engine), 'retired', False):
            raise ValueError('此云端转录服务已移除，请选择其他服务或本地模型；原结果保留。')
        if operation not in {'transcribe', 'download'} or outputs not in {'video', 'audio'}:
            raise ValueError('批次操作或下载内容无效')
        cloud = engine in cloud_ids()
        if cloud_concurrency is not None and (type(cloud_concurrency) is not int or cloud_concurrency <= 0):
            raise ValueError('并发应留空使用自动调度，或填写正整数')
        if not cloud and cloud_concurrency not in {None,1}:
            raise ValueError('本地转录保持逐条执行')
        if cloud:
            policy_for(engine, overrides=cloud_limits)
        elif cloud_limits is not None:
            raise ValueError('本地模型不使用云端额度')
        if operation == 'transcribe' and not cloud:
            normalize_engine(engine)
        if type(save_media) is not bool or type(prefer_subtitles) is not bool:
            raise ValueError('保存媒体和字幕选项无效')
        if save_media:
            from ..live_recording.index import directory
            directory(media_directory)
        elif media_directory is not None:
            raise ValueError('未选择保存媒体，请省略媒体目录')
        if folder_id is not None and folder_id not in {f['id'] for f in library._load_folders()}:
            raise ValueError('目标文件夹不存在')
        sources, seen = [], {}
        collected_by_key = None
        from ..audio.extractor import extract_url
        from ..link_transcription_application import canonicalize_public_url
        for number, item in enumerate(items):
            if type(item) is not dict or set(item) - {'source', 'title', 'stable_key'} or type(item.get('source')) is not str:
                raise ValueError('每项只接受 source、可选的 title 和采集作品 stable_key')
            raw = item['source']
            link = extract_url(raw)
            if operation == 'download' and not link:
                raise ValueError('下载批次只接受作品或音视频直链')
            source = canonicalize_public_url(link) if link else str(actual_file(raw, maximum=2*1024**3))
            stable_key = item.get('stable_key') or ''
            if type(stable_key) is not str:
                raise ValueError('采集作品编号无效')
            if stable_key:
                from ..collect.repository import CollectionRepository
                from ..collect.router import detect_link
                if collected_by_key is None:
                    collected_by_key = {i.stable_key:i for i in CollectionRepository().load().items}
                collected = collected_by_key.get(stable_key)
                if collected is None or collected.is_image or not collected.work_url or not link:
                    raise ValueError('采集作品编号与原链接不一致，未准备下载或转录')
                recorded_source = canonicalize_public_url(collected.work_url)
                if collected.platform == 'douyin':
                    # Desktop and share URLs identify the same work. Validate
                    # both against its saved ID; never trust URL substrings.
                    identities = (detect_link(source), detect_link(recorded_source))
                    matches = all(i.platform == 'douyin' and i.kind == 'work'
                                  and i.confidence == 'exact' and i.extracted_id == collected.work_id
                                  for i in identities)
                else:
                    matches = source == recorded_source
                if not matches:
                    raise ValueError('采集作品编号与原链接不一致，未准备下载或转录')
                # Freeze one saved source for aliases, duplicate detection and
                # result lookup. Preparing never resolves links over network.
                source = recorded_source
            meta = None
            if not link:
                from ..live_recording.media import fingerprint
                path = Path(source)
                meta = dict(bytes=path.stat().st_size, sha256=fingerprint(path))
            title = item.get('title') or (Path(source).stem if not link else '链接逐字稿')
            if type(title) is not str or not 1 <= len(title) <= 500:
                raise ValueError('文稿标题无效')
            duplicate = seen.get(source)
            seen.setdefault(source, number)
            sources.append(dict(index=number, source=source, title=title, stable_key=stable_key, local_source=meta,
                                state='duplicate' if duplicate is not None else 'pending',
                                duplicate_of=duplicate, progress=0, message='', document_id=None))
        plan = dict(batch_id=('extract-' if extraction_options is not None else 'batch-') + request_id, kind='transcription', operation=operation, outputs=outputs, items=sources,
                    engine=engine, folder_id=folder_id, save_media=save_media,
                    media_directory=media_directory, prefer_subtitles=prefer_subtitles,
                    cloud_concurrency=cloud_concurrency, cloud_limits=cloud_limits, library_root=self.library_root,
                    network=cloud or any(extract_url(i['source']) for i in sources))
        if cloud:
            plan['cloud_tracking_version'] = 1
        if extraction_options is not None:
            self._initialize_extraction(plan, extraction_options)
        plan_hash = documents.digest(encode(plan))
        with self.lock:
            path = self._path(plan['batch_id'])
            if path.exists():
                old = self._read(plan['batch_id'])
                comparable = deepcopy(plan)
                if not old['plan'].get('cloud_tracking_version'):
                    # A replay of an old request preserves its old frozen policy;
                    # new automatic settings require a genuinely new request ID.
                    for key in ('cloud_tracking_version','cloud_limits'):
                        if key not in old['plan']:
                            comparable.pop(key,None)
                    if cloud_concurrency is None and 'cloud_concurrency' in old['plan']:
                        comparable['cloud_concurrency'] = old['plan']['cloud_concurrency']
                if old['plan_hash'] != documents.digest(encode(comparable)):
                    raise library.RevisionConflict('同一批次编号已用于其他输入，未重建或覆盖')
                return self.show(plan['batch_id'])
            frozen = deepcopy(plan)
            plan.update(plan=frozen, state='prepared', plan_hash=plan_hash, message='')
            self._save(plan)
            return self.show(plan['batch_id'], limit=50 if extraction_options is not None else None)

    def prepare_download(self, request_id, items, destination, outputs='video'):
        return self.prepare(request_id, items, None, save_media=True, media_directory=destination,
                            operation='download', outputs=outputs)

    def start(self, batch_id, expected_hash, network_consent=False, resume=False, cloud_upload_consent=False,
              retry_unknown=False):
        self._same_library()
        if any(type(flag) is not bool for flag in (network_consent, resume, cloud_upload_consent, retry_unknown)):
            raise ValueError('联网确认或恢复选项无效')
        with self.lock:
            batch = self._read(batch_id)
            if batch.get('workspace'):
                raise ValueError('逐作品提取任务请使用 batches.control，以保留各条选择和已有结果')
            if batch['plan_hash'] != expected_hash:
                raise library.RevisionConflict('批次计划已有变化，请重新读取')
            from ..engines.providers import PRESETS
            retired = getattr(PRESETS.get(batch.get('engine')), 'retired', False)
            cached = {i['index'] for i in batch['items'] if i['state'] not in {'succeeded','duplicate','skipped'}
                      and self._result_path(batch_id,i['index']).exists()} if retired else set()
            if retired and batch['state'] != 'succeeded' and (retry_unknown or not cached):
                raise ValueError('此云端转录服务已移除，不再发送请求；原文稿和已保存结果保留。')
            from ..transcription_job_application import LocalTranscriptionJobError
            if batch.get('network') and not network_consent and not retired:
                raise LocalTranscriptionJobError('CONFIRMATION_REQUIRED', 'BATCH_NETWORK_CONSENT_REQUIRED')
            cloud = batch.get('engine') in cloud_ids()
            if cloud and not cloud_upload_consent and not retired:
                raise LocalTranscriptionJobError('CONFIRMATION_REQUIRED', 'CLOUD_AUDIO_UPLOAD_CONSENT_REQUIRED')
            if batch_id in self.workers or batch['state'] == 'succeeded':
                return self.show(batch_id)
            if batch['state'] != 'prepared' and not resume:
                return self.show(batch_id)
            scope = cached if retired else {i['index'] for i in batch['items'] if self._unknown(batch,i)} if retry_unknown else None
            if retry_unknown and (not resume or not scope):
                raise ValueError('本批没有可明确重试的未确认项')
            if self.workers and (not cloud or any(self._read(b).get('engine') not in cloud_ids() for b in self.workers)):
                raise ValueError('已有批次正在运行，请等它完成或取消后继续')
            cancel = threading.Event()
            self.cancels[batch_id] = cancel
            self.forces[batch_id] = threading.Event()
            self.halts[batch_id] = threading.Event()
            batch.update(state='running', message='')
            batch['run_scope'] = sorted(scope) if scope is not None else None
            batch.pop('reason_code', None)
            if resume:
                for item in batch['items']:
                    if scope is not None and item['index'] not in scope:
                        continue
                    if item['state'] not in {'succeeded', 'duplicate', 'skipped'}:
                        if self._unknown(batch,item) and not retry_unknown:
                            item.update(state='recovery_required',phase='unconfirmed',
                                message='此项可能已计费，结果未确认；普通恢复不再次上传，请单独确认重试')
                            continue
                        if retry_unknown and self._unknown(batch,item):
                            item.setdefault('cloud_attempt_history',[]).append(deepcopy(item['cloud_attempt']))
                            item.pop('cloud_attempt',None)
                        item.update(state='pending', message='', progress=0)
            self._save(batch)
            worker = threading.Thread(target=self._run, args=(batch_id, cancel), name='lulu-cli-batch', daemon=True)
            self.workers[batch_id] = worker
            try:
                worker.start()
            except Exception:
                self.workers.pop(batch_id, None)
                batch.update(state='interrupted', message='批次未能启动，可恢复；原资料保留')
                self._save(batch)
                raise
            return self.show(batch_id)

    def _unknown(self, batch, item):
        return (item.get('cloud_attempt',{}).get('state') in {'sending','unknown','received'}
                and not self._result_path(batch['batch_id'],item['index']).exists()
                and item['state'] != 'succeeded')

    def show(self, batch_id, offset=0, limit=None, include_hidden=False, locate_index=None):
        if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or not 0 <= limit <= 1000)):
            raise ValueError('批次分页范围无效')
        with self.lock:
            batch = self._read(batch_id)
            if batch.get('workspace'):
                return self.show_extraction(batch_id, offset, 50 if limit is None else limit, include_hidden, locate_index)
            if batch['state'] == 'running' and batch_id not in self.workers:
                batch.update(state='interrupted', message='上次批次已中断，已保存结果保留；请显式恢复')
                for item in batch['items']:
                    if self._unknown(batch,item):
                        item.update(state='recovery_required',phase='unconfirmed',message='上次请求结果未确认，可能已计费；未自动重发')
                self._save(batch)
            counts = {state: sum(i['state'] == state for i in batch['items']) for state in
                      ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'duplicate', 'skipped', 'collected', 'recovery_required')}
            progress = sum(100 if i['state'] in {'succeeded', 'duplicate', 'skipped'} else i['progress'] for i in batch['items']) / max(1, len(batch['items']))
            batch.pop('plan', None)
            for item in batch['items']:
                if item['state'] == 'duplicate':
                    original = batch['items'][item['duplicate_of']]
                    item.update(original_state=original['state'], document_id=original.get('document_id'), files=original.get('files'))
            total = len(batch['items'])
            unknown = sum(self._unknown(batch,i) for i in batch['items'])
            from ..engines.providers import PRESETS
            provider = PRESETS.get(batch.get('engine'))
            local_recovery = sum(i['state'] not in {'succeeded','duplicate','skipped'}
                and self._result_path(batch_id,i['index']).exists() for i in batch['items']) if provider and provider.retired else 0
            if limit is not None:
                batch['items'] = batch['items'][offset:offset+limit]
            return {**batch, 'counts': counts, 'progress': round(progress, 1), 'total': total,
                    'offset':offset, 'has_more':limit is not None and offset+limit < total,
                    'unconfirmed_count':unknown,
                    'local_recovery_count':local_recovery,
                    'upload_notice': ('此云端转录服务已移除，历史文稿和已保存结果保留，不再上传。' if provider and provider.retired else
                        f'音频会上传至{provider.display_name}并按账户计费；按服务额度自动调度，结果未确认时不自动重发' if provider and provider.requires_network else '')}

    def _batch_paths(self):
        return sorted([*self.root.glob('batch-*.json'), *self.root.glob('extract-*.json')])

    def list(self):
        return [self.show(p.stem,limit=0) for p in self._batch_paths()
                if not p.name.endswith(('.result.json','.state.json','.extract-result.json'))]

    def cancel(self, batch_id, force=False):
        if type(force) is not bool:
            raise ValueError('停止选项无效')
        with self.lock:
            batch = self._read(batch_id)
            if batch.get('workspace'):
                raise ValueError('逐作品提取任务请使用 batches.control 明确选择暂停或取消')
            cancel = self.cancels.get(batch_id)
            if cancel:
                cancel.set()
                if force:
                    self.forces[batch_id].set()
            elif batch['state'] in {'prepared', 'interrupted'}:
                batch['state'] = 'cancelled'
                for item in batch['items']:
                    if item['state'] in {'pending', 'running'}:
                        item['state'] = 'cancelled'
                self._save(batch)
            return self.show(batch_id)

    def _result_path(self, bid, index):
        path = self.root / (bid + '-' + str(index) + '.result.json')
        if path.is_symlink():
            raise ValueError('批次结果路径不安全')
        return path

    @staticmethod
    def _transcribe(item, batch, progress, cancelled, engine_override=None,
                    cloud_observer=None, cloud_force_cancel=None):
        from ..pipeline import transcribe_source
        policy = None
        if item.get('stable_key'):
            from ..task import TaskExecutionPolicy
            from ..collect.preferences import MaterialPreferenceSnapshot
            policy = TaskExecutionPolicy(origin='homepage_batch_transcribe', mode='transcription_only',
                stable_key=item['stable_key'], material_snapshot=MaterialPreferenceSnapshot('audio', False, False),
                media_destination='temporary_only', engine_name=batch['engine'],
                prefer_subtitles=batch['prefer_subtitles'], local_only=batch['engine'] not in cloud_ids())
        return transcribe_source(item['source'], engine_name=batch['engine'], enable_link_download=True,
            keep_media=batch['save_media'], media_dir=batch['media_directory'],
            link_media_mode='video' if batch['save_media'] else 'audio',
            prefer_subtitles=batch['prefer_subtitles'], progress=progress, is_cancelled=cancelled, execution_policy=policy,
            max_input_bytes=2*1024**3, bounded_cloud=batch['engine'] in cloud_ids(), engine_override=engine_override,
            cloud_observer=cloud_observer, cloud_force_cancel=cloud_force_cancel)

    @staticmethod
    def _prepare_cloud_engine(provider, limits=None):
        from .. import secrets
        from ..engines.cloud_requests import CloudBatchEngine
        cfg = secrets.get_config(provider, require_access=True, refresh=True)
        key = cfg.get('api_key')
        if type(key) is not str or not key.strip():
            raise RuntimeError('CLOUD_CONFIGURATION_MISSING')
        return CloudBatchEngine(provider, cfg, limits)

    def _run(self, bid, cancel):
        from ..engines.asr_resource import batch_context, finish_batch
        cloud_preparing = False
        cloud_engine = None
        try:
            batch = self._read(bid)
            if batch['kind'] == 'collection':
                self._collect(batch, cancel)
                return
            cloud = batch['engine'] in cloud_ids()
            if cloud:
                for item in batch['items']:
                    if (item['state'] not in {'succeeded', 'duplicate', 'skipped'}
                            and self._result_path(bid, item['index']).exists()):
                        self._run_one(batch, item, cancel)
            scope = batch.get('run_scope')
            eligible = [i for i in batch['items'] if i['state'] not in {'succeeded','duplicate','skipped'}
                        and not self._unknown(batch,i) and (scope is None or i['index'] in scope)]
            from ..engines.providers import PRESETS
            retired = getattr(PRESETS.get(batch['engine']), 'retired', False)
            if retired:
                eligible = []  # Old results may be saved; old services never upload.
            if cloud and self.default_runner and eligible and not cancel.is_set():
                cloud_preparing = True
                cloud_engine = self._prepare_cloud_engine(batch['engine'],batch.get('cloud_limits'))
                cloud_preparing = False
                if cloud_engine.policy.audio_seconds_per_hour or cloud_engine.policy.audio_seconds_per_day or cloud_engine.policy.requests_per_day:
                    with self.lock:
                        attempts = []
                        for path in self._batch_paths():
                            if path.name.endswith(('.state.json','.result.json')):
                                continue
                            prior = self._read(path.stem)
                            if prior.get('engine') != batch['engine']:
                                continue
                            for item in prior['items']:
                                for attempt in [*[a.get('cloud_attempt',{}) for a in item.get('attempts',[])],*item.get('cloud_attempt_history',[]),item.get('cloud_attempt',{})]:
                                    if attempt.get('model') == cloud_engine.model:
                                        attempts.append(attempt)
                        cloud_engine.gate.seed_audio(attempts)
            with batch_context(bid):
                if cloud and eligible:
                    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
                    capacity = RESOURCES.capacity(len(eligible),batch.get('cloud_concurrency'))
                    # Futures are also lazy: queued files have no process, connection
                    # or converted media until an execution slot is available.
                    iterator = iter(eligible)
                    with ThreadPoolExecutor(max_workers=capacity,thread_name_prefix='lulu-cloud') as pool:
                        pending = set()
                        while True:
                            while len(pending) < capacity and not cancel.is_set() and not self.halts[bid].is_set():
                                item = next(iterator,None)
                                if item is None:
                                    break
                                pending.add(pool.submit(self._run_one,batch,item,cancel,cloud_engine))
                            if not pending:
                                break
                            done,pending = wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
                            for future in done:
                                future.result()
                        if cancel.is_set():
                            for item in iterator:
                                item.update(state='cancelled',message='已取消，尚未上传')
                elif not cloud:
                    for item in batch['items']:
                        self._run_one(batch, item, cancel)
            with self.lock:
                incomplete = any(i['state'] not in {'succeeded','duplicate','skipped'} for i in batch['items'])
                batch['state'] = ('interrupted' if self.halts[bid].is_set() else 'cancelled' if cancel.is_set()
                                  else 'partial' if incomplete else 'succeeded')
                if retired:
                    batch['message'] = '此转录服务已移除，仅补保存本机已有结果；其他历史项保留，不再上传。'
                elif any(self._unknown(batch,i) for i in batch['items']):
                    batch['message'] = '部分云端请求结果未确认，普通恢复不会再次上传；请单独确认可能重复计费后重试'
                self._save(batch)
        except Exception as exc:
            with self.lock:
                batch = self._read(bid)
                if cloud_preparing:
                    missing = str(exc) == 'CLOUD_CONFIGURATION_MISSING'
                    batch.update(state='cancelled' if cancel.is_set() else 'interrupted',
                        reason_code='CLOUD_CONFIGURATION_MISSING' if missing else 'CLOUD_CONFIGURATION_UNAVAILABLE',
                        message=('没有找到这家服务的转录密钥，请在「AI 大模型 → 内容提取 → 云端 API」保存后恢复。本次尚未上传。'
                            if missing else '系统暂时未能读取转录密钥，请在 Lulu 内核对系统授权后显式恢复。本次尚未上传，未再次尝试读取。'))
                else:
                    batch.update(state='interrupted', message='批次中断，已保存文稿和结果保留，可恢复')
                self._save(batch)
        finally:
            if cloud_engine is not None:
                cloud_engine.close()
            finish_batch(bid)
            with self.lock:
                self.workers.pop(bid, None)
                self.cancels.pop(bid, None)
                self.forces.pop(bid, None)
                self.halts.pop(bid, None)
                for key in [k for k in self._progress_times if k[0] == bid]:
                    self._progress_times.pop(key,None)

    def _run_one(self, batch, item, cancel, engine_override=None):
        bid = batch['batch_id']
        if item['state'] in {'succeeded', 'duplicate', 'skipped'}:
            return
        if cancel.is_set():
            with self.lock:
                item.update(state='cancelled', message='批次已取消，未开始此项')
                self._save(batch,item)
            return
        halt = self.halts.get(bid, threading.Event())
        force = self.forces.get(bid, threading.Event())
        if halt.is_set() or self._unknown(batch,item):
            return
        cancelled = lambda:cancel.is_set() or halt.is_set()
        result_path = self._result_path(bid, item['index'])
        with self.lock:
            item.update(state='running', message='准备中', progress=0)
            self._save(batch,item)
        def progress(message, fraction):
            with self.lock:
                text = str(message)[:500]
                changed = text != item.get('message')
                item['message'] = text
                if type(fraction) in {int, float} and 0 <= fraction <= 1:
                    item['progress'] = min(95, int(fraction*100))
                key = (bid,item['index'])
                if changed or time.monotonic()-self._progress_times.get(key,0) >= .75:
                    self._save(batch,item)
                    self._progress_times[key] = time.monotonic()
        def observe(event, data):
            with self.lock:
                if event == 'sending':
                    if item.get('cloud_attempt'):
                        item.setdefault('cloud_attempt_history',[]).append(deepcopy(item['cloud_attempt']))
                    item['cloud_attempt'] = dict(data,state='sending')
                    item.pop('cloud_error',None)
                elif event in {'received','rejected'}:
                    item.setdefault('cloud_attempt',{}).update(data,state=event,finished_at=time.time())
                item['phase'] = {'waiting':'waiting_service','sending':'transcribing',
                    'received':'saving','rejected':'waiting_service','settling':'settling'}.get(event,event)
                self._save(batch,item)
        try:
            self._same_library()
            if batch.get('operation') == 'download':
                self._download(batch, item, cancel)
                return
            if item.get('stable_key') and batch['save_media'] and not item.get('files'):
                self._download_collected(batch, item, cancel)
            if result_path.exists():
                data = json.loads(result_path.read_text(encoding='utf-8'))
                if documents.digest(encode(data['transcript'])) != data['sha256']:
                    raise ValueError('已有结果校验失败，未重新推理或覆盖')
                raw = data['transcript']
                result = Transcript(**{**raw, 'segments': [Segment(**s) for s in raw['segments']]})
            else:
                local = item.get('local_source')
                if local:
                    from ..live_recording.media import fingerprint
                    path = actual_file(item['source'], maximum=2*1024**3)
                    if path.stat().st_size != local['bytes'] or fingerprint(path) != local['sha256']:
                        raise ValueError('原文件已变化，请重新准备批次')
                if item.get('cloud_attempt',{}).get('not_before',0) > time.time():
                    raise CloudFailure('rate_limited',retry_after=item['cloud_attempt']['not_before']-time.time())
                result = (self.runner(item, batch, progress, cancelled, engine_override=engine_override,
                                      cloud_observer=observe,cloud_force_cancel=force.is_set)
                    if self.default_runner else self.runner(item, batch, progress, cancel.is_set))
                if not result.text.strip():
                    raise ValueError('未取得有效正文，未创建空文稿')
                if result.engine != batch['engine'] and not str(result.engine).startswith('平台'):
                    raise ValueError('转录模型与计划不一致，未保存')
                raw = asdict(result)
                library._write_atomic_text(result_path, encode(dict(transcript=raw, sha256=documents.digest(encode(raw)))))
            with self.lock, library._transaction():
                self._same_library()
                if cancel.is_set() and batch['engine'] not in cloud_ids():
                    raise InterruptedError('已取消，已生成结果保留，可恢复保存')
                eid = 'transcript_' + bid + '_' + str(item['index'])
                library.save(result, item['title'], batch['folder_id'], entry_id=eid)
                library.finish_transcript_document(eid, result, item['title'])
                if item.get('stable_key'):
                    from ..collect.repository import CollectionRepository
                    repository = CollectionRepository()
                    current = next((i for i in repository.load().items if i.stable_key == item['stable_key']), None)
                    if current is not None:
                        repository.compare_and_swap_fields(item['stable_key'],
                            expected={'transcript_status': current.transcript_status}, updates={'transcript_status': 'completed'})
                item.update(state='succeeded', document_id=eid, progress=100, message='已保存完整文稿')
                item.pop('cloud_error',None)
                self._save(batch,item)
        except Exception as exc:
            with self.lock:
                if isinstance(exc,CloudFailure):
                    item['cloud_error'] = exc.public()
                    if exc.kind == 'unknown':
                        item.setdefault('cloud_attempt',{}).update(state='unknown',finished_at=time.time())
                        item['phase'] = 'unconfirmed'
                    else:
                        item.setdefault('cloud_attempt',{}).update(state='rejected',finished_at=time.time())
                    if exc.kind == 'account':
                        halt.set()
                        batch['message'] = str(exc)
                unknown = self._unknown(batch,item)
                item.update(state='recovery_required' if unknown or result_path.exists() else 'cancelled' if cancel.is_set() else 'failed',
                    message=str(exc)[:1000] if isinstance(exc, (ValueError, InterruptedError)) else
                    '此项未完成，已有结果保留；请核对网络、模型或磁盘后显式恢复'
                    + ('；云端可能已计费' if batch['engine'] in cloud_ids() else ''))
                if isinstance(exc,CloudFailure):
                    item['message'] = str(exc)
                self._save(batch,item)
            if getattr(exc, 'code', '') == 'RESOURCE_BUSY':
                raise

    def _download(self, batch, item, cancel):
        if item.get('stable_key'):
            self._download_collected(batch, item, cancel)
            item.update(state='succeeded', progress=100, message='下载文件已保存')
            with self.lock:
                self._save(batch,item)
            return
        extraction = self.runtime.extraction()
        jid = item.get('download_job_id')
        if jid:
            job = self.runtime._job_control.show(jid)
            if job.state.value in {'recovery_required', 'partial'}:
                raise ValueError('已有下载结果需要核对，请查询原下载任务，未重复下载')
            if job.state.value in {'failed', 'cancelled'}:
                jid = None
        if not jid:
            plan = extraction.prepare(item['source'], batch['outputs'], batch['media_directory'])
            item['download_attempt'] = item.get('download_attempt', 0) + 1
            request = batch['batch_id'] + '-' + str(item['index']) + '-' + str(item['download_attempt'])
            started = extraction.run(item['source'], batch['outputs'], batch['media_directory'], plan['plan_digest'], request)
            item['download_job_id'] = jid = started['job_id']
            with self.lock:
                self._save(batch,item)
        from ..job_ledger.contracts import TERMINAL_STATES
        while True:
            job = self.runtime._job_control.show(jid)
            if job.state in TERMINAL_STATES:
                break
            if cancel.is_set():
                extraction.cancel(jid)
            time.sleep(.15)
        if job.state.value != 'succeeded':
            raise ValueError('下载未完成；原任务和已有文件保留，请查看 download_job_id 后恢复')
        result = extraction.receipt(jid, job.result_ref)
        with self.lock:
            item.update(state='succeeded', progress=100, files=result['items'], message='下载文件已保存')
            self._save(batch,item)

    def _download_collected(self, batch, item, cancel):
        from ..collect.repository import CollectionRepository
        from ..collect.media import CollectionMediaAssociationService, ManagedMediaStore
        from ..collect.media_fetcher import fetch_managed_media
        from ..collect.preferences import MaterialPreferenceSnapshot
        from ..task import TaskExecutionPolicy
        from ..link_extraction_jobs import open_destination, publish
        from .store import WorkspaceStore
        import os
        repository = CollectionRepository()
        selected = next((i for i in repository.load().items if i.stable_key == item['stable_key']), None)
        if selected is None or selected.work_url != item['source']:
            raise ValueError('采集作品记录已有变化，请重新选择')
        store = ManagedMediaStore(repository.path.parent/'media')
        policy = TaskExecutionPolicy(origin='homepage_history_material', mode='materialize',
            stable_key=item['stable_key'], material_snapshot=MaterialPreferenceSnapshot('video', True, False),
            media_destination='managed_collection')
        result = CollectionMediaAssociationService(repository, store, fetch_managed_media).run([selected], policy=policy, is_cancelled=cancel.is_set)
        outcome = result.outcomes[0]
        if outcome.status != 'downloaded':
            raise ValueError('采集素材下载未完成：' + outcome.reason_code + '；原记录保留')
        source = store.existing_file(outcome.relative_path, expected_stable_key=item['stable_key'])
        if source is None:
            raise ValueError('已下载素材无法核对，未导出')
        import tempfile
        from contextlib import ExitStack
        with ExitStack() as resources:
            publish_root = store.root
            kind = batch.get('outputs', 'video')
            if kind == 'audio':
                from ..link_assets import separate_audio
                publish_root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='lulu-collected-audio-')))
                source = separate_audio(source, publish_root/'audio.m4a', cancel.is_set)
            destination, fd, folder_identity = open_destination(batch['media_directory'])
            resources.callback(os.close, fd)
            name = batch['batch_id'] + '-' + str(item['index'])
            target = destination/(name + source.suffix)
            from ..live_recording.media import fingerprint
            key = 'batch-media:' + name
            saved = WorkspaceStore().preference(key)
            expected = dict(path=str(target), bytes=source.stat().st_size, sha256=fingerprint(source))
            if saved and saved != expected:
                raise ValueError('本批已导出的素材内容或位置已有变化，未覆盖')
            if saved and target.is_file() and not target.is_symlink() and fingerprint(target) == saved['sha256']:
                receipt = saved
            else:
                WorkspaceStore().preference(key, expected)
                receipt = publish(source, publish_root, destination, fd, folder_identity, name,
                    cancelled=cancel.is_set, commit_lock=self.lock)
                WorkspaceStore().preference(key, receipt)
            item['files'] = {kind: {'state':'succeeded', **receipt}}
            with self.lock:
                self._save(batch,item)

    def prepare_collection(self, request_id, source, mode='anonymous_latest', limit=20, browser_mode='efficiency'):
        from ..collect.application import CollectionApplicationService
        service = self.collector or CollectionApplicationService()
        entry = service.inspect(source)
        if entry.identity.platform != 'douyin' or entry.identity.kind != 'homepage':
            raise ValueError('本轮只支持抖音主页采集')
        if mode not in entry.allowed_modes or (limit is not None and (type(limit) is not int or not 1 <= limit <= 500)):
            raise ValueError('采集方式或数量无效；全量请明确使用 limit=null')
        if browser_mode not in {'efficiency', 'low_risk'} or (mode == 'profile_all' and limit is not None):
            raise ValueError('浏览器方式应为 efficiency/low_risk；全量采集应明确使用 limit=null')
        identity(request_id)
        plan = dict(batch_id='batch-' + request_id, kind='collection', source=entry.identity.canonical_url,
                    mode=mode, limit=limit, browser_mode=browser_mode, library_root=self.library_root, network=True, items=[])
        plan_hash = documents.digest(encode(plan))
        with self.lock:
            if self._path(plan['batch_id']).exists():
                old = self._read(plan['batch_id'])
                if old['plan_hash'] != plan_hash:
                    raise library.RevisionConflict('同一批次编号已有其他任务')
                return self.show(plan['batch_id'])
            plan.update(plan=deepcopy(plan), plan_hash=plan_hash, state='prepared', message='')
            self._save(plan)
            return self.show(plan['batch_id'])

    def _collect(self, batch, cancel):
        from ..collect.application import CollectionApplicationService, CollectionCommand
        from ..collect.history import CollectionHistoryRepository
        from ..collect.contracts import CollectResult
        from ..collect.models import CollectedItem
        service = self.collector or CollectionApplicationService()
        def progress(message, count):
            with self.lock:
                batch.update(message=str(message)[:500], collected_count=count)
                self._save(batch)
        if batch.get('collection_pending'):
            saved = batch['collection_pending']
            result = CollectResult(**{**saved, 'items': tuple(CollectedItem(**i) for i in saved['items'])})
        else:
            result = service.collect(CollectionCommand(batch['source'], batch['mode'], limit=batch['limit'],
                browser_mode=batch['browser_mode']), progress=progress, is_cancelled=cancel.is_set)
            with self.lock:
                batch['collection_attempt'] = batch.get('collection_attempt', 0) + 1
                batch['collection_pending'] = asdict(result)
                self._save(batch)
        with self.lock:
            self._same_library()
            batch.update(items=[dict(asdict(item), index=n, state='collected', progress=100) for n, item in enumerate(result.items)],
                         completion=result.completion, reason_code=result.reason_code, message=result.next_action)
            if result.items or result.confirmed_empty:
                from ..collect.repository import CollectionRepository
                if result.items:
                    CollectionRepository().upsert_collected(result.items)
                snapshot = CollectionHistoryRepository().add_record(result.items, platform='douyin',
                    homepage_url=batch['source'], source_method='cli', completion=result.completion,
                    reason_code=result.reason_code, confirmed_empty=result.confirmed_empty,
                    record_id='record-cli-' + documents.digest(encode([batch['batch_id'], batch['collection_attempt']])))
                batch['record_id'] = 'record-cli-' + documents.digest(encode([batch['batch_id'], batch['collection_attempt']]))
            batch['state'] = {'complete':'succeeded', 'partial':'partial', 'cancelled':'cancelled'}.get(result.completion, 'failed')
            batch['collection_pending'] = None
            self._save(batch)
