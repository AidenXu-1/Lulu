"""Pinned Qwen feature installs, with one shared runtime and content reuse."""
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from .. import models_manager as mm
from ..artifact_trust import install_receipt_valid, write_install_receipt, remove_install_receipt, _receipt_path
from .qwen3_model_lock import MODELS

FEATURES = {'Base': '声音克隆', 'CustomVoice': '官方音色与表达', 'VoiceDesign': '文字设计声音'}
MODEL_ID = 'local_qwen3_tts'


def _trust(name):
    return MODELS[name]


def is_installed(root, name=None):
    names = [name] if name else MODELS
    return all(install_receipt_valid('qwen3-weights:'+n, Path(root)/n, _trust(n), mm.app_data_dir()/'.artifact-trust') for n in names)


@contextmanager
def deployment_lock(root, cancel=lambda: False, progress=lambda *_: None, *, lock_name=".qwen-deployment.lock", wait=True):
    """One writer across features/processes; the OS releases ownership on exit."""
    from . import runtime_packages as rp
    directory = Path(root).parent.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/lock_name
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise RuntimeError('千问安装位置不能是符号链接，请更换保存位置。')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0: os.write(fd, b'0')
        else:
            import fcntl
        while not locked:
            if cancel(): raise rp.CancelledError()
            try:
                if os.name == 'nt':
                    os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, PermissionError):
                if not wait: raise RuntimeError('千问正在生成或管理模型，请先完成操作并释放内存。')
                progress('等待共用组件准备完成…', -1); time.sleep(.1)
        yield
    finally:
        if locked and os.name == 'nt':
            os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _key(entry):
    return entry['sha256'], entry['size']


def _shared_keys():
    counts = {}
    for lock in MODELS.values():
        for key in {_key(e) for e in lock['files']}: counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _valid(path, entry, check):
    try:
        if not path.is_file() or path.stat().st_size != entry['size']: return False
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(8*1024*1024), b''):
                check(); h.update(block)
        return h.hexdigest() == entry['sha256']
    except OSError:
        return False


def _snapshot(root, name):
    lock = MODELS[name]
    return root.parent/'downloads'/name/('models--'+lock['repo'].replace('/', '--'))/'snapshots'/lock['revision']


def _shared_source(root, entry, check):
    pool = root.parent/'shared'/entry['sha256']
    if _valid(pool, entry, check): return pool
    for name, lock in MODELS.items():
        for other in lock['files']:
            if _key(other) != _key(entry): continue
            shared_cache = root.parent/'downloads'/'shared'/('models--'+lock['repo'].replace('/', '--'))/'snapshots'/lock['revision']
            for source in (root/name/other['path'], _snapshot(root, name)/other['path'], shared_cache/other['path']):
                if _valid(source, entry, check): return source
    return None


def _preserve_shared(root, entry, source, check):
    pool = root.parent/'shared'/entry['sha256']
    if source == pool: return pool
    pool.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.shared-', dir=pool.parent); os.close(fd)
    temp = Path(temp)
    try:
        # Copy to isolate shared data from old, mutable installations and HF cache.
        _copy_verified(source, temp, entry, check)
        temp.replace(pool)
    finally:
        temp.unlink(missing_ok=True)
    return pool


def _copy_verified(source, dest, entry, check):
    h = hashlib.sha256(); size = 0
    with source.open('rb') as inp, dest.open('wb') as out:
        for block in iter(lambda: inp.read(8*1024*1024), b''):
            check(); out.write(block); h.update(block); size += len(block)
    if size != entry['size'] or h.hexdigest() != entry['sha256']:
        raise RuntimeError('模型文件校验失败：'+entry['path'])


def _restore_receipt(transaction, name):
    receipt = _receipt_path(mm.app_data_dir()/'.artifact-trust', 'qwen3-weights:'+name)
    saved = transaction/'previous-receipt.json'
    if saved.exists():
        receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(saved, receipt); receipt.chmod(0o600)
    else:
        receipt.unlink(missing_ok=True)


def _recover(root, name):
    """Recover a process exit between the two directory renames before resuming."""
    for transaction in root.glob('.qwen-install-*'):
        journal = transaction/'pending.json'
        if not journal.is_file(): continue
        if json.loads(journal.read_text()).get('name') != name: continue
        destination = root/name; backup = transaction/'previous'
        if not is_installed(root, name) and backup.exists():
            if destination.exists(): destination.replace(transaction/'interrupted')
            backup.replace(destination)
            _restore_receipt(transaction, name)
        journal.unlink()
        shutil.rmtree(transaction)


