"""Verified SQLite snapshots and explicit recovery; never reset a damaged store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from .. import library
from .store import encode, identity, now, workspace_root

INTERVAL = 300
_attempted = {}
_warnings = {}


def database_path():
    return workspace_root() / 'conversations.sqlite3'


def folder():
    root = workspace_root() / 'backups'
    if root.is_symlink():
        raise ValueError('会话备份目录不安全，原文件保留')
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def digest(path):
    if path.is_symlink():
        raise ValueError('会话文件路径不安全')
    if not path.exists():
        return 'missing'
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def fingerprint():
    path = database_path()
    return hashlib.sha256(encode({suffix: digest(Path(str(path)+suffix))
        for suffix in ('','-journal','-wal','-shm')}).encode()).hexdigest()


def _inspect(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('数据库文件不存在或路径不安全')
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('数据库版本不匹配，不能用当前版本恢复')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库完整性检查未通过')
        counts = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ('conversations','messages','proposals','preferences')}
        for table,column in [('conversations','draft'),('messages','payload'),('proposals','payload'),('preferences','value')]:
            for row in db.execute(f'SELECT {column} FROM {table}'):
                json.loads(row[0])
        for row in db.execute('SELECT checkpoint FROM conversations WHERE checkpoint IS NOT NULL'):
            json.loads(row[0])
        return counts
    finally:
        db.close()


def _entry(backup_id):
    identity(backup_id)
    if backup_id not in {'auto-0','auto-1','auto-2'} and not backup_id.startswith('manual-'):
        raise ValueError('备份编号无效')
    path=folder()/(backup_id+'.sqlite3')
    metadata=folder()/(backup_id+'.json')
    if metadata.is_symlink():
        raise ValueError('备份信息路径不安全')
    value=json.loads(metadata.read_text())
    if value.get('id') != backup_id or value.get('sha256') != digest(path):
        raise ValueError('备份文件已变化，未用它覆盖当前资料')
    counts=_inspect(path)
    return dict(id=backup_id,created_at=value['created_at'],sha256=value['sha256'],
                bytes=path.stat().st_size,counts=counts,automatic=backup_id.startswith('auto-'),available=True)


def _backup_entries_for_deletion():
    """Verify every app-restorable snapshot before removing a conversation."""
    root = folder()
    names = {'auto-0', 'auto-1', 'auto-2'}
    names.update(path.stem for path in root.glob('manual-*.json'))
    names.update(path.stem for path in root.glob('manual-*.sqlite3'))
    entries = []
    for name in sorted(names):
        metadata = root / (name + '.json')
        database = root / (name + '.sqlite3')
        if metadata.is_symlink() or database.is_symlink():
            raise ValueError('有一份会话备份路径不安全，无法确认删除范围；原会话保留')
        if not metadata.exists() and not database.exists():
            continue
        if not metadata.exists() or not database.exists():
            raise ValueError('有一份会话备份缺少配套文件，无法确认删除范围；原会话保留')
        try:
            entries.append(_entry(name))
        except (ValueError, OSError, RuntimeError, sqlite3.Error, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('有一份会话备份无法通过检查，无法确认删除范围；原会话保留') from exc
    return entries


def _remove_conversation_rows(db, conversation_id):
    row = db.execute('SELECT document_id FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    if row is None:
        return False
    document_id = row[0]
    active_key = 'active:' + document_id
    active = db.execute('SELECT value FROM preferences WHERE key=?', (active_key,)).fetchone()
    db.execute('DELETE FROM proposals WHERE conversation_id=?', (conversation_id,))
    db.execute('DELETE FROM messages WHERE conversation_id=?', (conversation_id,))
    db.execute('DELETE FROM conversations WHERE id=?', (conversation_id,))
    db.execute('DELETE FROM preferences WHERE key=?', ('archived:' + conversation_id,))
    if active and json.loads(active[0]) == conversation_id:
        next_chat = db.execute(
            'SELECT id FROM conversations WHERE document_id=? ORDER BY updated_at DESC,id LIMIT 1',
            (document_id,),
        ).fetchone()
        if next_chat:
            db.execute('UPDATE preferences SET value=? WHERE key=?', (encode(next_chat[0]), active_key))
        else:
            db.execute('DELETE FROM preferences WHERE key=?', (active_key,))
    return True


def _forget_from_backup(entry, conversation_id):
    root = folder()
    source_path = root / (entry['id'] + '.sqlite3')
    metadata_path = root / (entry['id'] + '.json')
    staged = root / ('pending-delete-' + uuid4().hex + '.sqlite3')
    changed = False
    try:
        if source_path.is_symlink() or metadata_path.is_symlink():
            raise ValueError('会话备份路径不安全，未完成删除')
        source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
        try:
            destination = sqlite3.connect(staged)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        staged.chmod(0o600)
        db = sqlite3.connect(staged)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            changed = _remove_conversation_rows(db, conversation_id)
            db.commit()
            if changed:
                db.execute('VACUUM')
        finally:
            db.close()
        if not changed:
            return False
        counts = _inspect(staged)
        value = dict(id=entry['id'], created_at=entry['created_at'],
                     sha256=digest(staged), counts=counts)
        with staged.open('rb') as stream:
            os.fsync(stream.fileno())
        if digest(source_path) != entry['sha256'] or metadata_path.is_symlink():
            raise library.RevisionConflict('会话备份在删除期间发生变化，原备份保留')
        # Replace the verified snapshot first. If interrupted before the matching
        # receipt is written, the app will mark this copy unavailable rather than
        # restore a conversation the user asked to remove.
        os.replace(staged, source_path)
        library._write_atomic_text(metadata_path, encode(value))
        return True
    finally:
        if staged.exists():
            staged.unlink()


def forget_conversation(conversation_id):
    """Remove a deleted chat from every verified snapshot the app can restore."""
    identity(conversation_id)
    with library._transaction():
        entries = _backup_entries_for_deletion()
        return sum(_forget_from_backup(entry, conversation_id) for entry in entries)


def status():
    with library._transaction():
        finish_pending_restore()
        current=database_path()
        try:
            state='ready' if current.exists() and _inspect(current) is not None else 'missing'
        except (sqlite3.Error,ValueError,OSError):
            state='unavailable'
        try:
            stamp=fingerprint()
        except (ValueError,OSError):
            stamp=None
        entries=[]
        for metadata in sorted(folder().glob('*.json')):
            if not (metadata.stem.startswith('manual-') or metadata.stem in {'auto-0','auto-1','auto-2'}):
                continue
            try:
                entries.append(_entry(metadata.stem))
            except (ValueError,OSError,sqlite3.Error,KeyError):
                entries.append(dict(id=metadata.stem,available=False,message='备份检查未通过，原文件保留'))
        entries.sort(key=lambda row:row.get('created_at',''),reverse=True)
        return dict(state=state,fingerprint=stamp,backups=entries,
                    warning=_warnings.get(str(current),''),interval_minutes=5)


def backup(*, automatic=False, connection=None):
    with library._transaction():
        source=database_path()
        if source.is_symlink() or not source.is_file():
            raise ValueError('当前会话数据库不可备份，原资料保留')
        root=folder()
        if automatic:
            choices=[]
            for slot in ('auto-0','auto-1','auto-2'):
                metadata=root/(slot+'.json')
                path=root/(slot+'.sqlite3')
                if metadata.is_symlink() or path.is_symlink():
                    raise ValueError('自动备份路径不安全，未替换原文件')
                if not metadata.exists() and not path.exists():
                    choices.append(('',slot))
                else:
                    # Unknown or damaged files are evidence, never rotation targets.
                    entry=_entry(slot)
                    choices.append((entry['created_at'],slot))
            backup_id=min(choices)[1]
        else:
            backup_id='manual-'+uuid4().hex
        temp=root/('pending-'+uuid4().hex+'.sqlite3')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(fd)
        own=connection is None
        db=connection or sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        try:
            destination=sqlite3.connect(temp)
            try:
                db.backup(destination)
            finally:
                destination.close()
            counts=_inspect(temp)
            value=dict(id=backup_id,created_at=now(),sha256=digest(temp),counts=counts)
            with temp.open('rb') as stream: os.fsync(stream.fileno())
            os.replace(temp,root/(backup_id+'.sqlite3'))
            library._write_atomic_text(root/(backup_id+'.json'),encode(value))
            _warnings.pop(str(source),None)
            return _entry(backup_id)
        finally:
            if own: db.close()
            # Only this call's unused private temporary, never an existing backup.
            if temp.exists(): temp.unlink()


def maybe_backup(connection):
    path=database_path()
    stamp=time.monotonic()
    if stamp-_attempted.get(str(path),float('-inf'))<INTERVAL:
        return
    _attempted[str(path)]=stamp
    try:
        root=folder()
        latest=max((p.stat().st_mtime for p in root.glob('auto-*.json')),default=0)
        if time.time()-latest>=INTERVAL:
            backup(automatic=True,connection=connection)
    except (OSError,ValueError,sqlite3.Error,KeyError):
        # The original transaction is already committed. Report protection loss
        # separately instead of falsely reporting that the user's save failed.
        _warnings[str(path)]='会话已保存，但本次自动备份未完成；请在资料保护中检查存储空间或手动备份。'


def restore(backup_id, expected_fingerprint, expected_backup_hash):
    with library._transaction():
        if not expected_fingerprint or fingerprint()!=expected_fingerprint:
            raise library.RevisionConflict('会话记录在预览后已有变化，请重新查看备份再恢复')
        entry=_entry(backup_id)
        if entry['sha256']!=expected_backup_hash:
            raise library.RevisionConflict('选中的备份已有变化，请重新查看')
        source=database_path()
        try:
            healthy=_inspect(source) is not None
        except (sqlite3.Error,ValueError):
            healthy=False
        # A healthy pre-restore state is also listed in the recovery UI, making
        # undoing the choice possible without finding files in a hidden folder.
        rollback=backup() if healthy else None
        archive=workspace_root()/('recovery-'+uuid4().hex)
        archive.mkdir(mode=0o700)
        # Copy every sidecar before replacement. Do not reset, truncate or delete
        # the damaged evidence. All cooperating Store connections share this lock.
        for suffix in ('','-journal','-wal','-shm'):
            path=Path(str(source)+suffix)
            if path.is_symlink(): raise ValueError('会话文件路径不安全')
            if path.exists():
                destination=archive/path.name
                shutil.copyfile(path,destination)
                destination.chmod(0o600)
                with destination.open('rb') as stream: os.fsync(stream.fileno())
        staged=archive/'restored.sqlite3'
        shutil.copyfile(folder()/(backup_id+'.sqlite3'),staged)
        staged.chmod(0o600)
        if digest(staged)!=expected_backup_hash:
            raise ValueError('恢复副本检查失败，当前资料未覆盖')
        _inspect(staged)
        with staged.open('rb') as stream: os.fsync(stream.fileno())
        if fingerprint()!=expected_fingerprint:
            raise library.RevisionConflict('恢复前会话已有变化，当前资料未覆盖')
        receipt=dict(backup_id=backup_id,backup_sha256=expected_backup_hash,
            previous_fingerprint=expected_fingerprint,created_at=now(),archive=archive.name,
            files={suffix:digest(Path(str(source)+suffix)) for suffix in ('','-journal','-wal','-shm')})
        library._write_atomic_text(archive/'receipt.json',encode(receipt))
        pending=workspace_root()/'restore-pending.json'
        if pending.exists() or pending.is_symlink():
            raise RuntimeError('上一次恢复尚未结束，请重新打开资料保护')
        library._write_atomic_text(pending,encode(receipt))
        finish_pending_restore()
        _warnings.pop(str(source),None)
        _attempted.pop(str(source),None)
        return dict(state='restored',backup_id=backup_id,counts=entry['counts'],
                    rollback_backup_id=rollback['id'] if rollback else None,
                    preserved_as=archive.name,message='会话已恢复，恢复前的文件已单独保留。文稿正文保持当前版本。')


def finish_pending_restore():
    """Complete only a previously explicit restore, including crash between renames."""
    pending=workspace_root()/'restore-pending.json'
    if pending.is_symlink():
        raise ValueError('恢复记录路径不安全')
    if not pending.exists():
        return
    with library._transaction():
        value=json.loads(pending.read_text())
        name=identity(value['archive'])
        if not name.startswith('recovery-'):
            raise ValueError('恢复记录无效')
        archive=workspace_root()/name
        if archive.is_symlink() or (archive/'receipt.json').is_symlink():
            raise ValueError('恢复存档路径不安全')
        if json.loads((archive/'receipt.json').read_text())!=value:
            raise ValueError('恢复记录已变化，未覆盖资料')
        source=database_path()
        current=digest(source)
        if current not in {value['files'][''],value['backup_sha256']}:
            raise library.RevisionConflict('恢复中断后数据库已有外部修改，所有副本均已保留')
        # Validate everything before touching sidecars. A resumed restore must
        # never apply an old WAL/journal to the restored SQLite snapshot.
        for suffix in ('-journal','-wal','-shm'):
            path=Path(str(source)+suffix)
            if digest(path) not in {'missing',value['files'][suffix]}:
                raise library.RevisionConflict('恢复中断后旁路记录已有变化，未继续覆盖')
        staged=archive/'restored.sqlite3'
        if current!=value['backup_sha256'] and digest(staged)!=value['backup_sha256']:
            raise ValueError('恢复副本不完整，原资料保留')
        for suffix in ('-journal','-wal','-shm'):
            path=Path(str(source)+suffix)
            if path.exists():
                destination=archive/(path.name+'.original')
                if destination.exists():
                    raise library.RevisionConflict('恢复旁路记录出现冲突，未覆盖存档')
                os.replace(path,destination)
        if current!=value['backup_sha256']:
            os.replace(staged,source)
        _inspect(source)
        library._write_atomic_text(workspace_root()/'database-created.json',encode(dict(version=1,created_at=now())))
        fd=os.open(workspace_root(),os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        pending.unlink()
