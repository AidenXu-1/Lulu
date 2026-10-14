"""One SenseVoice computation across GUI/Core processes, released by the OS."""
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat


@contextmanager
def sensevoice_resource(root: Path | None = None):
    if root is None:
        from ..models_manager import app_data_dir
        root = app_data_dir()
    directory = root / 'transcription-locks'
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise RuntimeError('转录资源路径不安全')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('转录资源目录权限无效')
    fd = os.open(directory / 'sensevoice.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeError('转录资源文件无效')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                raise RuntimeError('SenseVoice 正在处理另一项任务，请稍后再试') from exc
            raise
        yield
    finally:
        os.close(fd)