def install_feature(name, progress=None, is_cancelled=None, *, local_archive=None, local_models=None):
    """Install exactly one feature. Check shared runtime under the same lock."""
    from . import runtime_packages as rp
    if name not in MODELS: raise ValueError('未知千问功能')
    progress = progress or (lambda *_: None); cancel = is_cancelled or (lambda: False)
    root = rp.model_dir(MODEL_ID)/'models'
    with deployment_lock(root, cancel, progress):
        if cancel(): raise rp.CancelledError()
        _recover(root, name)
        from .weights_reuse import recover_qwen_weights
        def check():
            if cancel(): raise rp.CancelledError()
        recover_qwen_weights(root, name, check, progress)
        if rp.is_runtime_installed(MODEL_ID):
            progress('共用运行环境已就绪，直接复用', .05)
        else:
            rp.install_runtime(MODEL_ID, progress, cancel, **({'local_archive': local_archive} if local_archive else {}))
        _install(root, progress, cancel, local_models=local_models, name=name)
        progress(FEATURES.get(name, name)+'已就绪', 1.0)


def install(root, progress=None, is_cancelled=None, *, local_models=None, name=None):
    """Compatibility for explicit full imports; feature UI always passes a name."""
    progress = progress or (lambda *_: None); cancel = is_cancelled or (lambda: False)
    with deployment_lock(root, cancel, progress):
        _install(Path(root), progress, cancel, local_models=local_models, name=name)


def _install(root, progress, cancel, *, local_models=None, name=None):
    from . import runtime_packages as rp
    names = [name] if name is not None else list(MODELS)
    if any(n not in MODELS for n in names): raise ValueError('未知千问功能')
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    shared = _shared_keys()
    def check():
        if cancel(): raise rp.CancelledError()
    for name in names:
        check()
        _recover(root, name)
        if is_installed(root, name): continue
        from .weights_reuse import recover_qwen_weights
        if recover_qwen_weights(root, name, check, progress): continue
        lock = MODELS[name]; sources = {}; missing = []
        for entry in lock['files']:
            check(); source = None
            if local_models is not None:
                source = Path(local_models)/name/entry['path']
            else:
                if _key(entry) in shared: source = _shared_source(root, entry, check)
                if source is None:
                    for candidate in (root/name/entry['path'], _snapshot(root, name)/entry['path']):
                        if _valid(candidate, entry, check): source = candidate; break
            if source is None: missing.append(entry['path'])
            else: sources[entry['path']] = source
        if missing:
            needed = sum(e['size'] for e in lock['files'] if e['path'] in missing)
        else: needed = 0
        # Budget for download plus verified staging; preserve installed model on failure.
        staging = sum(e['size'] for e in lock['files'] if _key(e) not in shared or not (root.parent/'shared'/e['sha256']).is_file())
        reserve = needed + staging + 256*1024*1024
        if shutil.disk_usage(root).free < reserve:
            raise RuntimeError(f'空间不足，本次安装需要约 {reserve/1024**3:.1f} GB 可用空间。原安装已保留。')
        if missing:
            progress(f'下载{FEATURES.get(name, name)} · 本次新增约 {needed/10**9:.2f} GB', -1)
            cache = root.parent/'downloads'/name; cache.mkdir(parents=True, exist_ok=True)
            rp._snapshot_download_cancellable(lock['repo'], lock['revision'], cache, cancel, allow_patterns=missing)
            for path in missing: sources[path] = _snapshot(root, name)/path
        transaction = Path(tempfile.mkdtemp(prefix='.qwen-install-', dir=root))
        candidate = transaction/'candidate'; candidate.mkdir(); destination = root/name; backup = transaction/'previous'
        promoted = False
        try:
            for entry in lock['files']:
                check(); source = sources[entry['path']]; dest = candidate/entry['path']; dest.parent.mkdir(parents=True, exist_ok=True)
                if _key(entry) in shared:
                    source = _preserve_shared(root, entry, source, check)
                    os.link(source, dest)
                else:
                    _copy_verified(source, dest, entry, check)
            (candidate/'.lulu-qwen-model.json').write_text(json.dumps({'model': name, 'revision': lock['revision']}))
            check()
            receipt = _receipt_path(mm.app_data_dir()/'.artifact-trust', 'qwen3-weights:'+name)
            if receipt.exists(): shutil.copyfile(receipt, transaction/'previous-receipt.json')
            (transaction/'pending.json').write_text(json.dumps({'name': name}))
            if os.path.lexists(destination): destination.replace(backup)
            candidate.replace(destination); promoted = True
            write_install_receipt('qwen3-weights:'+name, destination, _trust(name), mm.app_data_dir()/'.artifact-trust')
            if not is_installed(root, name): raise RuntimeError(name+' 安装收据校验失败')
        except BaseException:
            if promoted: destination.replace(transaction/'failed')
            if backup.exists(): backup.replace(destination)
            if (transaction/'pending.json').exists():
                _restore_receipt(transaction, name)
                (transaction/'pending.json').unlink()
            shutil.rmtree(transaction)
            raise
        else:
            (transaction/'pending.json').unlink()
            shutil.rmtree(transaction)
            if local_models is None: shutil.rmtree(root.parent/'downloads'/name, ignore_errors=True)
        progress(FEATURES.get(name, name)+'已就绪', .99)


def uninstall_feature(name, *, root=None, retain_common=False):
    from .qwen3_deployment import remove
    return remove(name, base=root, retain_common=retain_common)
