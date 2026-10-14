"""Cross-process local voice compute exclusion; OS releases it if an owner exits.

The durable job ledger owns work/recovery state. This lock owns only volatile
model computation, so an orphaned PID never becomes a permanent lock record.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat

from ..resource_control.contracts import integrity_error, resource_busy
from ..models_manager import app_data_dir


@contextmanager
def voxcpm2_resource(root: Path | None = None):
    import fcntl
    directory = (root or app_data_dir()) / 'synthesis-locks'
    for parent in [directory, *directory.parents]:
        if parent.is_symlink():
            raise integrity_error('TTS_LOCK_PATH_UNSAFE')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise integrity_error('TTS_LOCK_PATH_UNSAFE')
    fd = os.open(directory / 'local_voxcpm2.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise integrity_error('TTS_LOCK_PATH_UNSAFE')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                raise resource_busy('TTS_ENGINE_BUSY') from exc
            raise
        yield
    finally:
        os.close(fd)


def voxcpm2_busy(root: Path | None = None) -> bool:
    """Read current compute ownership without creating files or loading a model."""
    import fcntl
    path = (root or app_data_dir()) / 'synthesis-locks' / 'local_voxcpm2.lock'
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise integrity_error('TTS_LOCK_PATH_UNSAFE')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise integrity_error('TTS_LOCK_PATH_UNSAFE')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                return True
            raise
    finally:
        os.close(fd)
