"""Local append-only comments beside one recording; never stores access credentials."""
from collections import deque
from array import array
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import csv
import json
import math
import os
from pathlib import Path
import stat
import threading

from .contracts import LiveError
from .index import directory

MAX_BYTES = 256 * 1024**2
MAX_LINE = 128 * 1024
PAGE_SIZE = 200

def file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
STATES = {
    'connecting': '正在连接弹幕',
    'waiting': '已连接，等待弹幕',
    'recording': '正在保存弹幕',
    'reconnecting': '弹幕连接中断，正在重连',
    'retrying': '弹幕暂未连接，正在重试；录像继续',
    'unavailable': '暂未取得本场弹幕编号',
    'write_error': '弹幕未能保存，录像继续；请检查保存位置或可用空间',
    'limit': '弹幕记录达到 256 MiB，已停止保存；录像继续',
    'ended': '弹幕保存已结束',
    'interrupted': '上次弹幕记录中断，已有记录保留',
    'disabled': '本场未开启弹幕记录',
    'legacy': '这场录像没有弹幕记录',
}

def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def csv_text(value):
    # CSV quoting preserves newlines and emoji; only neutralize spreadsheet formulas.
    return "'" + value if value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@')) else value

def status_text(state, count=0):
    if state in {'recording', 'ended'}:
        return f'弹幕已保存 {count:,} 条' + ('，已结束' if state == 'ended' else '')
    return STATES.get(state, STATES['interrupted'])

def log_path(folder, *, create=False):
    root = directory(folder)
    sub = root / '弹幕记录'
    if not create and not sub.exists() and not sub.is_symlink():
        return None
    directory(sub, create=create)
    if sub.stat().st_uid != os.getuid():
        raise LiveError('弹幕文件夹归属不一致，原文件保留')
    return sub / '弹幕记录.jsonl'

