"""One explicit login mutation for browser state and live credentials.

Snapshots contain presence only. Secrets are read only for an authorized change
that may need rollback, never while browsing settings. Profile leases are the
same ones used by collection and browser login.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .profiles import ProfileRegistry, ProfileError
from .browser_shell import debug_port_for, _port_is_available

MARK = '.logged_in'


class LoginBusyError(RuntimeError):
    """Only fixed, user-facing messages for a still-owned browser profile."""


def login_state(root, index):
    try:
        registry = ProfileRegistry(root)
        resolution = registry.resolve('douyin')
        profile = resolution.state != 'missing'
        path = resolution.path / MARK
        registry._assert_no_symlink(registry.app_data, path)
        browser = path.is_file()
        error = False
    except Exception:
        browser, profile, error = False, False, True
    return dict(browser=browser, profile=profile, cookie=bool(index.setting('credential')),
                source=index.setting('login_source', ''),
                partial=bool(index.setting('login_clear_pending')),
                error=error or bool(index.setting('login_update_pending')))


class LoginStore:
    def __init__(self, root, index, read, write):
        self.registry = ProfileRegistry(root)
        self.index, self.read, self.write = index, read, write

    @contextmanager
    def _owned(self):
        resolution = self.registry.resolve('douyin', create=True)
        try:
            lease = self.registry.acquire('douyin')
        except ProfileError as exc:
            if exc.code == 'profile_busy':
                raise LoginBusyError('抖音专用浏览器正在使用，请先结束主页采集或关闭弹出的抖音浏览器后重试。') from None
            raise
        with lease:
            port = debug_port_for('douyin')
            if not all(_port_is_available(port + offset) for offset in range(4)):
                raise LoginBusyError('抖音专用浏览器尚未关闭，请关闭弹出的抖音浏览器窗口后重试。')
            yield resolution.path

    def _settings(self, **values):
        with self.index.lock, self.index.db:
            for key, value in values.items():
                self.index.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) '
                                      'DO UPDATE SET value=excluded.value', (key, value))

    def _restore_secret(self, value):
        try:
            self.write(value)
        except Exception:
            self._settings(login_update_pending='1')
        else:
            self._settings(login_update_pending='')

    def _pending_path(self):
        name = self.index.setting('login_clear_pending')
        if not name:
            return None
        if not re.fullmatch(r'douyin-default-[a-f0-9]{32}', name):
            raise RuntimeError('登录清除记录异常，请重试；原资料保留。')
        path = self.registry.managed_root / '.trash' / name
        self.registry._assert_no_symlink(self.registry.app_data, path)
        return path

    def save(self, value, source='manual'):
        if source not in {'manual', 'browser'} or not value or value.lower().startswith('cookie:') or '\n' in value or '\r' in value:
            raise RuntimeError('只粘贴 Cookie 内容，去掉开头的 Cookie: 和换行。')
        if self.index.setting('login_clear_pending'):
            raise RuntimeError('上次登录尚未完全清除，请先重试清除。')
        with self._owned() as profile:
            marker = profile / MARK
            self.registry._assert_no_symlink(self.registry.app_data, marker)
            previous = marker.read_bytes() if marker.exists() else None
            old = self.read() if self.index.setting('credential') else ''
            attempted = False
            try:
                # Browser presence and credential source become visible only
                # after the verified credential write and settings transaction.
                if source == 'browser':
                    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(b'ok'); stream.flush(); os.fsync(stream.fileno())
                attempted = True
                self.write(value)
                self._settings(credential='configured', login_source=source, login_update_pending='')
            except Exception:
                try:
                    if source == 'browser':
                        if previous is None:
                            marker.unlink(missing_ok=True)
                        else:
                            marker.write_bytes(previous)
                finally:
                    if attempted:
                        self._restore_secret(old)
                    if source == 'browser':
                        # The user may already have changed the persistent browser
                        # session. Do not reuse an older live credential as if both
                        # accounts were known to match after a failed import.
                        self._settings(login_update_pending='1')
                raise

    def clear(self):
        with self._owned() as profile:
            pending = self._pending_path()
            if pending:
                if pending.exists():
                    shutil.rmtree(pending)
                self._settings(login_clear_pending='')
            trash = self.registry.managed_root / '.trash'
            self.registry._assert_no_symlink(self.registry.app_data, trash)
            trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = trash / ('douyin-default-' + uuid4().hex)
            old = self.read() if self.index.setting('credential') else ''
            self._settings(login_clear_pending=target.name)
            os.replace(profile, target)
            attempted = False
            try:
                attempted = True
                self.write('')
                self._settings(credential='', login_source='', login_update_pending='')
            except Exception:
                os.replace(target, profile)
                self._settings(login_clear_pending='')
                if attempted:
                    self._restore_secret(old)
                raise
            # A failed strict removal remains an explicit partial state. Retry
            # can delete only this recorded, registry-owned tombstone.
            shutil.rmtree(target)
            self._settings(login_clear_pending='')
