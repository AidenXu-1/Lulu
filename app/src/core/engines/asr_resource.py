"""One local ASR owner across processes, including retained batch weights."""
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
import threading
from functools import wraps

_mutex = threading.Lock()
_context = threading.local()
_fd = None
_owner = None
_model = None


class ASRResourceBusy(RuntimeError):
    reason_code = 'ASR_RESOURCE_BUSY'
    code = 'RESOURCE_BUSY'


def _busy():
    return ASRResourceBusy('本地模型正在使用，请等当前批次完成或取消后再试。')


def _open_lock(root: Path | None):
    if root is None:
        from ..models_manager import app_data_dir
        root = app_data_dir()
    directory = root / 'transcription-locks'
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise RuntimeError('转录资源路径不安全')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('转录资源目录权限无效')
    fd = os.open(directory / 'asr.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeError('转录资源文件无效')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES): raise _busy() from exc
            raise
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_cache():
    # Never import a heavy runtime merely to release it.
    mx = sys.modules.get('mlx.core')
    if mx is not None:
        mx.synchronize()
    module = sys.modules.get('mlx_whisper.transcribe')
    if module is not None:
        module.ModelHolder.model = None
        module.ModelHolder.model_path = None
    if mx is not None:
        mx.clear_cache()


def _release():
    global _fd, _owner, _model
    _release_cache()  # If release fails, retain ownership and block maintenance.
    if _fd is not None:
        os.close(_fd)
    _fd = _owner = _model = None


@contextmanager
def batch_context(owner):
    previous = getattr(_context, 'owner', None)
    _context.owner = owner
    try: yield
    finally: _context.owner = previous


@contextmanager
def model_use(model_id, *, retain=False, root=None):
    global _fd, _owner, _model
    if not _mutex.acquire(blocking=False): raise _busy()
    owner = getattr(_context, 'owner', None)
    acquired = False
    success = False
    try:
        if _fd is not None and (owner is None or owner != _owner): raise _busy()
        if _fd is None:
            _fd = _open_lock(root)
            _owner = owner
        acquired = True
        if _model != model_id:
            _release_cache()
        _model = model_id
        yield
        success = True
    finally:
        try:
            if acquired and not (success and retain and owner is not None): _release()
        finally: _mutex.release()


def finish_batch(owner):
    if not _mutex.acquire(blocking=False): return False
    try:
        if _fd is not None and owner is not None and owner == _owner: _release()
        return True
    finally: _mutex.release()


def resource_fd():
    if _fd is None: raise RuntimeError('转录资源尚未取得')
    return _fd


@contextmanager
def maintenance(root=None):
    if getattr(_context, 'maintenance', False):
        yield
        return
    with batch_context(None), model_use(None, root=root):
        _context.maintenance = True
        try: yield
        finally: _context.maintenance = False


def model_maintenance(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with maintenance():
            return function(*args, **kwargs)
    return wrapped
