"""Bounded trash disposal under the library transaction; never follows media paths."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
import re
import stat
from pathlib import Path

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
    # An archived document never expires, including installations with the old
    # seven-day preference. Keep the compatibility API, but revoke that behavior.
    if enabled is not None and type(enabled) is not bool:raise ValueError('归档设置无效')
    if enabled is True:
        raise ValueError('归档长期保留，不支持自动删除；可手动移入系统废纸篓')
    path=library._dir()/'.trash-policy.json'
    if path.exists() or path.is_symlink():
        try:old=_read(path)
        except (OSError,ValueError):old=None
        if type(old) is dict and old=={'enabled':True,'days':7}:
            library._write_atomic_text(path,json.dumps({'enabled':False,'days':7}))
    return {'enabled': False, 'days': None}


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
    return {'deleted': [], 'failures': [], **policy()}


def move_to_system_trash(path):
    from PySide6.QtCore import QFile
    result = QFile.moveToTrash(str(path))
    moved, target = result if isinstance(result, tuple) else (result, '')
    if moved is not True:
        raise OSError('未能移入系统废纸篓，资料已保留；请检查磁盘连接和权限')
    return target


def _system_sources(root, token, data):
    eid = data['id'];library._entry_path(eid)
    if data.get('token')!=token or data.get('json')!=f'.delete-{eid}-{token}.json':
        raise ValueError('归档凭据不匹配，已保留资料')
    sources=[(root/data['json'],'record.json',data['json_identity'])]
    if data.get('markdown'):
        if data['markdown']!=f'.delete-{eid}-{token}.md':raise ValueError('正文凭据不匹配')
        sources.append((root/library.DOCS_DIR_NAME/data['markdown'],'正文.md',data['markdown_identity']))
    for name,identity in data.get('system_history',[]):
        if type(name) is not str or re.fullmatch(r'[0-9]+-[a-f0-9]{64}\.md',name) is None:
            raise ValueError('修改历史凭据异常，已保留资料')
        sources.append((root/'.versions'/eid/name,'修改历史/'+name,identity))
    return sources


def _system_bundle(root,token,data):
    title=re.sub(r'[\/\\\x00-\x1f:]', '_', str(data.get('title') or '资料'))[:60].strip('.') or '资料'
    return root/'.trash'/('Lulu-'+title+'-'+token+'.luluarchive')


def recover_system_stage(root, token, data):
    """Undo an interrupted staging move; never removes a material or overwrites."""
    if not data.get('system_stage'):return data
    if type(token) is not str or re.fullmatch('[0-9a-f]{24}',token) is None:raise ValueError('归档凭据无效')
    bundle=_system_bundle(root,token,data)
    if bundle.is_symlink():raise ValueError('删除暂存路径异常，已保留资料')
    sources=_system_sources(root,token,data)
    with ExitStack() as stack:
        seen=set()
        for source,relative,identity in sources:
            for directory in (source.parent, bundle, (bundle/relative).parent):
                if directory in seen:continue
                seen.add(directory)
                if directory.exists() or directory.is_symlink():
                    fd=_safe_dir(directory);stack.callback(os.close,fd)
            staged=bundle/relative
            if staged.exists() or staged.is_symlink():
                value=staged.lstat()
                if not stat.S_ISREG(value.st_mode) or value.st_nlink not in {1,2} or [value.st_dev,value.st_ino]!=identity:
                    raise ValueError('暂存资料身份异常，已保留全部文件')
                try:os.link(staged,source,follow_symlinks=False)
                except FileExistsError:
                    original=source.lstat()
                    if not stat.S_ISREG(original.st_mode) or [original.st_dev,original.st_ino]!=identity:
                        raise ValueError('原位置有新资料，未覆盖；删除暂存仍保留')
                staged.unlink()
            value=_regular(source)
            if [value.st_dev,value.st_ino]!=identity:raise ValueError('原位置资料已变化，未覆盖')
    # Empty directories and the duplicated note contain no user material.
    note=bundle/'恢复说明.txt'
    if note.exists() and not note.is_symlink():note.unlink()
    history=bundle/'修改历史'
    if history.exists():history.rmdir()
    if bundle.exists():bundle.rmdir()
    data=dict(data);data.pop('system_stage',None);data.pop('system_history',None)
    library._write_atomic_text(root/'.trash'/(token+'.json'),json.dumps(data,ensure_ascii=False))
    return data


@library._serialized
def system_trash(token, confirmed=False):
    """Move the owned files into one bundle, then let the OS move that bundle.

    No material is permanently unlinked. Failed handoff restores all files; a
    journal permits the same rollback after a process interruption.
    """
    if confirmed is not True:raise ValueError('请先确认移入系统废纸篓')
    if type(token) is not str or re.fullmatch('[0-9a-f]{24}',token) is None:raise ValueError('归档凭据无效')
    root=library._dir()
    with ExitStack() as stack:
        for directory in (root,root/'.trash'):
            fd=_safe_dir(directory);stack.callback(os.close,fd)
        receipt=root/'.trash'/(token+'.json')
        data=_read(receipt)
        if data.get('system_stage'):data=recover_system_stage(root,token,data)
        data=library._current_trash_identity(root,data)
        if data.get('purging'):raise ValueError('旧清理曾中断，剩余资料已保留，请先核对')
        eid=data['id'];destination=library._entry_path(eid)
        if destination.exists() or destination.is_symlink():raise ValueError('原位置已有资料，未删除')
        versions=root/'.versions'/eid;history=[]
        if versions.exists() or versions.is_symlink():
            for directory in (versions.parent,versions):
                fd=_safe_dir(directory);stack.callback(os.close,fd)
            for name in os.listdir(versions):
                if re.fullmatch(r'[0-9]+-[a-f0-9]{64}\.md',name) is None:raise ValueError('修改历史包含未知文件，已保留资料')
                value=_regular(versions/name);history.append([name,[value.st_dev,value.st_ino]])
        data.update(system_stage=True,system_history=history)
        sources=_system_sources(root,token,data)
        for source,relative,identity in sources:
            fd=_safe_dir(source.parent);stack.callback(os.close,fd)
            value=_regular(source)
            if [value.st_dev,value.st_ino]!=identity:raise ValueError('资料文件身份已变化，已保留资料')
        bundle=_system_bundle(root,token,data)
        bundle.mkdir(mode=0o700)  # Exclusive creation; never reuse an unknown directory.
        library._write_atomic_text(receipt,json.dumps(data,ensure_ascii=False))
        try:
            for source,relative,identity in sources:
                target=bundle/relative;target.parent.mkdir(mode=0o700,exist_ok=True)
                value=_regular(source)
                if [value.st_dev,value.st_ino]!=identity:raise ValueError('移动时资料身份已变化，已保留资料')
                os.rename(source,target)
                moved=_regular(target)
                if [moved.st_dev,moved.st_ino]!=identity:raise ValueError('移动后资料身份异常，已保留现场')
            (bundle/'恢复说明.txt').write_text('Lulu 资料：正文.md 可打开或重新导入；record.json 为原记录；修改历史保留旧版本。原始音视频和独立备份未删除。',encoding='utf-8')
            move_to_system_trash(bundle)
        except Exception:
            recover_system_stage(root,token,data)
            raise
        try:receipt.unlink()  # Only the journal; all material files are now in OS Trash.
        except OSError:pass  # A stale journal must not turn a completed OS move into a claimed failure.
        return {'token':token,'deleted':True,'system_trash':True}
