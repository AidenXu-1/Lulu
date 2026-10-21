"""Bounded Douyin comment connection, independently owned by a recording.

WebSocket/signature reference: biliup/biliup f81fd01a90e2f2830f19be5b031351fca7282925,
crates/danmaku/src/protocols/douyin.rs, MIT (BILIUP_LICENSE.txt).
Modified: pure Python, fresh visitor cookies, verified TLS, bounded decoding,
cancellation, message-ID deduplication and local reception timestamps.
"""
import base64
from collections import deque
import gzip
import hashlib
import io
import os
import ssl
import threading
import time
from urllib.parse import urlencode

import requests
from requests.certs import where as certificate_file

from .comments import CommentLog, STATES, status_text, timestamp
from .contracts import LiveError

HOSTS = ('webcast100-ws-web-hl.douyin.com', 'webcast100-ws-web-lq.douyin.com',
         'webcast100-ws-web-lf.douyin.com')
MAX_PAYLOAD = 4 * 1024**2
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')

class CommentConnectionError(Exception):
    """Deliberately excludes signed URLs, cookies and raw remote exceptions."""

def varint(data, offset):
    result = shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        result |= (byte & 127) << shift
        if not byte & 128:
            return result, offset
        shift += 7
    raise ValueError('invalid varint')

def protobuf(data):
    if not isinstance(data, bytes) or len(data) > MAX_PAYLOAD:
        raise ValueError('invalid message length')
    fields, offset = {}, 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        key, wire = tag >> 3, tag & 7
        if not key:
            raise ValueError('invalid field')
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 5, 2):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise ValueError('invalid field length')
            value, offset = data[offset:offset + size], offset + size
        else:
            raise ValueError('unsupported field')
        fields.setdefault(key, []).append(value)
    return fields

def first(fields, key, default=None):
    return fields.get(key, [default])[0]

def encode_number(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)

def encode_field(key, value):
    if isinstance(value, int):
        return encode_number(key << 3) + encode_number(value)
    return encode_number(key << 3 | 2) + encode_number(len(value)) + value

def rc4(key, data):
    state, j = list(range(256)), 0
    for i in range(256):
        j = (j + state[i] + key) % 256
        state[i], state[j] = state[j], state[i]
    result, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        result.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(result)

