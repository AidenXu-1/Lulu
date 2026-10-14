"""Private local integrity material, never provider credentials.

This detects normal corruption and coordinates durable writes. It does not
isolate other processes of the same user or provide an external rollback root.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock

from .license.store import SecureStoreError
from .job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode

SCHEME = 'local-integrity-v1'
TASK_DIRECTORY = 'task-storage-v2'
ID_KEY = 'lulu_local_installation_id_v1'
MIGRATION_KEY = 'lulu_local_legacy_migration_v1'
AUTO_MIGRATION_KEY = 'lulu_local_legacy_auto_attempt_v1'
HISTORY_KEY = 'lulu_local_legacy_history_v1'
MAX_BYTES = 20 * 1024 * 1024


def _account(name):
    if type(name) is not str or not (name in {
            ID_KEY, MIGRATION_KEY, AUTO_MIGRATION_KEY, HISTORY_KEY, 'lulu_core_job_registry_v1'}
            or re.fullmatch(r'lulu_core_job_(?:key|anchor)_v1:[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', name)
            or re.fullmatch(r'lulu_agent_entry_ownership_hmac_key_v1:[a-f0-9]{64}', name)):
        raise SecureStoreError('LOCAL_INTEGRITY_ACCOUNT_INVALID')
    return hashlib.sha256(name.encode()).hexdigest() + '.json'


def _file_metadata(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SecureStoreError('LOCAL_INTEGRITY_FILE_UNSAFE')


class LocalIntegrityStore:
    storage_scheme = SCHEME
    core_generation = None

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise SecureStoreError('LOCAL_INTEGRITY_PATH_UNSAFE')
        _validate_no_follow_ancestors(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _validate_owned_mode(self.root, mode=0o700, directory=True)
        self._thread_lock = RLock()

    def path_for(self, name):
        return self.root / _account(name)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            _validate_no_follow_ancestors(self.root)
            _validate_owned_mode(self.root, mode=0o700, directory=True)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            lock = None
            try:
                lock = os.open('storage.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                _file_metadata(os.fstat(lock))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise SecureStoreError('LOCAL_INTEGRITY_BUSY') from exc
                yield directory
            finally:
                if lock is not None: os.close(lock)
                os.close(directory)

    def _read(self, directory, name):
        filename = _account(name)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd); _file_metadata(info)
            if info.st_size > MAX_BYTES:
                raise SecureStoreError('LOCAL_INTEGRITY_VALUE_INVALID')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SecureStoreError('LOCAL_INTEGRITY_CHANGED')
            from .agent_entry.delivery_identity import strict_json
            value = strict_json(raw)
            if (type(value) is not dict or set(value) != {'schema', 'account', 'value'}
                    or value['schema'] != SCHEME or value['account'] != name
                    or type(value['value']) is not str):
                raise SecureStoreError('LOCAL_INTEGRITY_VALUE_INVALID')
            return value['value']
        finally:
            os.close(fd)

    def get_text(self, name):
        _account(name)
        with self._locked() as directory:
            return self._read(directory, name)

    def _write(self, directory, name, value, *, new=False):
        if type(value) is not str:
            raise SecureStoreError('LOCAL_INTEGRITY_VALUE_INVALID')
        filename = _account(name)
        current = self._read(directory, name)
        if new and current is not None:
            raise SecureStoreError('LOCAL_INTEGRITY_ALREADY_EXISTS')
        raw = (json.dumps({'schema': SCHEME, 'account': name, 'value': value},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
        if len(raw) > MAX_BYTES:
            raise SecureStoreError('LOCAL_INTEGRITY_VALUE_INVALID')
        temporary = '.' + filename + '.' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw); stream.flush(); os.fsync(fd)
            if new:
                os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(temporary, dir_fd=directory)
            else:
                if self._read(directory, name) != current:
                    raise SecureStoreError('LOCAL_INTEGRITY_CHANGED')
                os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(fd)
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass

    def write_new(self, name, value):
        _account(name)
        with self._locked() as directory:
            self._write(directory, name, value, new=True)

    create_core_task_material = write_new

    def set_text(self, name, value):
        _account(name)
        with self._locked() as directory:
            self._write(directory, name, value)

    def replace_core_task_material(self, name, expected, value):
        _account(name)
        with self._locked() as directory:
            if self._read(directory, name) != expected or expected is None:
                raise SecureStoreError('LOCAL_INTEGRITY_CHANGED')
            self._write(directory, name, value)

    def delete_text(self, name, *, missing_ok=False):
        _account(name)
        with self._locked() as directory:
            if self._read(directory, name) is None:
                if missing_ok: return
                raise SecureStoreError('LOCAL_INTEGRITY_MISSING')
            os.unlink(_account(name), dir_fd=directory); os.fsync(directory)


def product_local_store(product):
    product = Path(product)
    root = product / SCHEME
    # Missing local material cannot silently reinitialize an established store.
    established = (product / TASK_DIRECTORY, product / 'AgentEntry/local-app-generations')
    if not os.path.lexists(root) and any(os.path.lexists(path) for path in established):
        raise SecureStoreError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    return LocalIntegrityStore(root)
