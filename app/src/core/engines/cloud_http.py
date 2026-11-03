"""Structured cloud failures and bounded-memory audio uploads. No automatic replay."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import base64
import json
import math
from pathlib import Path
import re
from uuid import uuid4


class CloudFailure(RuntimeError):
    MESSAGES = {
        'rate_limited':'服务暂时限制请求，尚未受理，稍后按额度继续',
        'account':'服务账户、权限或余额需要核对，本批已停止后续发送',
        'rejected':'服务拒绝此音频，请核对格式、文件大小或服务开通情况',
        'unknown':'请求结果未确认，可能已计费；普通恢复不会再次上传',
    }
    def __init__(self, kind, *, status=0, code='', request_id='', retry_after=0):
        self.kind = kind if kind in self.MESSAGES else 'unknown'
        self.status = int(status) if isinstance(status, int) and not isinstance(status, bool) else 0
        self.service_code = safe_id(code)
        self.request_id = safe_id(request_id)
        delay = float(retry_after or 0)
        self.retry_after = max(0.,delay) if math.isfinite(delay) else 0.
        message = self.MESSAGES[self.kind]
        if self.kind == 'unknown' and self.service_code == 'EMPTY_TRANSCRIPT':
            message = '服务已返回，但没有取得有效文字；本次可能已计费，未自动重传'
        elif self.kind == 'unknown' and self.service_code == 'SDK_EXCEPTION':
            message = '识别过程发生异常，结果尚未确认；本次可能已计费，未自动重传'
        super().__init__(message)

    def public(self):
        return dict(kind=self.kind, http_status=self.status, service_code=self.service_code,
                    request_id=self.request_id, retry_after=self.retry_after)


def safe_id(value):
    text = str(value or '')
    return text[:160] if re.fullmatch(r'[a-zA-Z0-9_.:/-]{0,160}', text) else ''


def retry_after(value):
    try:
        seconds = float(value)
        return seconds if 0 <= seconds <= 7*24*3600 else 0.
    except (ValueError, TypeError):
        try:
            return max(0., (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.


def failure(status, data=None, headers=None):
    headers = headers or {}
    error = (data or {}).get('error', data or {}) if type(data) is dict else {}
    if type(error) is not dict:
        error = {}
    code = error.get('code') or headers.get('X-Api-Status-Code', '')
    message = str(error.get('message') or headers.get('X-Api-Message', ''))[:10000].lower()
    combined = (str(code)+' '+message).lower()
    rid = headers.get('X-Request-Id') or headers.get('x-request-id') or headers.get('X-Tt-Logid') or (data or {}).get('request_id', '')
    if status in {401,403} or any(s in combined for s in ('insufficient_quota','credit_balance','spend_limit','usage_limit','balance','arrear')):
        kind = 'account'
    elif status == 429:
        kind = 'rate_limited'
    elif 400 <= status < 500 or str(code).startswith('450'):
        kind = 'rejected'
    else:
        kind = 'unknown'
    return CloudFailure(kind, status=status, code=code, request_id=rid,
                        retry_after=retry_after(headers.get('Retry-After') or headers.get('retry-after')))


class UploadBody:
    """requests/http.client reads chunks; no fp.read() of the entire media file."""
    def __init__(self, path, prefix, suffix, *, encoded=False):
        self.file = Path(path).open('rb')
        self.prefix, self.suffix, self.encoded = prefix, suffix, encoded
        self.pending = b''
        self.phase = 0
        size = Path(path).stat().st_size
        self.length = len(prefix)+len(suffix)+(4*((size+2)//3) if encoded else size)

    def __len__(self):
        return self.length

    def read(self, size=65536):
        # A client asking for "all" still receives a bounded chunk.
        size = max(1, min(size if size and size > 0 else 65536, 65536))
        while len(self.pending) < size and self.phase < 3:
            if self.phase == 0:
                self.pending += self.prefix
                self.phase = 1
            elif self.phase == 1:
                chunk = self.file.read(49152 if self.encoded else 65536)
                if chunk:
                    self.pending += base64.b64encode(chunk) if self.encoded else chunk
                else:
                    self.phase = 2
            else:
                self.pending += self.suffix
                self.phase = 3
        value, self.pending = self.pending[:size], self.pending[size:]
        return value

    def close(self):
        self.file.close()


def multipart(path, fields):
    boundary = 'lulu-'+uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    body = UploadBody(path, b''.join(chunks), f'\r\n--{boundary}--\r\n'.encode())
    return body, 'multipart/form-data; boundary='+boundary


def volcano_body(path):
    # Base64 is streamed and contains no account credential in the JSON body.
    prefix = b'{"user":{"uid":"lulu"},"audio":{"format":"wav","rate":16000,"bits":16,"channel":1,"data":"'
    suffix = b'"},"request":{"model_name":"bigmodel","enable_punc":true,"show_utterances":true}}'
    return UploadBody(path, prefix, suffix, encoded=True)


def post_audio(url, headers, body, *, timeout=300):
    import requests
    headers = dict(headers, **{'Content-Length':str(len(body))})
    try:
        with requests.post(url, headers=headers, data=body, timeout=(15, timeout),
                           stream=True, allow_redirects=False) as response:
            payload = bytearray()
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > 16*1024*1024:
                    raise CloudFailure('unknown', status=response.status_code)
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeError):
                raise CloudFailure('unknown', status=response.status_code) from None
            if type(data) is not dict:
                raise CloudFailure('unknown', status=response.status_code)
            if response.status_code != 200:
                raise failure(response.status_code, data, response.headers)
            return data, requests.structures.CaseInsensitiveDict(response.headers)
    except CloudFailure:
        raise
    except requests.RequestException:
        raise CloudFailure('unknown') from None
    finally:
        body.close()
