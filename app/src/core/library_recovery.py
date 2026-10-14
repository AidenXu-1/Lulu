"""Read-only discovery of retained bodies; recovery always creates a new record."""
import hashlib
import os
from pathlib import Path
import re
import stat

from . import library
from .document_metrics import visible_characters


def _body(path):
    root = library.LIBRARY_DIR
    if root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        raise ValueError('原文件路径异常，已保留')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            raise ValueError('原文件超过恢复预览范围，已保留完整文件')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('原文件超过恢复预览范围，已保留完整文件')
        return data.decode('utf-8')
    finally:
        os.close(fd)


def preview(document_id):
    if not isinstance(document_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', document_id):
        raise ValueError('资料编号无效')
    index = library.LIBRARY_DIR / (document_id + '.json')
    if index.is_symlink():
        raise ValueError('原登记路径异常，已保留')
    record = library._load_raw(document_id)
    if record is not None and record.get('kind', 'transcript') not in {'transcript', 'rewrite'}:
        raise FileNotFoundError('该资料不属于文稿')
    if record is None:
        text = _body(library.LIBRARY_DIR / 'docs' / (document_id + '.md'))
        result = dict(id=document_id, kind='transcript', title='找回的文稿 ' + document_id,
                      created_at='', folder_id=None, source='原文件保留', meta={}, revision=0)
        reason = '登记记录缺失或无法读取，正文已保留。可先阅读，再恢复为一份新文稿。'
    else:
        result = dict(record)
        path = library._markdown_abs(record.get('markdown_path'), document_id)
        try:
            text = _body(path)
        except (OSError, UnicodeError, ValueError):
            text = record.get('text', '')
        reason = '原文稿暂时无法正常打开，当前显示可读取的正文副本。恢复会另存一份，原文件保留。'
    return dict(result, text=text, char_count=len(text), display_char_count=visible_characters(text),
                content_hash=hashlib.sha256(text.encode()).hexdigest(), read_only=True,
                recovery_required=True, recovery_message=reason)


def candidates(indexed_ids):
    """Never discover trash, symlinks, retired kinds, or unrelated directories."""
    directory = library.LIBRARY_DIR / 'docs'
    if directory.is_symlink():
        return [], ['文稿目录路径异常，原文件已保留']
    found, errors = [], []
    for path in directory.glob('*.md'):
        if path.name.startswith('.'):
            continue
        index = library.LIBRARY_DIR / (path.stem + '.json')
        if index.is_symlink():
            errors.append(index.name)
            continue
        if path.stem in indexed_ids:
            continue
        try:
            found.append(preview(path.stem))
        except (OSError, ValueError):
            errors.append(path.name)
    return found, errors


def recover(document_id, expected_hash, request_id):
    from .agent_workspace.store import identity
    from .models import Transcript
    identity(request_id)
    with library._transaction():
        doc = preview(document_id)
        if doc['content_hash'] != expected_hash:
            raise library.RevisionConflict('原文件已有变化，请重新查看后再恢复；未覆盖任何内容')
        eid = 'recovered_' + hashlib.sha256((document_id + '\0' + request_id).encode()).hexdigest()
        library.save(Transcript(text=doc['text'], segments=[], language=None, engine='recovery',
                                source='恢复的文稿', meta={'recovered_from': document_id}),
                     doc['title'], entry_id=eid)
        return {'document_id': eid}
