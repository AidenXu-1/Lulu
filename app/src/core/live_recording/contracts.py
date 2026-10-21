"""Closed request and media contracts for the measured GUI channel."""
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
import math
import re

WEB_RID_PATTERN = r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}'

class LiveError(ValueError):
    pass

MESSAGES = {
    'live': '正在录制', 'offline': '等待开播', 'paused': '已暂停',
    'resolving': '正在识别', 'checking': '正在检查', 'reconnecting': '正在重连',
    'login_required': '需要登录凭据', 'access_limited': '访问受限，稍后重试',
    'network_error': '网络连接失败，稍后重试', 'parse_error': '暂时无法识别直播状态',
    'disk_full': '空间不足，已暂停', 'write_error': '保存失败，已暂停',
    'stopping': '正在保存最后一段', 'credential_error': '凭据无法读取，已暂停',
    'recovery_required': '上次录制中断，原文件保留',
    'recovery_checked': '上次录制中断，已检查保留文件',
}

LIVE_MESSAGES = {'live': '直播中', 'offline': '未直播', 'unknown': '无法确认'}
QUERY_ERRORS = {'login_required', 'access_limited', 'network_error', 'parse_error'}

def confirmed_live_state(state):
    # Local recorder activity and request failures never prove a broadcast ended.
    return state if state in {'live', 'offline'} else 'unknown'

@dataclass(frozen=True)
class Stream:
    url: str = field(repr=False)
    protocol: str = 'flv'
    bitrate: int = 0
    codec: str = ''

@dataclass(frozen=True)
class LiveMetrics:
    title: str = ''
    viewer_count: int | None = None
    viewer_count_raw: str = ''
    like_count: int | None = None
    cumulative_view_count: int | None = None
    cumulative_view_count_raw: str = ''

@dataclass(frozen=True)
class Observation:
    state: str
    sec_uid: str = ''
    web_rid: str = ''
    name: str = ''
    streams: tuple[Stream, ...] = field(default=(), repr=False)
    received_bytes: int = 0
    metrics: LiveMetrics | None = None
    room_id: str = ''

def recording_message(recording):
    """Use the same media lifecycle label in the archive and both detail views."""
    if recording['ended'] is None:
        return {'live': '正在录制', 'connecting': '正在连接直播',
                'reconnecting': '等待重连', 'stopping': '正在保存最后一段'}.get(
                    recording.get('recording_state'), '正在录制')
    reason = recording.get('reason', '')
    if reason == 'recovery_required':
        return '上次录制中断'
    if reason == 'recovery_checked':
        return '中断录像已检查'
    if reason in {'recording_error', 'write_error', 'reconnect_timeout'}:
        return '录制中断'
    return '录制有缺口' if recording.get('gaps', '[]') != '[]' else '已保存'

def public_link(value):
    if type(value) is not str or len(value) > 4096:
        raise LiveError('请粘贴抖音个人主页或直播间链接')
    match = re.search(r'https://[^\s<>"\u3000]+', value.strip())
    if not match:
        raise LiveError('请粘贴完整的 https 抖音链接')
    url = urlsplit(match.group().rstrip('。，,;；)）'))
    if (url.scheme != 'https' or url.username or url.password or url.port not in {None, 443}
            or url.hostname not in {'www.douyin.com', 'live.douyin.com', 'v.douyin.com',
                                    'www.iesdouyin.com', 'webcast.amemv.com'}):
        raise LiveError('仅支持抖音个人主页、分享链接和直播间')
    # Keep only public identity fields, never share tokens or signed stream URLs.
    from urllib.parse import parse_qs, urlencode
    query = parse_qs(url.query)
    kept = {k: query[k][0] for k in ('sec_user_id',) if k in query}
    path = url.path.rstrip('/') or '/'
    supported = ((url.hostname == 'live.douyin.com' and re.fullmatch(r'/(?:live/)?' + WEB_RID_PATTERN, path))
                 or (url.hostname == 'v.douyin.com' and re.fullmatch(r'/[A-Za-z0-9_-]{2,100}', path))
                 or re.fullmatch(r'/(?:user|share/user)/[A-Za-z0-9_-]{1,200}', path)
                 or re.fullmatch(r'/(?:douyin/webcast/)?(?:reflow|share/live)/[0-9]{1,30}', path))
    if not supported:
        raise LiveError('这个链接不是支持的抖音主页或直播间')
    return urlunsplit(('https', url.netloc, path, urlencode(kept), ''))

