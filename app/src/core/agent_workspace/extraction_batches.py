"""Per-work choices and results over the existing authoritative batch runtime.

No GUI state or credentials live here. Source validation, ASR locks and cloud
account gates are the same ones used by the original batch commands.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import threading
import time

from .. import library
from ..models import Transcript, Segment
from ..engines.cloud_http import CloudFailure
from ..engines.cloud_requests import cloud_ids
from . import documents
from .store import encode, identity

OUTPUTS = ('text', 'video', 'cover')
SETTLED = frozenset({'succeeded', 'duplicate', 'skipped'})
ACTIVE = frozenset({'queued', 'running', 'settling'})


def read_item_file(path, expected_sha256):
    """Private JSONL admission avoids making IPC byte limits a work-count cap."""
    import hashlib
    import json
    import os
    import stat
    from .operations import actual_file
    if (type(expected_sha256) is not str or len(expected_sha256)!=64
            or any(c not in '0123456789abcdef' for c in expected_sha256)):
        raise ValueError('作品清单摘要无效')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('重复字段')
            result[key]=value
        return result
    path=actual_file(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
            raise ValueError('作品清单必须为本人私有的普通文件（权限0600）')
        digest=hashlib.sha256();items=[]
        with os.fdopen(fd,'rb',closefd=False) as stream:
            while line:=stream.readline(256*1024+1):
                if len(line)>256*1024:
                    raise ValueError('单条作品描述过长')
                digest.update(line)
                try: items.append(json.loads(line,object_pairs_hook=unique))
                except (ValueError,UnicodeError,RecursionError):
                    raise ValueError('作品清单格式无效，每行应为一条作品的JSON') from None
        if digest.hexdigest()!=expected_sha256:
            raise library.RevisionConflict('作品清单已变化，未创建任务')
        return items
    finally:
        os.close(fd)


def selected_outputs(value):
    if (type(value) is not list or any(type(k) is not str or k not in OUTPUTS for k in value)
            or len(set(value)) != len(value)):
        raise ValueError('提取内容只接受不重复的 text、video、cover')
    return [k for k in OUTPUTS if k in value]


def item_version(item):
    # Progress text changes frequently; it does not invalidate an unrelated click.
    return documents.digest(encode({k: item.get(k) for k in
        ('index', 'selection', 'attempt_id', 'hidden')}))


def visibility_version(items):
    return documents.digest(encode([i['index'] for i in items if not i.get('hidden')]))


def extraction_activity(item):
    chosen = item.get('attempt_selection') or item['selection']
    missing = [k for k in chosen['outputs'] if item['assets'][k]['state'] != 'ready']
    return dict(steps=['prepare'] + [k for k in ('text', 'cover') if k in missing] + ['save'],
                stage='prepare', step='preparing', fraction=None,
                started_at=time.time(), attempt_id=item.get('attempt_id'))


class ExtractionBatchMixin:
    def stage_extraction(self, request_id, items, engine='local_sensevoice_q8',
                         folder_id=None, name=None, group=False, media_directory=None,
                         skipped=None):
        identity(request_id)
        if type(group) is not bool or (name is not None and
                (type(name) is not str or not 1 <= len(name) <= 500)):
            raise ValueError('任务名称或分组无效')
        if type(items) is not list or not items:
            raise ValueError('请明确选择本批作品')
        if skipped is not None and (type(skipped) is not list or any(
                type(v) is not dict or set(v) != {'title', 'reason'} or
                any(type(x) is not str or len(x) > 1000 for x in v.values()) for v in skipped)):
            raise ValueError('跳过说明无效')
        clean, selections = [], []
        for row in items:
            if type(row) is not dict or set(row) - {
                    'source', 'title', 'stable_key', 'outputs', 'prefer_subtitles', 'media_directory', 'new_text'}:
                raise ValueError('作品参数无效')
            clean.append({k: row[k] for k in ('source', 'title', 'stable_key') if k in row})
            outputs = selected_outputs(row.get('outputs', ['text']))
            subtitles = row.get('prefer_subtitles', False)
            if type(subtitles) is not bool:
                raise ValueError('优先字幕必须为布尔值')
            if type(row.get('new_text', False)) is not bool:
                raise ValueError('另建文案任务必须为布尔值')
            directory = row.get('media_directory', media_directory)
            if directory is not None and (type(directory) is not str or not Path(directory).is_absolute()):
                raise ValueError('素材保存位置必须是完整路径')
            selections.append(dict(outputs=outputs, prefer_subtitles=subtitles, media_directory=directory,
                                   new_text=row.get('new_text', False)))
        intent = dict(items=items, engine=engine, folder_id=folder_id, name=name,
                      group=group, media_directory=media_directory, skipped=skipped or [])
        fingerprint = documents.digest(encode(intent))
        with self.lock:
            path = self._path('extract-' + request_id)
            if path.exists():
                batch = self._read('extract-' + request_id)
                if batch.get('workspace', {}).get('staging_digest') != fingerprint:
                    raise library.RevisionConflict('同一任务编号已有其他选择，原任务保留')
                return self.show(batch['batch_id'], limit=50)
            options = dict(version=1, name=name or (clean[0].get('title') or '提取任务'),
                           group=group, staging_digest=fingerprint, skipped=skipped or [],
                           created_at=time.time(), selections=selections)
            return self.prepare(request_id, clean, engine, folder_id,
                                extraction_options=options)

    @staticmethod
    def _check_media_directory(directory):
        if directory is None:
            return
        if type(directory) is not str:
            raise ValueError('请选择素材保存文件夹')
        from ..link_extraction_jobs import open_destination
        import os
        _path, fd, _identity = open_destination(directory)
        os.close(fd)

    def _initialize_extraction(self, plan, options):
        plan['workspace'] = {k: v for k, v in options.items() if k != 'selections'}
        for key in ('outputs','save_media','media_directory','prefer_subtitles','operation'):
            plan.pop(key, None)
        known_documents = {}
        for entry in sorted(library.list_entries(), key=lambda e:str(e.get('created_at') or ''), reverse=True):
            if entry.get('kind') == 'transcript':
                known_documents.setdefault(entry.get('source_url') or entry.get('source'), entry)
        unfinished = {}
        for path in self._batch_paths():
            if path.name.endswith(('.result.json', '.state.json', '.extract-result.json')):
                continue
            prior = self._extraction_live(path.stem)
            if not prior.get('workspace'):
                continue
            for row in prior['items']:
                if not row.get('hidden') and row['state'] not in SETTLED | {'cancelled'}:
                    unfinished.setdefault(row['source'], (prior['batch_id'], row['index']))
        for item, selection in zip(plan['items'], options['selections']):
            if item.get('local_source') and any(k != 'text' for k in selection['outputs']):
                raise ValueError('本地文件只支持提取文案')
            assets = {k: dict(state='missing') for k in OUTPUTS}
            # A filename can now contain different audio. Only link results are
            # reused; local inputs retain their frozen file fingerprint.
            document = (known_documents.get(item['source']) if not item.get('local_source')
                        and not selection.get('new_text') else None)
            if document:
                tr = library.load(document['id'])
                if tr.text.strip():
                    assets['text'] = dict(state='ready', document_id=document['id'])
                for key, value in (tr.meta or {}).get('extraction_assets', {}).items():
                    if key in {'video', 'cover'} and self._usable_asset(value):
                        assets[key] = dict(state='ready', path=value)
            if item.get('stable_key'):
                from ..collect.repository import CollectionRepository
                collected = next((v for v in CollectionRepository().load().items
                                  if v.stable_key == item['stable_key']), None)
                if collected:
                    for key, value in [('video', collected.media_local_path),
                                       ('cover', collected.cover_local_path)]:
                        if self._usable_asset(value):
                            assets[key] = dict(state='ready', path=value)
            item.update(selection=selection, assets=assets, hidden=False, auto_paused=False,
                        attempt_id=None, attempts=[], attempt_selection=None,
                        document_id=assets['text'].get('document_id'))
            if item['state'] != 'duplicate':
                item['state'] = ('succeeded' if selection['outputs'] and not self._missing_outputs(item) else 'draft')
                prior = unfinished.get(item['source'])
                if prior and not selection.get('new_text') and self._missing_outputs(item):
                    item.update(state='duplicate', existing_task=dict(batch_id=prior[0], index=prior[1]),
                                message='此作品已有未结束任务，请查看原任务，避免重复处理')

    @staticmethod
    def _usable_asset(value):
        if type(value) is not str or not value:
            return False
        try:
            path = Path(value)
            return (path.is_absolute() and '..' not in path.parts
                    and not any(p.is_symlink() for p in [path, *path.parents]) and path.is_file()
                    and path.stat().st_size > 0)
        except OSError:
            return False

    @staticmethod
    def _missing_outputs(item):
        return [k for k in item['selection']['outputs']
                if item['assets'][k]['state'] not in {'ready', 'unknown'}]

    def _reconcile_missing_assets(self, batch):
        changed = False
        for item in batch['items']:
            for key in OUTPUTS:
                asset = item['assets'][key]
                if asset['state'] != 'ready':
                    continue
                if key == 'text':
                    # Inspect the active record without loading/migrating text or
                    # restoring anything the user deliberately removed.
                    eid = asset.get('document_id') or item.get('document_id')
                    try:
                        available = bool(eid and library._load_raw(eid))
                    except (OSError, ValueError, TypeError):
                        available = False
                else:
                    available = self._usable_asset(asset.get('path'))
                if not available:
                    asset.update(state='failed', error=('文稿已移出或删除，可补充提取' if key == 'text'
                                                       else '素材已移动或删除，可补充提取'))
                    if key == 'text':
                        asset.pop('document_id', None)
                        item['document_id'] = None
                    if key in item['selection']['outputs'] and item['state'] == 'succeeded':
                        item['state'] = 'failed'
                    if key in item['selection']['outputs']:
                        item['message'] = '部分内容已移出或删除，可补充提取'
                    changed = True
        return changed

    def _extraction_live(self, bid):
        batch = self._active_extractions.get(bid)
        if batch is not None:
            return batch
        batch = self._read(bid)
        changed = self._reconcile_missing_assets(batch) if batch.get('workspace') else False
        interrupted = False
        if batch.get('workspace') and bid not in self.workers:
            for item in batch['items']:
                if item['state'] not in ACTIVE and not (item['state']=='interrupted' and any(
                        a['state'] in {'pending','running'} for a in item['assets'].values())):
                    continue
                for key, asset in item['assets'].items():
                    if asset['state'] in {'pending', 'running'}:
                        asset['state'] = ('unknown' if key == 'text' and
                            item.get('cloud_attempt', {}).get('state') in {'sending', 'received', 'unknown'} else 'failed')
                item.update(state='recovery_required' if self._pending_extraction_result(bid, item)
                            or any(a['state']=='unknown' for a in item['assets'].values()) else 'interrupted',
                            message='上次处理已中断，已有结果保留；未自动重新发送')
                changed = interrupted = True
        if changed:
            batch['state'] = ('interrupted' if interrupted else
                              'succeeded' if all(i['state'] in SETTLED for i in batch['items']) else
                              'paused' if any(i['state'] == 'paused' for i in batch['items']) else 'partial')
            self._save(batch)
        return batch

    def _pending_extraction_result(self, bid, item):
        return bool(item.get('attempt_id') and item.get('applied_attempt_id') != item['attempt_id']
                    and self._extraction_result_path(bid, item).exists())

    def _extraction_batch(self, bid, expected_hash):
        self._same_library()
        batch = self._extraction_live(bid)
        if type(batch.get('workspace')) is not dict or batch['workspace'].get('version') != 1:
            raise ValueError('此任务格式暂不支持操作，原记录保留')
        if batch['plan_hash'] != expected_hash:
            raise library.RevisionConflict('任务选择已变化，请刷新后重试')
        return batch

    def _command(self, batch, request_id, payload):
        identity(request_id)
        folder = self.root / 'extraction-commands'
        if folder.is_symlink():
            raise ValueError('任务操作记录位置不安全')
        folder.mkdir(mode=0o700, exist_ok=True)
        key = documents.digest(encode([batch['batch_id'], request_id]))
        path = folder / (key + '.json')
        intent = documents.digest(encode(payload))
        if path.is_symlink():
            raise ValueError('任务操作记录不安全')
        if path.exists():
            import json
            old = json.loads(path.read_text(encoding='utf-8'))
            if old.get('intent') != intent:
                raise library.RevisionConflict('同一操作编号不能改用于其他选择')
            return path, intent, True
        return path, intent, False

    @staticmethod
    def _commit_command(path, intent):
        library._write_atomic_text(path, encode(dict(intent=intent)))

    def configure_extraction(self, request_id, batch_id, expected_hash, changes=None, engine=None):
        with self.lock:
            self._same_library()
            batch = self._extraction_live(batch_id)
            record, intent, replay = self._command(batch, request_id, dict(changes=changes, engine=engine))
            if replay:
                return self.show(batch_id, limit=50)
            batch = self._extraction_batch(batch_id, expected_hash)
            if changes is None:
                changes = []
            if type(changes) is not list or (not changes and engine is None):
                raise ValueError('请明确需要修改的作品或模型')
            updates, seen = [], set()
            for change in changes:
                if type(change) is not dict or set(change) - {
                        'index', 'version', 'outputs', 'prefer_subtitles', 'media_directory'}:
                    raise ValueError('作品设置参数无效')
                index = change.get('index')
                if type(index) is not int or not 0 <= index < len(batch['items']) or index in seen:
                    raise ValueError('作品编号无效或重复')
                seen.add(index)
                item = batch['items'][index]
                if change.get('version') != item_version(item):
                    raise library.RevisionConflict('这条作品已变化，请刷新后重新选择')
                if (item['state'] in ACTIVE | {'paused','duplicate'} or item.get('hidden')
                        or self._pending_extraction_result(batch_id, item)):
                    raise ValueError('本条已开始或结果待核对，当前尝试的选择保持不变')
                selection = deepcopy(item['selection'])
                if 'outputs' in change:
                    selection['outputs'] = selected_outputs(change['outputs'])
                if 'prefer_subtitles' in change:
                    if type(change['prefer_subtitles']) is not bool:
                        raise ValueError('优先字幕必须为布尔值')
                    if item['assets']['text']['state'] == 'unknown':
                        raise ValueError('文案结果待核对，已发送请求的字幕策略保持不变')
                    selection['prefer_subtitles'] = change['prefer_subtitles']
                if 'media_directory' in change:
                    self._check_media_directory(change['media_directory'])
                    selection['media_directory'] = change['media_directory']
                if item.get('local_source') and selection['outputs'] not in [[], ['text']]:
                    raise ValueError('本地文件只支持提取文案')
                updates.append((item, selection))
            if engine is not None:
                from ..engines.providers import PRESETS
                if (engine not in PRESETS or PRESETS[engine].retired
                        or any(i.get('attempt_id') for i in batch['items'])
                        or batch_id in self.workers):
                    raise ValueError('本批已开始或模型不可用，不能修改本次模型')
                batch['engine'] = engine
                batch['plan']['engine'] = engine
                batch['plan_hash'] = documents.digest(encode(batch['plan']))
            for item, selection in updates:
                item['selection'] = selection
                item.update(state='draft' if self._missing_outputs(item) else
                            'recovery_required' if any(a['state']=='unknown' for a in item['assets'].values()) else
                            'succeeded' if any(a['state']=='ready' for a in item['assets'].values()) else 'draft',
                            message='', error=None)
            self._save(batch)
            self._commit_command(record, intent)
            reply = self.show(batch_id, limit=50)
            reply['changed_items'] = [self._extraction_public_item(batch_id,i) for i,_ in updates[:50]]
            return reply

    def control_extraction(self, request_id, batch_id, expected_hash, action,
                           item_indices=None, expected_versions=None,
                           network_consent=False, cloud_upload_consent=False,
                           retry_unknown_confirmed=False, excluded_indices=None, dismissal_id=None,
                           expected_visibility=None):
        if action not in {'start', 'pause', 'resume', 'resume_selected', 'cancel', 'retry', 'check', 'retry_unknown', 'dismiss', 'restore'}:
            raise ValueError('任务操作无效')
        if any(type(v) is not bool for v in
               (network_consent, cloud_upload_consent, retry_unknown_confirmed)):
            raise ValueError('任务确认参数无效')
        with self.lock:
            batch = self._extraction_batch(batch_id, expected_hash)
            if dismissal_id is not None:
                identity(dismissal_id)
                if action!='restore' or item_indices is not None or excluded_indices is not None:
                    raise ValueError('撤销编号仅可用于恢复原次移出的作品')
            payload = dict(action=action, item_indices=item_indices, expected_versions=expected_versions,
                           network_consent=network_consent, cloud_upload_consent=cloud_upload_consent,
                           retry_unknown_confirmed=retry_unknown_confirmed, excluded_indices=excluded_indices,
                           dismissal_id=dismissal_id)
            if expected_visibility is not None:
                payload['expected_visibility'] = expected_visibility
            record, intent, replay = self._command(batch, request_id, payload)
            if replay:
                return self.show(batch_id, limit=50)
            if expected_visibility is not None and (type(expected_visibility) is not str
                    or expected_visibility != visibility_version(batch['items'])):
                raise library.RevisionConflict('本批作品范围已变化，请刷新后重新选择')
            previous_visibility = visibility_version(batch['items'])
            if item_indices is None:
                chosen = ([i for i in batch['items'] if i.get('hidden_by')==dismissal_id and i.get('hidden')]
                          if dismissal_id else [i for i in batch['items'] if not i.get('hidden')])
                if excluded_indices is not None:
                    if (type(excluded_indices) is not list
                            or any(type(i) is not int or not 0 <= i < len(batch['items']) for i in excluded_indices)
                            or len(set(excluded_indices)) != len(excluded_indices)):
                        raise ValueError('排除的作品编号无效')
                    excluded = set(excluded_indices)
                    chosen = [i for i in chosen if i['index'] not in excluded]
            else:
                if excluded_indices is not None:
                    raise ValueError('明确选择与整批排除不能同时使用')
                if (type(item_indices) is not list
                        or any(type(n) is not int or not 0 <= n < len(batch['items']) for n in item_indices)
                        or len(set(item_indices)) != len(item_indices)):
                    raise ValueError('作品选择无效')
                if type(expected_versions) is not dict or set(expected_versions) != {str(n) for n in item_indices}:
                    raise ValueError('请提供所选作品的当前版本')
                chosen = [batch['items'][n] for n in item_indices]
                if any(expected_versions[str(i['index'])] != item_version(i) for i in chosen):
                    raise library.RevisionConflict('所选作品已变化，请刷新后重试')
            if action in {'retry_unknown', 'restore', 'check'} and item_indices is None and not dismissal_id:
                raise ValueError('此操作必须明确指定作品，不能隐式扩大到整批')
            runnable, recoverable = [], []
            for item in chosen:
                if action == 'restore':
                    continue
                if item.get('hidden') or item['state'] in {'duplicate', 'skipped'}:
                    continue
                if action == 'check' and item['state'] in ACTIVE:
                    raise ValueError('本条仍在处理中，请等待收尾后再核对或补保存')
                if item['state'] in ACTIVE:
                    continue
                if action in {'start', 'retry'}:
                    if self._pending_extraction_result(batch_id, item):
                        recoverable.append(item)
                    elif (item['state'] in {'draft', 'failed', 'cancelled', 'interrupted', 'succeeded','recovery_required'}
                            and self._missing_outputs(item)):
                        runnable.append(item)
                elif action in {'resume','resume_selected'} and item['state'] == 'paused':
                    if ((action=='resume_selected' or item_indices is not None or item.get('auto_paused'))
                            and self._missing_outputs(item)):
                        runnable.append(item)
                elif action == 'retry_unknown' and any(a['state'] == 'unknown' for a in item['assets'].values()):
                    if not retry_unknown_confirmed:
                        raise ValueError('结果不明的请求可能已计费，请单独确认再次上传')
                    runnable.append(item)
            for item in runnable:
                outputs = (list((item.get('attempt_selection') or item['selection']).get('outputs', []))
                           if action in {'resume','resume_selected'} else self._missing_outputs(item))
                if action == 'retry_unknown':
                    outputs = [k for k in OUTPUTS if item['assets'][k]['state'] == 'unknown']
                if item.get('local_source') is None and not network_consent:
                    raise ValueError('请确认本次所选作品需要联网提取')
                if ('text' in outputs and batch['engine'] in cloud_ids()
                        and not cloud_upload_consent):
                    raise ValueError('需要识别时音频会上传所选服务商，请明确确认')
                if any(k in outputs for k in ('video', 'cover')):
                    if not item['selection']['media_directory']:
                        raise ValueError('请先为需要视频或封面的作品选择保存位置')
                    self._check_media_directory(item['selection']['media_directory'])
            for item in chosen:
                if action in {'pause', 'cancel', 'dismiss'}:
                    if item['state'] in {'queued', 'draft'} or (
                            item['state'] == 'paused' and action in {'cancel', 'dismiss'}):
                        item.update(state='paused' if action == 'pause' else 'cancelled',
                                    auto_paused=action == 'pause' and item_indices is None)
                    elif item['state'] in {'running', 'settling'} and action != 'pause':
                        self._extraction_cancel_events.setdefault((batch_id, item['index']), threading.Event()).set()
                        item.update(state='settling', message='正在结束本项，已有结果保留')
                    if action == 'dismiss':
                        item['hidden'] = True
                        item['hidden_by'] = request_id
                elif action == 'restore':
                    item['hidden'] = False
                    item.pop('hidden_by',None)
                elif action == 'check':
                    if self._pending_extraction_result(batch_id, item):
                        item.update(state='queued', recover_only=True)
                    else:
                        item['message'] = '暂未取得可核验的完整结果，请到服务后台核对；未再次上传音频'
            for item in recoverable:
                item.update(state='queued',recover_only=True)
            for item in runnable:
                if action not in {'resume','resume_selected'} or not item.get('attempt_id'):
                    if item.get('attempt_id'):
                        item['attempts'].append(dict(id=item['attempt_id'], selection=item.get('attempt_selection'),
                                                     cloud_attempt=item.get('cloud_attempt'), assets=deepcopy(item['assets'])))
                    item['attempt_id'] = documents.digest(encode([request_id, item['index']]))[:32]
                    outputs = ([k for k in OUTPUTS if item['assets'][k]['state'] == 'unknown']
                               if action == 'retry_unknown' else self._missing_outputs(item))
                    item['attempt_selection'] = {**deepcopy(item['selection']), 'outputs': outputs}
                    item.pop('cloud_attempt', None)
                    for key in outputs:
                        item['assets'][key] = dict(state='pending')
                item.update(state='queued', auto_paused=False, error=None, progress=0)
                item.pop('activity', None)
            self._save(batch)
            self._commit_command(record, intent)
            if any(i['state'] == 'queued' and not i.get('hidden') for i in batch['items']):
                self._start_extraction(batch)
            reply = self.show(batch_id, limit=50)
            reply['previous_visibility'] = previous_visibility
            if item_indices is not None:
                reply['changed_items'] = [self._extraction_public_item(batch_id,i) for i in chosen[:50]]
            if action == 'dismiss':
                reply.update(dismissal_id=request_id,dismissed_count=sum(i.get('hidden',False) for i in chosen))
                reply['dismissed'] = {str(i['index']):item_version(i) for i in chosen[:50] if i.get('hidden')}
            return reply

    def _extraction_result_path(self, bid, item):
        path=self.root / (bid + '-' + str(item['index']) + '-' + item['attempt_id'] + '.extract-result.json')
        if path.is_symlink():
            raise ValueError('已有结果位置不安全，未覆盖或重新提取')
        return path

    def _start_extraction(self, batch):
        bid = batch['batch_id']
        if bid in self.workers:
            return
        self._active_extractions[bid] = batch
        batch['state'] = 'running'
        self._save(batch)
        worker = threading.Thread(target=self._run_extraction, args=(batch,), name='lulu-extraction-batch', daemon=True)
        self.workers[bid] = worker
        try:
            worker.start()
        except BaseException:
            self.workers.pop(bid, None)
            self._active_extractions.pop(bid, None)
            batch['state'] = 'interrupted'
            self._save(batch)
            raise

    def _run_extraction(self, batch):
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        from ..engines.asr_resource import batch_context, finish_batch
        from ..engines.cloud_limits import RESOURCES
        bid, engine = batch['batch_id'], None
        cloud = batch['engine'] in cloud_ids()
        failed_configuration = False
        try:
            capacity = RESOURCES.capacity(len(batch['items']), None) if cloud else 1
            with batch_context(bid), ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='lulu-extract') as pool:
                pending = set()
                while True:
                    with self.lock:
                        eligible = [i for i in batch['items'] if i['state'] == 'queued' and not i.get('hidden')]
                    if engine is None and cloud and self.default_runner and any(
                            'text' in (i.get('attempt_selection') or {}).get('outputs', [])
                            and not self._extraction_result_path(bid, i).exists() for i in eligible):
                        try:
                            engine = self._prepare_cloud_engine(batch['engine'])
                            self._seed_extraction_usage(engine, batch['engine'])
                        except Exception:
                            failed_configuration = True
                            with self.lock:
                                for item in eligible:
                                    if 'text' in (item.get('attempt_selection') or {}).get('outputs', []):
                                        item.update(state='interrupted', message='云端转录配置未能读取，本次尚未上传；请在模型设置核对后重试')
                                        self._save(batch, item)
                            eligible = [i for i in eligible if i['state'] == 'queued']
                    with self.lock:
                        for item in eligible[:max(0, capacity-len(pending))]:
                            if item['state'] != 'queued':
                                continue
                            item.update(state='running', message='准备提取', progress=0,
                                        activity=extraction_activity(item))
                            self._save(batch, item)
                            self._extraction_cancel_events[(bid, item['index'])] = threading.Event()
                            pending.add(pool.submit(self._dispatch_extraction_item, batch, item, engine))
                    if not pending:
                        with self.lock:
                            if not any(i['state'] == 'queued' and not i.get('hidden') for i in batch['items']):
                                break
                        continue
                    done, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
        except Exception:
            with self.lock:
                batch['message'] = '处理已中断，已有结果保留；请刷新核对后显式恢复'
                for item in batch['items']:
                    if item['state'] in ACTIVE:
                        item.update(state='interrupted', message=batch['message'])
        finally:
            if engine is not None:
                engine.close()
            finish_batch(bid)
            with self.lock:
                for item in batch['items']:
                    if item['state'] in {'running', 'settling'}:
                        item['state'] = 'interrupted'
                batch['state'] = ('succeeded' if all(i['state'] in SETTLED for i in batch['items'])
                                  else 'paused' if any(i['state'] == 'paused' for i in batch['items']) else 'partial')
                self.workers.pop(bid, None)
                self._active_extractions.pop(bid, None)
                self._save(batch)
                # A request can arrive between the last empty check and teardown.
                if not failed_configuration and any(i['state'] == 'queued' and not i.get('hidden') for i in batch['items']):
                    self._start_extraction(batch)

    def _dispatch_extraction_item(self, batch, item, cloud_engine):
        from ..engines.asr_resource import batch_context
        cancel = self._extraction_cancel_events[(batch['batch_id'],item['index'])]
        with batch_context(batch['batch_id'],wait_cancelled=cancel.is_set):
            self._run_extraction_item(batch, item, cloud_engine)

    def _seed_extraction_usage(self, engine, provider):
        attempts = []
        with self.lock:
            for path in self._batch_paths():
                if path.name.endswith(('.result.json','.state.json','.extract-result.json')):
                    continue
                prior = self._read(path.stem)
                if prior.get('engine') != provider:
                    continue
                for item in prior['items']:
                    history = [a.get('cloud_attempt', {}) for a in item.get('attempts', [])]
                    for attempt in [*history, *item.get('cloud_attempt_history', []), item.get('cloud_attempt', {})]:
                        if attempt and attempt.get('model') == engine.model:
                            attempts.append(attempt)
        engine.gate.seed_audio(attempts)

    def _run_extraction_item(self, batch, item, cloud_engine):
        import json
        from ..task import TaskExecutionPolicy
        from ..collect.preferences import MaterialPreferenceSnapshot
        from ..collect.cover import CoverLocalizationService
        from ..models_manager import app_data_dir
        from .. import extraction
        bid = batch['batch_id']
        cancel = self._extraction_cancel_events.setdefault((bid, item['index']), threading.Event())
        chosen = item.get('attempt_selection') or item['selection']
        requested = [k for k in chosen['outputs'] if item['assets'][k]['state'] != 'ready']
        path = self._extraction_result_path(bid, item)
        def cancelled():
            # Ordinary cancellation stops unsent work. Once a cloud call may
            # have been accepted, keep its result and finish saving it.
            return cancel.is_set() and not (batch['engine'] in cloud_ids() and 'text' in requested
                and item.get('cloud_attempt', {}).get('state') in {'sending', 'received', 'unknown'})
        def progress(message, fraction=-1):
            with self.lock:
                if item['state'] != 'settling':
                    item['message'] = str(message)[:500]
                if not (type(fraction) in {int, float} and 0 <= fraction <= 1):
                    item['activity']['fraction'] = None
                if type(fraction) in {int, float} and 0 <= fraction <= 1:
                    item['progress'] = round(fraction*95)
                key = (bid, item['index'])
                if time.monotonic()-self._progress_times.get(key, 0) > .5:
                    self._save(batch, item)
                    self._progress_times[key] = time.monotonic()
        def stage_changed(step):
            with self.lock:
                activity = item['activity']
                stage = ('text' if step in {'subtitles', 'transcription'} else
                         'cover' if step == 'cover' else 'save' if step == 'saving' else
                         activity['stage'] if step == 'result' else 'prepare')
                if activity['step'] != step:
                    activity.update(stage=stage, step=step, fraction=None)
                    self._save(batch, item)
        def stage_progress(message, fraction=-1):
            with self.lock:
                # Cloud waiting markers are not measured recognition progress.
                known = type(fraction) in {int, float} and 0 <= fraction <= 1
                item['activity']['fraction'] = (fraction if known and not (
                    batch['engine'] in cloud_ids() and item['activity']['stage'] == 'text') else None)
        def observe(event, data):
            with self.lock:
                if event == 'sending':
                    item['cloud_attempt'] = dict(data, state='sending')
                elif event in {'received', 'rejected'}:
                    item.setdefault('cloud_attempt', {}).update(data, state=event, finished_at=time.time())
                self._save(batch, item)
        def asset_changed(key, state, value=None):
            with self.lock:
                if key not in requested or item['assets'][key]['state'] == 'ready':
                    return
                item['assets'][key] = dict(state=state, **({'path':value} if value else {}))
                self._save(batch, item)
        def checkpoint(result):
            raw = asdict(result)
            library._write_atomic_text(path, encode(dict(transcript=raw,
                sha256=documents.digest(encode(raw)), complete=False)))
        try:
            if path.exists():
                from ..updater.secure_io import read_nofollow
                saved = json.loads(read_nofollow(path,mode=0o600,maximum=32*1024*1024))
                if documents.digest(encode(saved['transcript'])) != saved['sha256']:
                    raise ValueError('已有结果校验失败，未重新提取')
                result = Transcript(**{**saved['transcript'], 'segments':[Segment(**s) for s in saved['transcript']['segments']]})
            elif item.pop('recover_only', False):
                raise ValueError('本机暂无完整结果可补保存，未重新上传')
            else:
                if item.get('local_source'):
                    from ..live_recording.media import fingerprint
                    from .operations import actual_file
                    source = actual_file(item['source'], maximum=2*1024**3)
                    if source.stat().st_size != item['local_source']['bytes'] or fingerprint(source) != item['local_source']['sha256']:
                        raise ValueError('原文件已变化，未处理替换后的材料')
                policy = TaskExecutionPolicy(origin='single_link', mode='materialize', stable_key=item.get('stable_key') or '',
                    material_snapshot=MaterialPreferenceSnapshot('video' if 'video' in requested else 'audio',
                                                                 'video' in requested, 'cover' in requested),
                    media_destination='user_directory' if any(k in requested for k in ('video', 'cover')) else 'none',
                    media_directory=chosen.get('media_directory'), prefer_subtitles=chosen['prefer_subtitles'],
                    engine_name=batch['engine'], requested_outputs=tuple(requested))
                if not self.default_runner:
                    result = self.runner({**item, 'requested_outputs': requested}, batch, progress, cancel.is_set)
                else:
                    result = extraction.acquire_source(item['source'], execution_policy=policy,
                        engine_name=batch['engine'], progress=progress,
                        stage_changed=stage_changed, stage_progress=stage_progress,
                        is_cancelled=cancelled,
                        stop_new_work=cancel.is_set if batch['engine'] in cloud_ids() and 'text' in requested else None,
                        cover_workflow=CoverLocalizationService(app_data_dir()/'collection'/'covers'),
                        bounded_cloud=batch['engine'] in cloud_ids(), engine_override=cloud_engine,
                        cloud_observer=observe, max_input_bytes=2*1024**3,
                        asset_changed=asset_changed, result_checkpoint=checkpoint)
                raw = asdict(result)
                library._write_atomic_text(path, encode(dict(transcript=raw, sha256=documents.digest(encode(raw)), complete=True)))
            stage_changed('saving')
            progress('正在保存所选内容')
            with self.lock, library._transaction():
                self._same_library()
                title = item.get('resolved_title') or item['title']
                if not item.get('local_source'):
                    for candidate in (result.title, (result.meta or {}).get('title')):
                        if isinstance(candidate, str) and candidate.strip():
                            title = candidate.strip()[:500]
                            item['resolved_title'] = title
                            break
                for key, value in (result.meta or {}).get('extraction_assets', {}).items():
                    if key in requested and key in {'video', 'cover'} and self._usable_asset(value):
                        item['assets'][key] = dict(state='ready', path=value)
                if 'text' in requested and result.text.strip():
                    if result.engine != batch['engine'] and not str(result.engine).startswith('平台'):
                        raise ValueError('转录模型与本次选择不一致，未保存文稿')
                    eid = 'transcript_' + documents.digest(encode([bid,item['index'],item['attempt_id']]))
                    library.save(result, title, batch['folder_id'], entry_id=eid)
                    library.finish_transcript_document(eid, result, title)
                    item['document_id'] = eid
                    item['assets']['text'] = dict(state='ready', document_id=eid)
                    if item.get('stable_key'):
                        from ..collect.repository import CollectionRepository
                        repository=CollectionRepository()
                        current=next((r for r in repository.load().items if r.stable_key==item['stable_key']),None)
                        if current and current.transcript_status!='completed':
                            repository.compare_and_swap_fields(item['stable_key'],
                                expected={'transcript_status':current.transcript_status},
                                updates={'transcript_status':'completed'})
                failure = (result.meta or {}).get('extraction_text_failure') or {}
                if failure.get('kind') == 'account':
                    for waiting in batch['items']:
                        if waiting['state']=='queued' and 'text' in (waiting.get('attempt_selection') or {}).get('outputs', []):
                            waiting.update(state='interrupted', message='转录账号权限或额度不足，已停止后续发送；已有结果保留')
                            self._save(batch, waiting)
                for key in requested:
                    if item['assets'][key]['state'] != 'ready':
                        uncertain = key == 'text' and (failure.get('kind') == 'unknown'
                            or item.get('cloud_attempt', {}).get('state') in {'sending', 'unknown'})
                        item['assets'][key] = dict(state='unknown' if uncertain else 'failed')
                incomplete = any(item['assets'][k]['state'] != 'ready' for k in requested)
                item.update(state='recovery_required' if any(a['state']=='unknown' for a in item['assets'].values()) else
                            'failed' if incomplete else 'succeeded', progress=100 if not incomplete else item['progress'],
                            message='本次所选内容已保存' if not incomplete else '已保留成功内容，请只处理未完成项',
                            error='；'.join((result.meta or {}).get('extraction_errors', [])) or None)
                item.pop('recover_only', None)
                item['activity']['finished_at'] = time.time()
                item['applied_attempt_id'] = item['attempt_id']
                self._save(batch, item)
        except Exception as exc:
            with self.lock:
                item['activity']['finished_at'] = time.time()
                for key in requested:
                    if item['assets'][key]['state'] == 'ready':
                        continue
                    uncertain = key == 'text' and (isinstance(exc, CloudFailure) and exc.kind == 'unknown'
                        or item.get('cloud_attempt', {}).get('state') in {'sending', 'received', 'unknown'})
                    item['assets'][key] = dict(state='unknown' if uncertain else 'failed')
                item.update(state='recovery_required' if path.exists() or any(a['state']=='unknown' for a in item['assets'].values())
                            else 'cancelled' if cancel.is_set() else 'failed',
                            message='本项未完成，已有结果保留；请核对后显式继续',
                            error=str(exc)[:1000] if isinstance(exc, (ValueError, InterruptedError)) else '提取未完成')
                self._save(batch, item)
        finally:
            self._extraction_cancel_events.pop((bid, item['index']), None)

    def _extraction_public_item(self, bid, item):
        item = deepcopy(item)
        # The submitted title stays in the frozen plan; discovered metadata is display state.
        item['title'] = item.pop('resolved_title', None) or item['title']
        item['version'] = item_version(item)
        item['save_pending'] = item['state'] not in ACTIVE and self._pending_extraction_result(bid, item)
        item['attempt_count'] = len(item.pop('attempts', [])) + bool(item.get('attempt_id'))
        return item

    def show_extraction(self, batch_id, offset=0, limit=None, include_hidden=False, locate_index=None):
        if type(include_hidden) is not bool or (locate_index is not None and type(locate_index) is not int):
            raise ValueError('任务查询范围无效')
        with self.lock:
            batch = deepcopy(self._extraction_live(batch_id))
            all_items = batch['items']
            shown = [i for i in all_items if include_hidden or not i.get('hidden')]
            if locate_index is not None:
                position = next((n for n,i in enumerate(shown) if i['index']==locate_index), None)
                if position is None:
                    raise ValueError('此条已移出列表或不在本批')
                offset = max(0, position//25*25)
                batch['focus_position'] = position
                batch['focus_index'] = locate_index
            counts = {s:sum(i['state']==s for i in all_items if not i.get('hidden')) for s in
                      ('draft','queued','running','settling','paused','succeeded','failed','interrupted','cancelled','duplicate','recovery_required')}
            batch.pop('plan', None)
            page = shown[offset:offset+limit] if limit is not None else shown[offset:]
            batch['items'] = [self._extraction_public_item(batch_id,i) for i in page]
            batch.update(total=len(shown),original_total=len(all_items), visible_total=sum(not i.get('hidden') for i in all_items),
                         visibility_version=visibility_version(all_items),
                         offset=offset, has_more=limit is not None and offset+limit<len(shown), counts=counts,
                         progress=round(100*counts['succeeded']/max(1,sum(counts.values())),1),
                         unconfirmed_count=sum(any(a['state']=='unknown' for a in i['assets'].values()) for i in all_items),
                         upload_notice='需要语音识别时音频会上传至所选服务商并按账户计费' if batch['engine'] in cloud_ids() else '')
            batch['needs_cloud_upload'] = batch['engine'] in cloud_ids() and any(
                not i.get('hidden') and 'text' in i['selection']['outputs']
                and i['assets']['text']['state'] not in {'ready', 'unknown'} for i in all_items)
            batch['model_locked'] = any(i.get('attempt_id') for i in all_items)
            batch['auto_paused_count'] = sum(not i.get('hidden') and i['state']=='paused'
                                             and i.get('auto_paused',False) for i in all_items)
            batch['startable_count'] = sum(not i.get('hidden') and i['state'] not in ACTIVE|{'paused','duplicate'}
                and bool(self._missing_outputs(i) or self._pending_extraction_result(batch_id,i)) for i in all_items)
            return batch