def signature(params):
    stub = hashlib.md5(','.join(key + '=' + value for key, value in params).encode()).digest()
    last = hashlib.md5(stub).digest()[-2:]
    random1, random2 = os.urandom(2)
    random2 = random2 * 255 // 256
    payload = bytearray([1, 0, 1, 14, 69, 63, *last, random2, 0])
    for value in payload[:9]:
        payload[9] ^= value
    raw = bytes([64 | (random1 & 31), random2]) + rc4(random2, payload)
    alphabet = b'Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe'
    return base64.b64encode(raw).translate(bytes.maketrans(
        b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', alphabet)).decode()

def connection_url(room_id, host_index):
    if not room_id.isdigit() or not 1 <= len(room_id) <= 20:
        raise CommentConnectionError()
    unique = str(7300000000000000000 + int.from_bytes(os.urandom(8)) % 699999999999999999)
    keys = [('live_id', '1'), ('aid', '6383'), ('version_code', '180800'),
            ('webcast_sdk_version', '1.0.15'), ('room_id', room_id), ('sub_room_id', ''),
            ('sub_channel_id', ''), ('did_rule', '3'), ('user_unique_id', unique),
            ('device_platform', 'web'), ('device_type', ''), ('ac', ''), ('identity', 'audience')]
    # Empty fields belong to the signature input; the verified connection URL
    # omits them, matching the independent live probe.
    params = {key: value for key, value in keys if value}
    params.update(app_name='douyin_web', compress='gzip', browser_language='zh-CN',
                  browser_platform='Win32', browser_name='Mozilla', browser_version='120.0.0.0',
                  enter_from='web_live', update_version_code='1.0.15',
                  host='https://live.douyin.com', endpoint='live_pc',
                  need_persist_msg_count='15', heartbeatDuration='0', signature=signature(keys))
    return 'wss://' + HOSTS[host_index % len(HOSTS)] + '/webcast/im/push/v2/?' + urlencode(params)

def decode_frame(raw):
    frame = protobuf(raw)
    payload = first(frame, 8, b'')
    if not payload:
        return [], None
    if payload.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
            payload = compressed.read(MAX_PAYLOAD + 1)
    response = protobuf(payload)
    ack = (encode_field(2, first(frame, 2, 0)) + encode_field(7, b'ack') +
           encode_field(8, first(response, 5, b''))) if first(response, 9, 0) else None
    chats = []
    for chunk in response.get(1, []):
        message = protobuf(chunk)
        if first(message, 1) != b'WebcastChatMessage':
            continue
        chat = protobuf(first(message, 2, b''))
        common = protobuf(first(chat, 1, b''))
        user = protobuf(first(chat, 2, b''))
        content = first(chat, 3, b'').decode('utf-8')
        nickname = first(user, 3, b'').decode('utf-8')
        if content and len(content) <= 20000 and len(nickname) <= 2000:
            chats.append({'nickname': nickname, 'text': content,
                          'message_id': str(first(message, 3) or first(common, 2) or '')})
    return chats, ack

class DouyinCommentConnection:
    def __init__(self, room_id, credential='', host_index=0):
        self.room_id, self.credential, self.host_index = room_id, credential, host_index
        self.socket = None

    def connect(self, cancel):
        import websocket  # Already supplied by the existing dashscope dependency.
        if cancel.is_set():
            raise CommentConnectionError()
        cookie = self.credential
        if not cookie:
            with requests.Session() as session:
                session.trust_env = False
                with session.get('https://live.douyin.com/', headers={'User-Agent': UA},
                                 timeout=(3, 5), stream=True, allow_redirects=False, verify=True) as response:
                    if response.status_code != 200:
                        raise CommentConnectionError()
                visitor = next((c.value for c in session.cookies if c.name == 'ttwid'
                                and c.domain.lstrip('.') == 'douyin.com'), '')
                if not visitor and not cancel.is_set():
                    with session.post('https://ttwid.bytedance.com/ttwid/union/register/',
                            headers={'User-Agent': UA}, json={'region': 'cn', 'aid': 6383, 'needFid': False,
                            'service': 'www.douyin.com', 'migrate_info': {'ticket': '', 'source': 'node'},
                            'cbUrlProtocol': 'https', 'union': True}, timeout=(3, 5), stream=True,
                            allow_redirects=False, verify=True) as response:
                        if response.status_code != 200:
                            raise CommentConnectionError()
                    visitor = next((c.value for c in session.cookies if c.name == 'ttwid'
                                    and c.domain.lstrip('.') == 'bytedance.com'), '')
                if not visitor:
                    raise CommentConnectionError()
                cookie = 'ttwid=' + visitor
        if cancel.is_set():
            raise CommentConnectionError()
        self.socket = websocket.create_connection(connection_url(self.room_id, self.host_index),
            timeout=5, cookie=cookie, origin='https://live.douyin.com',
            header=['User-Agent: ' + UA, 'Referer: https://live.douyin.com/'],
            sslopt={'cert_reqs': ssl.CERT_REQUIRED, 'ca_certs': certificate_file(), 'check_hostname': True},
            http_proxy_host=None, http_no_proxy=['*'])
        self.socket.settimeout(1)
        if cancel.is_set():
            self.close()
            raise CommentConnectionError()

    def receive(self):
        import websocket
        try:
            raw = self.socket.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if not raw:
            raise CommentConnectionError()
        if isinstance(raw, str):
            raw = raw.encode()
        chats, ack = decode_frame(raw)
        if ack:
            self.socket.send_binary(ack)
        return chats

    def heartbeat(self):
        self.socket.send_binary(encode_field(7, b'hb'))

    def abort(self):
        if self.socket:
            self.socket.abort()

    def close(self):
        if self.socket:
            # Shutdown is immediate; no multi-second close handshake on cancellation.
            self.socket.shutdown()

class CommentRecorder:
    def __init__(self, recording, room_id, credential='', *, connection_factory=DouyinCommentConnection,
                 retry_delays=(3, 10, 30, 60), origin=None):
        self.recording, self.room_id, self.credential = recording, room_id, credential
        self.connection_factory, self.retry_delays = connection_factory, retry_delays
        self.origin = time.monotonic() if origin is None else origin
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.connection = None
        self.state, self.count = 'connecting', 0
        self.stop_reason = 'ended'
        self.log = CommentLog(recording)

    def start(self):
        self.thread = threading.Thread(target=self._run, name='lulu-live-comments', daemon=True)
        self.thread.start()

    def request_stop(self, reason='ended'):
        self.stop_reason = reason
        self.cancel.set()
        with self.lock:
            connection = self.connection
        if connection:
            try:
                connection.abort()
            except Exception:
                pass  # A bounded receive timeout is the fallback.

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self):
        with self.lock:
            return {'comments_state': self.state, 'comments_count': self.count,
                    'comments_message': status_text(self.state, self.count)}

    def _append(self, entries):
        try:
            self.log.append(entries)
            return True
        except (OSError, ValueError) as exc:
            with self.lock:
                self.state = 'limit' if isinstance(exc, LiveError) and str(exc) == STATES['limit'] else 'write_error'
            return False

    def _state(self, state):
        if self.state == state and state != 'connecting':
            return True
        if not self._append([{'kind': 'status', 'state': state, 'received_at': timestamp()}]):
            return False
        with self.lock:
            self.state = state
        return True

    def _run(self):
        seen, order = set(), deque()
        attempt = 0
        connected_once = False
        try:
            if not self.room_id:
                self._state('unavailable')
                return
            while not self.cancel.is_set():
                if not self._state('connecting' if attempt == 0 else 'reconnecting' if connected_once else 'retrying'):
                    return
                connection = self.connection_factory(self.room_id, self.credential, attempt)
                with self.lock:
                    self.connection = connection
                try:
                    connection.connect(self.cancel)
                    if self.cancel.is_set():
                        break
                    connected_once = True
                    if not self._state('recording' if self.count else 'waiting'):
                        return
                    last_frame = last_heartbeat = time.monotonic()
                    while not self.cancel.is_set():
                        now = time.monotonic()
                        if now - last_heartbeat >= 10:
                            connection.heartbeat()
                            last_heartbeat = now
                        chats = connection.receive()
                        if chats is None:
                            if now - last_frame >= 30:
                                raise CommentConnectionError()
                            continue
                        last_frame = time.monotonic()
                        received, elapsed = timestamp(), max(0, time.monotonic() - self.origin)
                        pending, ids = [], []
                        for chat in chats:
                            mid = chat['message_id']
                            if mid and (mid in seen or mid in ids):
                                continue
                            if mid:
                                ids.append(mid)
                            pending.append({**chat, 'kind': 'chat', 'received_at': received,
                                            'elapsed_seconds': round(elapsed, 3)})
                        # Cancellation fences queued messages before admission to disk.
                        if self.cancel.is_set():
                            break
                        if pending:
                            if not self._append(pending):
                                return
                            for mid in ids:
                                if len(order) >= 20000:
                                    seen.discard(order.popleft())
                                seen.add(mid)
                                order.append(mid)
                            with self.lock:
                                self.count += len(pending)
                            if self.state == 'waiting' and not self._state('recording'):
                                return
                except Exception:
                    if not self.cancel.is_set() and not self._state('reconnecting' if connected_once else 'retrying'):
                        return
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    with self.lock:
                        self.connection = None
                if self.cancel.wait(self.retry_delays[min(attempt, len(self.retry_delays) - 1)]):
                    break
                attempt += 1
        except Exception:
            # An unexpected recorder error must never enter the video failure path.
            with self.lock:
                self.state = 'interrupted'
        finally:
            self.credential = ''
            if self.cancel.is_set() and self.state not in {'write_error', 'limit', 'unavailable'}:
                self._state(self.stop_reason)
