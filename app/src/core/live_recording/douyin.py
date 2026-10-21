"""Narrow requests adapter for StreamGet v4.0.10's Douyin web rules.

No baked credentials, browser scraping, JS runtime or raw exception logging.
"""
import json
import re
from dataclasses import replace
from decimal import Decimal
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .ab_sign import ab_sign
from .contracts import LiveMetrics, Observation, Stream, WEB_RID_PATTERN, public_link, safe_stream_url

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36')
ALLOWED = {'www.douyin.com', 'live.douyin.com', 'v.douyin.com',
           'www.iesdouyin.com', 'webcast.amemv.com'}

class _ResponseError(Exception):
    def __init__(self, state):
        self.state = state

def _object(value):
    return json.loads(value) if isinstance(value, str) else value

def count(value):
    """Only an explicit whole number is exact; '800+' and '1.2万' stay unknown."""
    if type(value) is int and 0 <= value <= 10**15:
        return value
    if type(value) is str and re.fullmatch(r'[0-9]{1,16}', value.strip()):
        number = int(value)
        return number if number <= 10**15 else None
    return None

def cumulative_text(value):
    """Keep a platform's rounded count or lower bound without inventing precision."""
    if type(value) is not str:
        return ''
    value = value.strip()
    match = re.fullmatch(r'([0-9]{1,16}(?:\.[0-9]{1,4})?)(万|亿)(?:[+＋])?|([0-9]{1,16})[+＋]', value)
    if not match:
        return ''
    number = (Decimal(match[1]) * (10000 if match[2] == '万' else 100000000)
              if match[1] else Decimal(match[3]))
    return value if 0 <= number <= 10**15 else ''

def room_metrics(room):
    stats = room.get('stats') if type(room.get('stats')) is dict else {}
    display = room.get('room_view_stats') if type(room.get('room_view_stats')) is dict else {}
    # Do not mix total viewers with currently online viewers, or rank/heat values.
    raw = stats.get('user_count_str')
    if raw is None or raw == '':
        if '在线' in str(display.get('display_long', '')):
            raw = display.get('display_value')
    if raw is None or raw == '':
        raw = room.get('user_count_str', room.get('user_count'))
    raw = str(raw)[:100] if type(raw) in {str, int} else ''
    title = room.get('title')
    cumulative = count(stats.get('total_user_str'))
    # Some rooms explicitly display a cumulative "人看过" counter instead of
    # online viewers. Its integer display_value is separate from rounded text.
    if (cumulative is None and display.get('display_type') == 3 and display.get('incremental') is True
            and '人看过' in str(display.get('display_long', ''))):
        cumulative = count(display.get('display_value'))
    cumulative_raw = cumulative_text(stats.get('total_user_str')) if cumulative is None else ''
    return LiveMetrics(title=title[:500] if type(title) is str else '',
        viewer_count=count(raw), viewer_count_raw=raw, like_count=count(room.get('like_count')),
        cumulative_view_count=cumulative, cumulative_view_count_raw=cumulative_raw)

def parse_room(payload, web_rid=''):
    """Only explicit status=4 means offline; empty/restricted data is an error."""
    if type(payload) is not dict:
        return Observation('parse_error')
    if payload.get('status_code') not in {None, 0}:
        return Observation('access_limited')
    data = payload.get('data')
    if type(data) is not dict:
        return Observation('parse_error')
    rooms = data.get('data')
    room = rooms[0] if isinstance(rooms, list) and rooms else data.get('room')
    if type(room) is not dict:
        return Observation('login_required' if data.get('prompts') else 'parse_error')
    user = room.get('owner') or data.get('user') or {}
    sec_uid = str(user.get('sec_uid') or user.get('secUid') or '')
    rid = str(user.get('web_rid') or user.get('webRid') or web_rid)
    if rid and re.fullmatch(WEB_RID_PATTERN, rid) is None:
        rid = ''
    name = str(user.get('nickname') or '')[:200]
    metrics = room_metrics(room)
    if room.get('status') == 4:
        return Observation('offline', sec_uid, rid, name, metrics=metrics)
    if room.get('status') != 2:
        return Observation('parse_error', sec_uid, rid, name)
    stream_url = room.get('stream_url') or room.get('streamUrl') or {}
    choices = []
    try:
        core = stream_url.get('live_core_sdk_data') or stream_url.get('liveCoreSdkData') or {}
        pull = core.get('pull_data') or core.get('pullData') or {}
        raw = _object(pull.get('stream_data') or pull.get('streamData') or {})
        for quality, entry in raw.get('data', {}).items():
            for line in ('main', 'backup'):
                stream = entry.get(line) or {}
                try:
                    sdk = _object(stream.get('sdk_params') or {})
                    bitrate = max(0, int(sdk.get('vbitrate') or 0))
                    # Origin ranks above unavailable bitrate metadata; equal bitrate prefers H264.
                    if quality.lower() == 'origin' and not bitrate:
                        bitrate = 100000000
                    codec = str(sdk.get('VCodec') or '').lower()
                except (TypeError, ValueError, AttributeError):
                    continue  # One bad route must not hide its usable alternative.
                for protocol in ('flv', 'hls'):
                    url = stream.get(protocol)
                    if safe_stream_url(url):
                        choices.append(Stream(url, protocol, bitrate, codec))
    except (TypeError, ValueError, AttributeError):
        return Observation('parse_error', sec_uid, rid, name, metrics=metrics)
    if not choices:
        quality_rank = {'ORIGIN': 100000000, 'FULL_HD1': 8000000, 'HD1': 4000000,
                        'SD1': 2000000, 'SD2': 1000000}
        for protocol, keys in (('flv', ('flv_pull_url', 'flvPullUrl')),
                               ('hls', ('hls_pull_url_map', 'hlsPullUrlMap'))):
            values = stream_url.get(keys[0]) or stream_url.get(keys[1]) or {}
            if isinstance(values, dict):
                for quality, url in values.items():
                    if safe_stream_url(url):
                        choices.append(Stream(url, protocol, quality_rank.get(quality, 0)))
    choices.sort(key=lambda s: (-s.bitrate, s.codec not in {'h264', 'avc'}, s.protocol != 'flv'))
    internal = str(room.get('id_str') or room.get('id') or '')
    internal = internal if re.fullmatch(r'[0-9]{1,20}', internal) else ''
    return Observation('live' if choices else 'parse_error', sec_uid, rid, name, tuple(choices),
                       metrics=metrics, room_id=internal)