def open_log(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise LiveError('弹幕记录无法安全读取，原文件保留')
    if info.st_size > MAX_BYTES:
        os.close(fd)
        raise LiveError(STATES['limit'])
    return fd

class CommentLog:
    def __init__(self, recording):
        self.recording = recording
        self.lock = threading.RLock()
        self.signature = None
        self.epoch = 0

    def append(self, entries):
        with self.lock:
            self._append(entries)

    def _append(self, entries):
        if not entries:
            return
        payload = b''.join((json.dumps({**entry, 'recording_id': self.recording['id']},
                            ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8') for entry in entries)
        if any(len(line) > MAX_LINE for line in payload.splitlines()):
            raise LiveError('弹幕内容超出可保存范围，原文件保留')
        path = log_path(self.recording['directory'], create=True)
        with os.fdopen(open_log(path, os.O_RDWR | os.O_CREAT | os.O_APPEND), 'r+b') as file:
            before = os.fstat(file.fileno())
            size = before.st_size
            if file_signature(before) != self.signature:
                self.epoch += 1  # External replacement/edit: cached prefixes cannot be trusted.
            if size:
                file.seek(-1, os.SEEK_END)
                if file.read(1) != b'\n':
                    raise LiveError('弹幕记录末尾不完整，原文件保留；未追加内容')
            if size + len(payload) > MAX_BYTES:
                raise LiveError(STATES['limit'])
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
            self.signature = file_signature(os.fstat(file.fileno()))

def entries(recording, *, io_lock=None):
    # Reserve only a complete file-length snapshot. Release before streaming so
    # long searches/exports cannot hold up subsequent network writes.
    with io_lock if io_lock is not None else nullcontext():
        path = log_path(recording['directory'])
        if path is None or (not path.exists() and not path.is_symlink()):
            return
        file = os.fdopen(open_log(path, os.O_RDONLY), 'rb')
        remaining = os.fstat(file.fileno()).st_size
    with file:
        for _, value in _entries(file, recording, remaining):
            yield value

def _entries(file, recording, limit, start=0):
    file.seek(start)
    remaining = limit - start
    while remaining:
        offset = limit - remaining
        line = file.readline(min(remaining, MAX_LINE + 1))
        remaining -= len(line)
        if len(line) > MAX_LINE or not line.endswith(b'\n'):
            raise LiveError('弹幕记录不完整，原文件保留；请检查文件夹')
        try:
            value = json.loads(line)
            when = datetime.fromisoformat(value['received_at'])
            if (value['recording_id'] != recording['id'] or when.tzinfo is None
                    or value['kind'] not in {'chat', 'status'}):
                raise ValueError()
            if value['kind'] == 'chat':
                if (type(value['nickname']) is not str or type(value['text']) is not str
                        or not value['text'] or len(value['text']) > 20000
                        or type(value['message_id']) is not str
                        or type(value['elapsed_seconds']) not in {int, float}
                        or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0):
                    raise ValueError()
            elif value['state'] not in STATES:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            raise LiveError('弹幕记录格式异常，原文件保留；请检查文件夹') from None
        yield offset, value


class CommentReadCache:
    """One query per scene; only 200 rows, 80 bins and sparse byte offsets retained."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = None


def read_comments(recording, query='', page=1, *, io_lock=None, time_range=None, cache=None, log=None):
    cache = cache if cache is not None else CommentReadCache()
    with cache.lock:
        try:
            return _read_comments(recording, query, page, io_lock=io_lock, time_range=time_range,
                                  cache=cache, log=log)
        except Exception:
            cache.state = None
            raise


def _read_comments(recording, query, page, *, io_lock, time_range, cache, log):
    lock = log.lock if log is not None else io_lock
    # The opened descriptor and length form a single snapshot for count AND rows.
    with lock if lock is not None else nullcontext():
        path = log_path(recording['directory'])
        exists = path is not None and (path.exists() or path.is_symlink())
        file = os.fdopen(open_log(path, os.O_RDONLY), 'rb') if exists else None
        signature = file_signature(os.fstat(file.fileno())) if file else None
        epoch = (log, log.epoch) if log and log.signature == signature else None
    try:
        return _read_snapshot(file, signature, epoch, recording, query, page,
                              time_range, cache, path if exists else None, log, lock)
    finally:
        if file is not None:
            file.close()


def _read_snapshot(file, signature, epoch, recording, query, page, time_range, cache, path, log, lock):
    query = query.casefold()
    key = (recording['id'], recording['directory'], query, time_range,
           recording.get('started'), recording.get('ended'))
    old = cache.state
    same = old is not None and old['key'] == key
    unchanged = same and old['signature'] == signature
    append = (same and signature is not None and old['signature'] is not None
              and signature[:2] == old['signature'][:2] and signature[2] > old['signature'][2]
              and epoch is not None and old['epoch'] == epoch)
    if unchanged and old['requested_page'] == page:
        return deepcopy(old['result'])
    if unchanged or append:
        count, matching, query_matching = old['count'], old['matching'], old['query_matching']
        origin, duration, step, gap_start = old['origin'], old['duration'], old['step'], old['gap_start']
        events, buckets, offsets, tail = old['events'], old['buckets'], old['offsets'], old['tail']
        start_offset = old['signature'][2] if old['signature'] else 0
    else:
        count = matching = query_matching = 0
        events, offsets, tail = deque(maxlen=30), array('Q'), deque(maxlen=PAGE_SIZE)
        origin = recording.get('started')
        duration = max(0, (recording.get('ended') or origin or 0) - (origin or 0))
        step, buckets, gap_start, start_offset = 5, {}, None, 0
    limit = signature[2] if signature else 0

    def extend(value):
        nonlocal duration, step, buckets
        duration = max(duration, value)
        while duration >= step * 80:
            step *= 2
            merged = {}
            for i, values in buckets.items():
                target = merged.setdefault(i // 2, [0, 0, False])
                target[0] += values[0]
                target[1] += values[1]
                target[2] |= values[2]
            buckets = merged

    def mark_gap(start, end):
        extend(end)
        if end > start:
            for i in range(int(start // step), min(80, math.ceil(end / step))):
                buckets.setdefault(i, [0, 0, False])[2] = True

    def accepts(entry):
        return ((not query or query in entry['nickname'].casefold() or query in entry['text'].casefold())
                and (time_range is None or time_range[0] <= entry['elapsed_seconds'] < time_range[1]))

    extend(duration)
    for offset, entry in _entries(file, recording, limit, start_offset) if file else ():
        if entry['kind'] == 'status':
            events.append(entry)
            if origin is not None:
                at = max(0, datetime.fromisoformat(entry['received_at']).timestamp() - origin)
                extend(at)
                state = entry['state']
                if state in {'waiting', 'recording', 'ended'}:
                    if gap_start is not None:
                        mark_gap(gap_start, at)
                        gap_start = None
                elif gap_start is None:
                    gap_start = at
            continue
        if origin is None:
            origin = datetime.fromisoformat(entry['received_at']).timestamp() - entry['elapsed_seconds']
        # At most 80 aggregate bins, regardless of the file length or recording duration.
        extend(entry['elapsed_seconds'] + .001)
        bucket = buckets.setdefault(int(entry['elapsed_seconds'] // step), [0, 0, False])
        bucket[0] += 1
        count += 1
        if not query or query in entry['nickname'].casefold() or query in entry['text'].casefold():
            query_matching += 1
            bucket[1] += 1
            if time_range is None or time_range[0] <= entry['elapsed_seconds'] < time_range[1]:
                if matching % PAGE_SIZE == 0:
                    offsets.append(offset)
                tail.append(entry)
                matching += 1
    # Do not store a provisional open gap as permanent history.
    stored_buckets = deepcopy(buckets)
    if gap_start is not None:
        mark_gap(gap_start, duration)
    bins = []
    for i in range(min(80, math.ceil(duration / step)) if count or events else 0):
        number, matches, interrupted = buckets.get(i, [0, 0, False])
        bins.append({'start': i * step, 'end': min((i + 1) * step, duration),
                     'count': number, 'matching': matches, 'interrupted': interrupted})
    pages = max(1, (matching + PAGE_SIZE - 1) // PAGE_SIZE)
    requested_page = page
    page = max(1, min(page, pages))
    start, end = max(0, matching - page * PAGE_SIZE), matching - (page - 1) * PAGE_SIZE
    messages = list(tail) if page == 1 else []
    if matching and page > 1:
        block = start // PAGE_SIZE
        position = block * PAGE_SIZE
        for _, entry in _entries(file, recording, limit, offsets[block]):
            if entry['kind'] != 'chat' or not accepts(entry):
                continue
            if position >= start:
                messages.append(entry)
            position += 1
            if position >= end:
                break
    if file:
        with lock if lock is not None else nullcontext():
            after = file_signature(os.fstat(file.fileno()))
            trusted_append = (log is not None and epoch is not None and (log, log.epoch) == epoch
                              and log.signature == after and after[:2] == signature[:2]
                              and after[2] >= limit)
        if after != signature and not trusted_append:
            raise LiveError('弹幕文件在读取时发生变化，已有文件保留；请重试')
    result = {'messages': messages, 'total': count, 'matching': matching, 'page': page, 'pages': pages,
            'events': list(events), 'folder': str(path.parent) if path else '',
            'has_file': path is not None, 'read_error': False, 'query_matching': query_matching,
            'timeline': {'duration': duration, 'step': step, 'bins': bins, 'has_status': bool(events)}}
    cache.state = dict(key=key, signature=signature, epoch=epoch, count=count, matching=matching,
        query_matching=query_matching, origin=origin, duration=duration, step=step, gap_start=gap_start,
        events=events, buckets=stored_buckets, offsets=offsets, tail=tail,
        requested_page=requested_page, result=deepcopy(result))
    return result

def export_comments(recording, target, format, *, cancelled=lambda: False, io_lock=None):
    target = Path(target)
    if format not in {'txt', 'csv'} or target.suffix.lower() != '.' + format:
        raise LiveError('请选择 TXT 或 CSV 文件')
    parent = directory(target.parent)
    if parent.stat().st_uid != os.getuid():
        raise LiveError('导出文件夹不属于当前用户，请另选位置')
    path = log_path(recording['directory'])
    if path is None or not path.exists():
        raise LiveError('这场录像没有弹幕记录，未创建导出文件')
    # Build the snapshot first, using a temporary spool rather than a large RAM copy.
    from tempfile import TemporaryFile
    with TemporaryFile(mode='w+', encoding='utf-8', newline='') as spool:
        writer = csv.writer(spool) if format == 'csv' else None
        if writer:
            spool.write('\ufeff')
            writer.writerow(['收到时间（含时区）', '昵称', '弹幕内容', '记录类型'])
        else:
            spool.write('时间为本机收到弹幕的时间，不代表录像播放位置。\n')
        for entry in entries(recording, io_lock=io_lock):
            if cancelled():
                raise LiveError('导出已停止，原弹幕记录保留')
            state = entry['kind'] == 'status'
            name = '系统' if state else entry['nickname']
            content = STATES[entry['state']] if state else entry['text']
            if writer:
                writer.writerow([entry['received_at'], csv_text(name), csv_text(content),
                                 '状态' if state else '弹幕'])
            else:
                content = content.replace('\r', r'\r').replace('\n', r'\n')
                name = name.replace('\r', r'\r').replace('\n', r'\n')
                spool.write(f"[{entry['received_at']}] {name}：{content}\n")
        spool.flush()
        spool.seek(0)
        if cancelled():
            raise LiveError('导出已停止，原弹幕记录保留')
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise LiveError('目标文件已存在，请换一个文件名；已有文件保留') from None
        # No overwrite or unlink cleanup: a failed export remains visible for recovery.
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as output:
            while chunk := spool.read(64 * 1024):
                if cancelled():
                    raise LiveError('导出已停止，原记录保留；未完成的导出文件也已保留')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    return str(target)