def safe_stream_url(value):
    try:
        url = urlsplit(value)
        domains = ('douyincdn.com', 'douyinliving.com', 'douyin.com', 'bytecdn.cn', 'bytefcdn.com',
                   'byteimg.com', 'ibytedtos.com', 'pstatp.com', 'amemv.com')
        return (type(value) is str and len(value) <= 16384 and url.scheme in {'http', 'https'}
                and not url.username and not url.password and url.port in {None, 80, 443}
                and any(url.hostname == d or (url.hostname or '').endswith('.' + d) for d in domains))
    except ValueError:
        return False

def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[a-z]+-[a-f0-9]{32}', value):
        raise LiveError('操作编号无效，请刷新页面')
    return value

FIELDS = {
    'open': {'nonce'}, 'heartbeat': {'session'}, 'stop': {'session'},
    'snapshot': {'session', 'tab', 'query', 'filter', 'page'},
    'add': {'session', 'source'}, 'start': {'session', 'host_id'},
    'pause': {'session', 'host_id'}, 'remove': {'session', 'host_id'},
    'pause_all': {'session'}, 'directory': {'session', 'path'},
    'credential': {'session', 'value'}, 'detail': {'session', 'recording_id'},
    'data': {'session', 'recording_id'},
    'comments_setting': {'session', 'enabled'},
    'comments': {'session', 'recording_id', 'query', 'page'},
    'comments_export': {'session', 'recording_id', 'format', 'path'},
    'delete_recording': {'session', 'recording_id'},
    'export': {'session', 'segment_id', 'format', 'path'}, 'export_status': {'session', 'export_id'},
    'transcribe': {'session', 'segment_id'},
}
ORDERED_CONTROLS = {'start', 'pause', 'pause_all', 'remove', 'directory'}

def validate_request(operation, arguments):
    if operation not in FIELDS or type(arguments) is not dict:
        raise LiveError('直播操作字段无效')
    fields = set(arguments)
    if fields != FIELDS[operation] and not (operation in ORDERED_CONTROLS and fields == FIELDS[operation] | {'sequence'}) and not (operation == 'credential' and fields == FIELDS[operation] | {'source'}) and not (operation == 'comments' and
            fields == FIELDS[operation] | {'range_start', 'range_end'}):
        raise LiveError('直播操作字段无效')
    for key, value in arguments.items():
        if key == 'sequence':
            if type(value) is not int or not 1 <= value <= 2**53 - 1:
                raise LiveError('直播操作顺序无效')
        elif key in {'range_start', 'range_end'}:
            try:
                valid = type(value) in {int, float} and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise LiveError('弹幕时间范围无效')
        elif key == 'enabled':
            if type(value) is not bool:
                raise LiveError('弹幕开关无效')
        elif key == 'page':
            if type(value) is not int or not 1 <= value <= 1000000:
                raise LiveError('页码无效')
        elif type(value) is not str or len(value) > (16384 if key == 'value' else 4096):
            raise LiveError('直播操作内容无效')
    if operation == 'open' and not re.fullmatch(r'[a-f0-9]{32}', arguments['nonce']):
        raise LiveError('启动标识无效')
    for key in ('session', 'host_id', 'recording_id', 'segment_id', 'export_id'):
        if key in arguments:
            identifier(arguments[key])
    if operation == 'credential' and arguments.get('source', 'manual') not in {'browser', 'manual'}:
        raise LiveError('登录来源无效')
    if operation == 'add':
        public_link(arguments['source'])
    if operation == 'snapshot':
        if arguments['tab'] not in {'hosts', 'recordings'} or arguments['filter'] not in {
                'all', 'live', 'offline', 'unknown', 'enabled', 'paused', 'today', 'week', 'month'}:
            raise LiveError('筛选条件无效')
        if len(arguments['query']) > 300:
            raise LiveError('搜索内容过长')
    if operation == 'credential' and ('\r' in arguments['value'] or '\n' in arguments['value']):
        raise LiveError('凭据不能包含换行')
    if operation == 'comments' and len(arguments['query']) > 300:
        raise LiveError('搜索内容过长')
    if 'range_start' in arguments and arguments['range_end'] <= arguments['range_start']:
        raise LiveError('弹幕时间范围无效')
    if operation == 'comments_export' and arguments['format'] not in {'txt', 'csv'}:
        raise LiveError('请选择 TXT 或 CSV 文件')
    if operation == 'export' and arguments['format'] not in {'mp4', 'mp3'}:
        raise LiveError('请选择 MP4 视频或 MP3 音频')