class DouyinProvider:
    def __init__(self, session_factory=requests.Session):
        self.session_factory = session_factory

    def _visitor_cookie(self, session):
        """Same anonymous bootstrap as collection, scoped to this bounded check."""
        self._get(session, 'https://www.douyin.com/')
        if time.monotonic() > session._lulu_deadline:
            raise _ResponseError('network_error')
        # This fixed endpoint creates a public visitor ID, never a login session.
        with session.post('https://ttwid.bytedance.com/ttwid/union/register/',
                json={'region': 'cn', 'aid': 6383, 'needFid': False, 'service': 'www.douyin.com',
                      'migrate_info': {'ticket': '', 'source': 'node'}, 'cbUrlProtocol': 'https', 'union': True},
                headers={'User-Agent': UA}, timeout=(5, 15), allow_redirects=False,
                stream=True, verify=True) as reply:
            if reply.status_code in {401, 403, 429}:
                raise _ResponseError('access_limited')
            if reply.status_code != 200:
                raise _ResponseError('network_error')
            size = 0
            for chunk in reply.iter_content(8192):
                size += len(chunk)
                session._lulu_received += len(chunk)
                if time.monotonic() > session._lulu_deadline:
                    raise _ResponseError('network_error')
                if size > 64 * 1024:
                    raise _ResponseError('access_limited')
        visitor = next((item.value for item in session.cookies
                        if item.name == 'ttwid' and item.domain.lstrip('.') == 'bytedance.com'), '')
        if not visitor:
            raise _ResponseError('access_limited')
        # The visitor endpoint sets ttwid on bytedance.com; Douyin needs it explicitly.
        # Keep the homepage nonce scoped to its own host; share-page nonces differ.
        request = requests.Request('GET', 'https://www.douyin.com/').prepare()
        cookie = requests.cookies.get_cookie_header(session.cookies, request) or ''
        cookie = (cookie + '; ' if cookie else '') + 'ttwid=' + visitor
        if len(cookie) > 16384:
            raise _ResponseError('access_limited')
        return cookie

    @staticmethod
    def _get(session, url, credential='', params=None):
        headers = {'User-Agent': UA, 'Referer': 'https://live.douyin.com/',
                   'Accept-Language': 'zh-CN,zh;q=0.8'}
        if credential:
            headers['Cookie'] = credential
        for _ in range(6):
            if time.monotonic() > getattr(session, '_lulu_deadline', float('inf')):
                raise _ResponseError('network_error')
            target = urlsplit(url)
            if (target.scheme != 'https' or target.hostname not in ALLOWED or target.username
                    or target.password or target.port not in {None, 443}):
                raise _ResponseError('parse_error')
            with session.get(url, params=params, headers=headers, timeout=(5, 15),
                             allow_redirects=False, stream=True, verify=True) as reply:
                if reply.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, reply.headers.get('Location', ''))
                    params = None
                    continue
                if reply.status_code in {403, 429}:
                    raise _ResponseError('access_limited')
                if reply.status_code == 401:
                    raise _ResponseError('login_required')
                if reply.status_code != 200:
                    raise _ResponseError('network_error')
                chunks, size = [], 0
                for chunk in reply.iter_content(8192):
                    size += len(chunk)
                    session._lulu_received = getattr(session, '_lulu_received', 0) + len(chunk)
                    if time.monotonic() > getattr(session, '_lulu_deadline', float('inf')):
                        raise _ResponseError('network_error')
                    if size > 4 * 1024 * 1024:
                        raise _ResponseError('parse_error')
                    chunks.append(chunk)
                text = b''.join(chunks).decode('utf-8', errors='replace')
                if any(s in text.lower() for s in ('captcha', 'verifycenter', '验证后继续')):
                    raise _ResponseError('access_limited')
                return text, url
        raise _ResponseError('parse_error')

    def _api(self, session, url, params, credential):
        from urllib.parse import urlencode
        query = urlencode(params)
        text, _ = self._get(session, url, credential, {**params, 'a_bogus': ab_sign(query, UA)})
        if not text.strip():
            raise _ResponseError('access_limited')
        return json.loads(text)

    def check(self, host, credential=''):
        sec_uid, rid, name = host.get('sec_uid', ''), host.get('web_rid', ''), host.get('name', '')
        session = None
        try:
            source = public_link(host['source'])
            with self.session_factory() as session:
                session.trust_env = False
                session._lulu_deadline = time.monotonic() + 25
                session._lulu_received = 0
                source_parts = urlsplit(source)
                if not rid and source_parts.hostname == 'live.douyin.com':
                    rid = source_parts.path.rsplit('/', 1)[-1]
                if source_parts.hostname == 'v.douyin.com' or '/reflow/' in source_parts.path or '/share/' in source_parts.path:
                    # Resolve the public share before bootstrapping its API session.
                    # A www.douyin.com nonce is invalid on the mobile share host.
                    text, resolved = self._get(session, source)
                    parts = urlsplit(resolved)
                    sec_uid = parse_qs(parts.query).get('sec_user_id', [sec_uid])[0]
                    user_match = re.search(r'/(?:user|share/user)/([\w-]+)', parts.path)
                    room_match = re.fullmatch(r'/(?:live/)?(' + WEB_RID_PATTERN + ')', parts.path)
                    sec_uid = user_match.group(1) if user_match else sec_uid
                    rid = room_match.group(1) if room_match and parts.hostname == 'live.douyin.com' else rid
                    if not rid:
                        match = re.search(r'"web_?rid"\s*:\s*"?(\d+)', text, re.I)
                        if match:
                            rid = match.group(1)
                if not sec_uid:
                    match = re.search(r'/(?:user|share/user)/([\w-]+)', source_parts.path)
                    sec_uid = match.group(1) if match else ''
                if not rid and source_parts.hostname == 'live.douyin.com':
                    rid = source_parts.path.rsplit('/', 1)[-1]
                if not credential:
                    credential = self._visitor_cookie(session)
                if sec_uid:
                    profile = self._api(session, 'https://www.douyin.com/aweme/v1/web/user/profile/other/',
                                        {'aid': '6383', 'device_platform': 'webapp', 'sec_user_id': sec_uid}, credential)
                    if profile.get('status_code') not in {0, None}:
                        raise _ResponseError('access_limited')
                    user = profile.get('user') or {}
                    if user.get('sec_uid') and user['sec_uid'] != sec_uid:
                        raise _ResponseError('parse_error')
                    name = str(user.get('nickname') or name)[:200]
                    # A published room ID identifies the ongoing room; web_rid is the reusable public URL.
                    rid = str(user.get('web_rid') or rid)
                    room_data = _object(user.get('room_data') or {})
                    owner = room_data.get('owner') or {}
                    rid = str(owner.get('web_rid') or rid)
                    # Profile room_id can be 0 during a live broadcast; the public
                    # Douyin handle is also a valid web/enter lookup identity.
                    if not rid and re.fullmatch(WEB_RID_PATTERN, str(user.get('unique_id') or '')):
                        rid = user['unique_id']
                    if not rid:
                        text, _ = self._get(session, 'https://www.douyin.com/user/' + sec_uid, credential)
                        match = re.search(r'"web_?rid"\s*:\s*"?(\d+)', text.replace('\\"', '"'), re.I)
                        rid = match.group(1) if match else ''
                if not re.fullmatch(WEB_RID_PATTERN, rid):
                    raise _ResponseError('parse_error')
                payload = self._api(session, 'https://live.douyin.com/webcast/room/web/enter/',
                    {'aid': '6383', 'app_name': 'douyin_web', 'live_id': '1', 'device_platform': 'web',
                     'language': 'zh-CN', 'browser_language': 'zh-CN', 'browser_platform': 'Win32',
                     'browser_name': 'Chrome', 'browser_version': '116.0.0.0', 'web_rid': rid,
                     'is_need_double_stream': 'false', 'msToken': ''}, credential)
                value = parse_room(payload, rid)
                if sec_uid and value.sec_uid and value.sec_uid != sec_uid:
                    raise _ResponseError('parse_error')
                return replace(value, sec_uid=value.sec_uid or sec_uid, name=value.name or name,
                               received_bytes=session._lulu_received)
        except _ResponseError as exc:
            state = exc.state
        except requests.RequestException:
            state = 'network_error'
        except (ValueError, KeyError, TypeError, AttributeError):
            state = 'parse_error'
        return Observation(state, sec_uid, rid, name, received_bytes=getattr(session, '_lulu_received', 0))
