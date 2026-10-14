"""One whitelisted, append-only CSV beside each recording. No raw room replies."""
import csv
from collections import deque
from datetime import datetime, timezone
import io
import os
import stat

from .contracts import LiveError, public_link
from .douyin import count, cumulative_text
from .index import directory

COLUMNS = ('sampled_at', 'recording_id', 'host_name', 'live_title', 'room_url',
           'viewer_count', 'viewer_count_raw', 'like_count', 'cumulative_view_count', 'sample_status')
MAX_POINTS = 10000
MAX_BYTES = 32 * 1024**2
STATES = {'ok', 'offline', 'access_limited', 'login_required', 'network_error', 'parse_error'}

def text(value, limit):
    value = value if type(value) is str else ''
    value = ''.join(c for c in value[:limit] if c >= ' ' and c != '\x7f')
    return "'" + value if value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@')) else value

def display_text(value):
    return value[1:] if value.startswith("''") or (value.startswith("'") and value[1:].lstrip().startswith(('=', '+', '-', '@'))) else value

def data_path(folder, *, create=False):
    root = directory(folder)
    sub = root / '直播数据'
    if not create and not sub.exists() and not sub.is_symlink():
        return None
    directory(sub, create=create)
    path = sub / '直播数据.csv'
    if path.is_symlink():
        raise LiveError('直播数据文件是链接，已保留；请检查保存位置')
    return path

def _open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise LiveError('直播数据文件无法安全读取，原文件保留')
    if info.st_size > MAX_BYTES:
        os.close(fd)
        raise LiveError('直播数据文件过大，请直接打开文件夹读取；原文件保留')
    return fd

def append_sample(recording, host, observation, *, sampled_at=None):
    path = data_path(recording['directory'], create=True)
    metrics = observation.metrics
    # parse_room retains metrics only for an explicitly live room when video
    # URLs fail. Public data validity must not depend on playable video URLs.
    status = 'ok' if observation.state in {'live', 'parse_error'} and metrics else observation.state
    if status not in STATES:
        status = 'parse_error'
    available = status == 'ok'
    cumulative = count(metrics.cumulative_view_count) if available else None
    if cumulative is None and available:
        cumulative = cumulative_text(metrics.cumulative_view_count_raw) or None
    room_url = 'https://live.douyin.com/' + observation.web_rid if observation.web_rid else host['source']
    room_url = public_link(room_url)
    row = [sampled_at or datetime.now(timezone.utc).isoformat(timespec='seconds'), recording['id'],
           text(observation.name or recording['name'], 200), text(metrics.title, 500) if metrics else '', room_url,
           count(metrics.viewer_count) if available else None,
           text(metrics.viewer_count_raw, 100) if available else '',
           count(metrics.like_count) if available else None,
           cumulative, status]
    # Caller serializes readers/writers. Inspect the exact opened file before appending.
    fd = _open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'r+b') as file:
        size = os.fstat(file.fileno()).st_size
        if size:
            header = file.readline(4096).decode('utf-8-sig')
            file.seek(-1, os.SEEK_END)
            if tuple(next(csv.reader([header]))) != COLUMNS or file.read(1) != b'\n':
                raise LiveError('直播数据文件不完整，原文件保留；未追加内容')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        if not size:
            output.write('\ufeff')
            writer.writerow(COLUMNS)
        writer.writerow(row)
        file.write(output.getvalue().encode('utf-8'))
        file.flush()
        os.fsync(file.fileno())

def read_data(recording):
    path = data_path(recording['directory'])
    result = {'samples': [], 'sample_count': 0, 'valid_count': 0, 'invalid_count': 0,
              'peak_viewers': None, 'last_viewers': None, 'last_viewers_raw': '',
              'title': '', 'folder': '', 'truncated': False}
    if path is None or (not path.exists() and not path.is_symlink()):
        return result
    points = deque(maxlen=MAX_POINTS)
    with os.fdopen(_open(path, os.O_RDONLY), 'r', encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise LiveError('直播数据格式无法识别，原文件保留；请打开文件夹检查')
        previous = None
        for row in reader:
            try:
                when = datetime.fromisoformat(row['sampled_at'])
                at = when.timestamp()
                if (when.tzinfo is None or row['recording_id'] != recording['id'] or
                        row['sample_status'] not in STATES or None in row or
                        any(value is None for value in row.values()) or (previous is not None and at < previous)):
                    raise ValueError
                numeric = [count(row[key]) for key in ('viewer_count', 'like_count', 'cumulative_view_count')]
                cumulative_raw = cumulative_text(row['cumulative_view_count']) if numeric[2] is None else ''
                if (any(row[key] != '' and number is None for key, number in
                        zip(('viewer_count', 'like_count'), numeric[:2])) or
                        (row['cumulative_view_count'] != '' and numeric[2] is None and not cumulative_raw)):
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError):
                result['invalid_count'] += 1
                # A damaged row must not cause a line to bridge a missing observation.
                if points:
                    points.append({'at': points[-1]['at'], 'viewer_count': None, 'like_count': None,
                                   'cumulative_view_count': None, 'status': 'parse_error'})
                continue
            previous = at
            ok = row['sample_status'] == 'ok'
            if not ok:
                numeric = [None, None, None]
                cumulative_raw = ''
            viewers, likes, cumulative = numeric
            points.append({'at': at, 'viewer_count': viewers, 'like_count': likes,
                           'cumulative_view_count': cumulative, 'cumulative_view_count_raw': cumulative_raw,
                           'status': row['sample_status']})
            result['sample_count'] += 1
            result['valid_count'] += int(ok and (any(n is not None for n in numeric) or bool(cumulative_raw)))
            if viewers is not None:
                result['peak_viewers'] = max(viewers, result['peak_viewers'] or 0)
            result['last_viewers'] = viewers
            raw = row['viewer_count_raw'] if ok else ''
            result['last_viewers_raw'] = display_text(raw)[:100]
            title = row['live_title']
            if title:
                result['title'] = display_text(title)[:500]
    result.update(samples=list(points), folder=str(path.parent),
                  truncated=result['sample_count'] + result['invalid_count'] > MAX_POINTS)
    return result
