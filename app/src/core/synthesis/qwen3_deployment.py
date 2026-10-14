"""Persistent, dependency-aware Qwen deployment. No network at import or status read."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import shutil
import time
import uuid
from . import qwen3_weights as qw, runtime_packages as rp

SESSION = uuid.uuid4().hex

PENDING = {'queued', 'installing', 'paused', 'failed', 'cancelling', 'cleanup_failed'}
RUNTIME_UNPACKED_BYTES = 937002403  # Conservative pinned runtime allowance.


def root():
    return rp.model_dir(qw.MODEL_ID)/'models'


def read_state(base=None):
    base = Path(base) if base else root()
    path = base.parent/'deployment.json'
    if not path.exists(): return {'schema': 1, 'features': {}, 'components': {}}
    data = json.loads(path.read_text())
    if data.get('schema') != 1: raise RuntimeError('千问安装记录版本不兼容，请更新 Lulu。')
    return data


@contextmanager
def edit_state(base=None):
    base = Path(base) if base else root()
    # A short independent lock lets pause/cancel/queue reach the single download owner.
    with qw.deployment_lock(base, lock_name='.qwen-state.lock'):
        data = read_state(base)
        yield data
        rp._write_json_atomic(base.parent/'deployment.json', data)


def request(names, action='queued', base=None):
    names = list(names)
    if any(n not in qw.FEATURES for n in names): raise ValueError('未知千问功能')
    if action not in {'queued', 'paused', 'cancelling'}: raise ValueError(action)
    with edit_state(base) as state:
        for name in names:
            state['features'][name] = {'state': action, 'updated': time.time(), 'session': SESSION, 'pid': os.getpid()}


def _feature(name, status, error='', base=None):
    with edit_state(base) as state:
        current = state['features'].get(name, {}).get('state')
        if current in {'paused', 'cancelling'} and status in {'installing', 'failed'}: return
        state['features'][name] = {'state': status, 'error': error, 'updated': time.time(), 'session': SESSION, 'pid': os.getpid()}


def _component(name, status, base=None):
    with edit_state(base) as state: state['components'][name] = status


def consumers(base=None, exclude=()):
    base = Path(base) if base else root()
    state = read_state(base)['features']
    return [n for n in qw.FEATURES if n not in exclude and
            (qw.is_installed(base, n) or (base/n).exists() or state.get(n, {}).get('state') in PENDING)]


def _live_download(value):
    if value.get('state') not in {'queued', 'installing'}: return False
    if value.get('session') == SESSION: return True
    try:
        os.kill(int(value.get('pid', 0)), 0)
        return int(value.get('pid', 0)) > 0
    except (OSError, ValueError, TypeError): return False


def _shared_entries():
    seen = set(); common = qw._shared_keys()
    for name, lock in qw.MODELS.items():
        for entry in lock['files']:
            key = qw._key(entry)
            if key in common and key not in seen:
                seen.add(key); yield name, entry


def plan(names, base=None, cancel=lambda: False):
    """Runs in a worker: validate local bytes before quoting new network costs."""
    base = Path(base) if base else root()
    def check():
        if cancel(): raise rp.CancelledError()
    selected = list(dict.fromkeys(names))
    if any(n not in qw.FEATURES for n in selected): raise ValueError('未知千问功能')
    state = read_state(base)['features']
    active = [n for n, value in state.items() if _live_download(value) and n not in selected]
    shared_keys = qw._shared_keys(); seen = set(); missing = 0; shared_missing = 0; disk = 0; extras = []
    for name in selected:
        check()
        if qw.is_installed(base, name): continue
        own_network = 0; own_added = 0
        own_stage = sum(e['size'] for e in qw.MODELS[name]['files'] if qw._key(e) not in shared_keys)
        for entry in qw.MODELS[name]['files']:
            key = qw._key(entry)
            if key in seen: continue
            seen.add(key)
            source = qw._shared_source(base, entry, check) if key in shared_keys else None
            if source is None:
                for candidate in (base/name/entry['path'], qw._snapshot(base, name)/entry['path']):
                    if qw._valid(candidate, entry, check): source = candidate; break
            if not (base/name/entry['path']).exists():
                delta = entry['size'] if key not in shared_keys or source is None or source != base.parent/'shared'/entry['sha256'] else 0
                disk += delta
                if key not in shared_keys: own_added += delta
            if source is None:
                missing += entry['size']
                if key in shared_keys: shared_missing += entry['size']
                else: own_network += entry['size']
        extras.append(own_stage + own_network - own_added)
    runtime_ready = rp.is_runtime_installed(qw.MODEL_ID)
    env = rp._trusted_manifest(rp.TTS_RUNTIMES[qw.MODEL_ID], rp.current_platform())['size'] if not runtime_ready else 0
    if active: missing -= shared_missing  # Owned by the existing common job.
    new_download = missing + (env if not active else 0)
    added = disk + (RUNTIME_UNPACKED_BYTES if not runtime_ready and not active else 0)
    # Features install serially: reserve only the largest extra download/candidate,
    # rather than three simultaneous staging copies. Old files remain for rollback.
    extra = max([shared_missing, env, *extras])
    required = added + extra + 256*1024**2
    return {'names': selected, 'download': new_download, 'disk': added, 'free': required,
            'common': '正在准备，共用' if active else '已就绪，复用' if runtime_ready and not shared_missing else '自动安装',
            'partial': any((base.parent/'downloads'/n).exists() for n in selected)}


def _common_paths(base):
    return [base.parent/'runtime', base.parent/'shared', base.parent/'downloads'/'shared',
            base.parent/rp._STATE, *rp._runtime_cache_paths(qw.MODEL_ID)]


def removable_bytes(paths, keep_paths=()):
    """Count physical blocks only when no remaining hard link owns them."""
    counts = {}; keep = set()
    for path in keep_paths:
        for p in ([path] if path.is_file() else path.rglob('*') if path.exists() else []):
            if p.is_file() and not p.is_symlink():
                s = p.stat(); keep.add((s.st_dev, s.st_ino))
    for path in paths:
        for p in ([path] if path.is_file() else path.rglob('*') if path.exists() and not path.is_symlink() else []):
            if p.is_file() and not p.is_symlink():
                s = p.stat(); key = (s.st_dev, s.st_ino)
                prior = counts.get(key, (0, s)); counts[key] = (prior[0]+1, s)
    return sum(getattr(s, 'st_blocks', (s.st_size+511)//512)*512 for key, (count, s) in counts.items()
               if key not in keep and count >= s.st_nlink)


def removal_plan(name=None, base=None):
    base = Path(base) if base else root()
    others = consumers(base, exclude=[name] if name else [])
    common = _common_paths(base)
    own = [base/name, base.parent/'downloads'/name] if name else []
    pool_copy = 0
    if name and not any(qw.is_installed(base, n) for n in others):
        for entry in qw.MODELS[name]['files']:
            if qw._key(entry) in qw._shared_keys() and not (base.parent/'shared'/entry['sha256']).exists():
                source = base/name/entry['path']
                if source.exists(): pool_copy += getattr(source.stat(), 'st_blocks', (entry['size']+511)//512)*512
    own_bytes = removable_bytes(own)
    return {'consumers': others, 'own': max(0, own_bytes-pool_copy) if others else own_bytes,
            'retain': max(0, own_bytes-pool_copy), 'common': sum(rp._tree_bytes(p) for p in common)+pool_copy,
            'total': removable_bytes(own + (common if not others else []))}


def _remove(name, base, retain_common=False):
    # Caller holds the same exclusion as generation and all deployment operations.
    others = consumers(base, exclude=[name])
    if (others or retain_common) and not any(qw.is_installed(base, n) for n in others):
        for entry in qw.MODELS[name]['files']:
            if qw._key(entry) in qw._shared_keys():
                source = qw._shared_source(base, entry, lambda: None)
                if source: qw._preserve_shared(base, entry, source, lambda: None)
    rp._remove_managed_path(base/name)
    rp._remove_managed_path(base.parent/'downloads'/name)
    qw.remove_install_receipt('qwen3-weights:'+name, qw.mm.app_data_dir()/'.artifact-trust')
    with edit_state(base) as state: state['features'].pop(name, None)
    # Recheck under the state lock, preventing a new installation intent during cleanup.
    with qw.deployment_lock(base, lock_name='.qwen-state.lock'):
        if not consumers(base) and not retain_common:
            for path in _common_paths(base): rp._remove_managed_path(path)
            rp.remove_install_receipt(rp._receipt_id('tts-runtime', qw.MODEL_ID), qw.mm.app_data_dir()/'.artifact-trust')
            data = read_state(base); data['components'] = {}; rp._write_json_atomic(base.parent/'deployment.json', data)


def remove(name=None, *, retain_common=False, base=None):
    base = Path(base) if base else root()
    with qw.deployment_lock(base, wait=False):
        try:
            if name: _remove(name, base, retain_common)
            else:
                with qw.deployment_lock(base, lock_name='.qwen-state.lock'):
                    if consumers(base): raise RuntimeError('还有功能使用共用组件，已保留。')
                    for path in _common_paths(base): rp._remove_managed_path(path)
                    rp.remove_install_receipt(rp._receipt_id('tts-runtime', qw.MODEL_ID), qw.mm.app_data_dir()/'.artifact-trust')
                    data = read_state(base); data['components'] = {}; rp._write_json_atomic(base.parent/'deployment.json', data)
        except Exception as exc:
            if name: _feature(name, 'cleanup_failed', str(exc), base)
            raise RuntimeError('清理未完成，可重试：'+str(exc)) from exc


def _prepare_shared(base, progress, cancel, local_models=None):
    """One durable common cache independent of the feature that initiated it."""
    def check():
        if cancel(): raise rp.CancelledError()
    grouped = {}
    for name, entry in _shared_entries():
        check(); source = qw._shared_source(base, entry, check)
        if source is None and local_models: source = Path(local_models)/name/entry['path']
        if source is not None: qw._preserve_shared(base, entry, source, check)
        else: grouped.setdefault(name, []).append(entry)
    for name, entries in grouped.items():
        lock = qw.MODELS[name]; cache = base.parent/'downloads'/'shared'
        progress('共用组件 · 下载通用模型文件', -1)
        rp._snapshot_download_cancellable(lock['repo'], lock['revision'], cache, cancel, allow_patterns=[e['path'] for e in entries])
        snapshot = cache/('models--'+lock['repo'].replace('/', '--'))/'snapshots'/lock['revision']
        for entry in entries: qw._preserve_shared(base, entry, snapshot/entry['path'], check)
    rp._remove_managed_path(base.parent/'downloads'/'shared')


def run(progress=None, stop=None, *, local_archive=None, local_models=None):
    """Drain explicit queued intentions; app restart never calls this automatically."""
    progress = progress or (lambda *_: None); stop = stop or (lambda: False); base = root()
    with qw.deployment_lock(base, stop, progress):
        base.mkdir(parents=True, exist_ok=True)
        for name in qw.FEATURES: qw._recover(base, name)
        def runnable(): return [n for n, value in read_state(base)['features'].items() if value['state'] in {'queued', 'installing'} and value.get('session') == SESSION]
        def common_cancel():
            return stop() or not any(_live_download(v) for v in read_state(base)['features'].values())
        try:
            if runnable():
                quoted = plan(runnable(), base)
                if shutil.disk_usage(base.parent).free < quoted['free']: raise RuntimeError('空间不足，请先腾出约 %.1f GB 可用空间。'%(quoted['free']/10**9))
                _component('runtime', 'installing', base)
                if not rp.is_runtime_installed(qw.MODEL_ID): rp.install_runtime(qw.MODEL_ID, progress, common_cancel, **({'local_archive': local_archive} if local_archive else {}))
                _component('runtime', 'ready', base); _component('shared', 'installing', base)
                _prepare_shared(base, progress, common_cancel, local_models)
                _component('shared', 'ready', base)
            while runnable() and not stop():
                name = runnable()[0]; _feature(name, 'installing', base=base)
                def cancel(): return stop() or read_state(base)['features'].get(name, {}).get('state') in {'paused', 'cancelling'}
                try:
                    qw._install(base, progress, cancel, local_models=local_models, name=name)
                    _feature(name, 'ready', base=base)
                except rp.CancelledError: pass
                except Exception as exc:
                    _feature(name, 'failed', str(exc), base); progress(qw.FEATURES[name]+'：'+str(exc), -1)
        except rp.CancelledError: pass
        except Exception as exc:
            for name in runnable(): _feature(name, 'failed', str(exc), base)
            with edit_state(base) as state:
                for key, value in state['components'].items():
                    if value == 'installing': state['components'][key] = 'failed'
            raise
        finally:
            with edit_state(base) as state:
                for value in state['features'].values():
                    if value['state'] in {'queued', 'installing'} and value.get('session') == SESSION: value['state'] = 'paused'
                for key, value in state['components'].items():
                    if value == 'installing': state['components'][key] = 'paused'
            for name, value in list(read_state(base)['features'].items()):
                if value['state'] == 'cancelling' and value.get('session') == SESSION:
                    try: _remove(name, base)
                    except Exception as exc: _feature(name, 'cleanup_failed', str(exc), base)
