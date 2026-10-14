"""Bounded trash disposal under the library transaction; never follows media paths."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
import re
import stat

from . import library


def _regular(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError('清理路径或文件身份异常，已保留资料')
    return value


def _read(path):
    before = _regular(path)
    if before.st_size > 16 * 1024 * 1024:
        raise ValueError('删除记录过大，已保留资料')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('清理文件已变化，已保留资料')
        return json.load(stream)


def _safe_dir(path):
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError('清理目录不安全，已保留资料')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
        os.close(fd)
        raise ValueError('清理目录已变化，已保留资料')
    return fd


@library._serialized
def policy(enabled=None):
    path = library._dir() / '.trash-policy.json'
    value = _read(path) if path.exists() or path.is_symlink() else {'enabled': False, 'days': 7}
    if type(value) is not dict or type(value.get('enabled')) is not bool or value.get('days') != 7:
        raise ValueError('自动清理设置异常，未执行清理')
    if enabled is not None:
        if type(enabled) is not bool:
            raise ValueError('自动清理开关无效')
        value = {'enabled': enabled, 'days': 7}
        library._write_atomic_text(path, json.dumps(value))
    return value


@library._serialized
def purge(token, confirmed=False):
    if confirmed is not True:
        raise ValueError('请先确认彻底删除；此操作无法恢复')
    if not isinstance(token, str) or re.fullmatch('[0-9a-f]{24}', token) is None:
        raise ValueError('删除凭据无效')
    root = library._dir()
    with ExitStack() as stack:
        fds = {}
        def directory(key, path):
            if key not in fds:
                fd = _safe_dir(path); stack.callback(os.close, fd); fds[key] = fd
            return fds[key]
        directory('root', root)
        directory('trash', root / '.trash')
        receipt = root / '.trash' / (token + '.json')
        data = _read(receipt)
        data = library._current_trash_identity(root, data)
        eid = data['id']; destination = library._entry_path(eid)
        if data.get('token') != token or data.get('json') != f'.delete-{eid}-{token}.json':
            raise ValueError('删除凭据不匹配，已保留资料')
        if destination.exists() or destination.is_symlink():
            raise ValueError('原位置已有资料，请先处理恢复冲突')
        targets = [('root', data['json'], data['json_identity'])]
        if data.get('markdown'):
            if data['markdown'] != f'.delete-{eid}-{token}.md':
                raise ValueError('正文凭据不匹配')
            directory('docs', root / library.DOCS_DIR_NAME)
            if (root / library.DOCS_DIR_NAME / (eid + '.md')).exists():
                raise ValueError('原位置已有正文，未清理')
            targets.append(('docs', data['markdown'], data['markdown_identity']))
        versions = root / '.versions' / eid
        if versions.exists() or versions.is_symlink():
            directory('versions-root', versions.parent)
            directory('versions', versions)
        if data.get('purging'):
            history = data.get('purge_history', [])
        else:
            history = []
            if 'versions' in fds:
                for name in os.listdir(fds['versions']):
                    item = os.stat(name, dir_fd=fds['versions'], follow_symlinks=False)
                    history.append([name, [item.st_dev, item.st_ino]])
        for name, identity in history:
            if re.fullmatch(r'[0-9]+-[a-f0-9]{64}\.md', name) is None:
                raise ValueError('修改历史包含未知文件，已保留资料')
            if 'versions' not in fds:
                raise ValueError('修改历史目录缺失，已保留资料')
            targets.append(('versions', name, identity))
        # Validate every file before irreversible work, retaining the receipt for retry.
        pending = []
        for key, name, identity in targets:
            try:
                item = os.stat(name, dir_fd=fds[key], follow_symlinks=False)
            except FileNotFoundError:
                if data.get('purging'): continue
                raise ValueError('待删除文件缺失，已保留现场')
            if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
                    or [item.st_dev, item.st_ino] != identity):
                raise ValueError('待删除文件身份已变化，已保留现场')
            pending.append((key, name, identity))
        data.update(purging=True, purge_history=history)
        library._write_atomic_text(receipt, json.dumps(data, ensure_ascii=False))
        for key, name, identity in pending:
            item = os.stat(name, dir_fd=fds[key], follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or [item.st_dev, item.st_ino] != identity:
                raise ValueError('清理中文件已变化，请重试剩余项目')
            os.unlink(name, dir_fd=fds[key])
            os.fsync(fds[key])
        os.unlink(token + '.json', dir_fd=fds['trash']); os.fsync(fds['trash'])
    return {'token': token, 'deleted': True}


@library._serialized
def cleanup(now=None):
    settings = policy()
    result = {'deleted': [], 'failures': [], **settings}
    if not settings['enabled']: return result
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None: current = current.astimezone()
    for row in library.list_deleted():
        try:
            deleted = datetime.fromisoformat(row['deleted_at'])
            if deleted.tzinfo is None: deleted = deleted.astimezone()
            if (current - deleted).total_seconds() < 7 * 86400: continue
            purge(row['token'], confirmed=True)
            result['deleted'].append(row['token'])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result['failures'].append({'token': row['token'], 'message': str(exc)})
    return result
