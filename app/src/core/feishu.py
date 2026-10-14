"""存入飞书知识库(Wiki):把文稿 Markdown 经开放平台 API 导入成飞书文档,落到指定知识库节点。

鉴权走 **OAuth 登录授权(user_access_token)**——以用户本人身份操作,借用其权限,
故能写自己的知识库(含飞书个人版 my.feishu.cn),无需把应用加进知识库做成员。
链路:浏览器登录授权拿 code → 换 user_access_token(+refresh_token)→ 上传 md
     (medias/upload_all)→ import_tasks 转 docx → 轮询 → move_docs_to_wiki 入库。

- app_id/app_secret + refresh_token 存系统 keyring 槽 "feishu_wiki";access_token 仅内存。
- 目标位置(space_id/node)存 config.json(非敏感)。
- ⚠️ 用户显式选择的云端能力:触发会把文稿上传到所配置的飞书知识库(同 ADR-0007 红线)。
- 应用需在飞书后台开通 OAuth、加重定向 URL(本地 127.0.0.1:8910~8912/callback)、配 drive/docx/wiki 权限。

注:请求组装做成可单测(mock requests);真实跑通需用户登录授权后端到端验证(见 docs/spec/feishu-wiki.md)。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Callable, Iterable
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4
import zlib

from . import obsidian, secrets

CONFIG_KEY = "feishu_wiki"
_BASE = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = f"{_BASE}/authen/v2/oauth/token"
# 本机回调端口:按序试,任一空闲即可(SOP 让用户把这几个回调都登记上)
OAUTH_PORTS = (8910, 8911, 8912)
# 申请的用户身份权限(够把文档导入知识库 + 建多维表格);offline_access 用于拿 refresh_token
SCOPES = "offline_access drive:drive docx:document wiki:wiki bitable:app"


def redirect_uri(port: int = OAUTH_PORTS[0]) -> str:
    return f"http://127.0.0.1:{port}/callback"


REDIRECT_URI = redirect_uri()  # 默认/展示用(8910);实际登录用所选空闲端口

DEV_CONSOLE_URL = "https://open.feishu.cn/app"
# Lulu 仅申请手动保存文档、表格和选择存放位置所需权限。
SCOPE_BUNDLE = SCOPES.split()


def app_auth_url(app_id: str) -> str:
    """开发者后台该应用的『待开通权限』页(自动勾选 SCOPE_BUNDLE)。"""
    import urllib.parse
    q = urllib.parse.quote(",".join(SCOPE_BUNDLE))
    return f"{DEV_CONSOLE_URL}/{app_id}/auth?q={q}"


def safe_url(app_id: str) -> str:
    """开发者后台该应用的『安全设置』页(登记重定向 URL 的地方)。"""
    return f"{DEV_CONSOLE_URL}/{app_id}/safe"

# user_access_token 内存缓存;refresh_token 落 keyring
_user_cache = {"token": "", "exp": 0.0}
_auth_lock = threading.RLock()
_user_sessions = threading.local()


def _credential_binding(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


@contextmanager
def user_session():
    """Pin the authenticated identity for the entire operation, including retries.

    A concurrent login can change the next operation, never this one's uploads.
    Expiry fails normally instead of silently switching to another account.
    """
    if getattr(_user_sessions, 'token', None) is not None:
        yield
        return
    _user_sessions.token = _user_token()
    try:
        yield
    finally:
        del _user_sessions.token


def save_app_credentials(app_id, app_secret):
    with _auth_lock:
        value = secrets.get_config(CONFIG_KEY, require_access=True)
        changed = (value.get('app_id'), value.get('app_secret')) != (app_id, app_secret)
        value.update(app_id=app_id, app_secret=app_secret)
        if changed:
            value.pop('refresh_token', None)
            _user_cache.clear()
            _user_cache.update(token='', exp=0.0)
        secrets.set_key_verified(CONFIG_KEY, json.dumps(value))
        return value


# ----------------------------- 凭据 / 登录授权 ----------------------------- #
def _cfg() -> dict:
    c = secrets.get_config(CONFIG_KEY)
    return {
        "app_id": (c.get("app_id") or "").strip(),
        "app_secret": (c.get("app_secret") or "").strip(),
        "refresh_token": (c.get("refresh_token") or "").strip(),
    }


def is_configured() -> bool:
    """已填应用凭据(换 token 用)。"""
    c = _cfg()
    return bool(c["app_id"] and c["app_secret"])


def is_authorized() -> bool:
    """已完成登录授权(有 refresh_token)。"""
    return bool(_cfg()["refresh_token"])


def _json(r):
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"飞书返回异常(HTTP {getattr(r, 'status_code', 'unknown')})。"
        ) from exc


class FeishuApiError(RuntimeError):
    """保留可机械判断的错误码，同时只向 UI 暴露脱敏说明。"""

    def __init__(self, code: object, message: str, *, retry_after: float = 0.0):
        self.code = code
        self.retry_after = max(0.0, float(retry_after or 0.0))
        super().__init__(message)


class FeishuTransportError(RuntimeError):
    def __init__(
        self,
        *,
        retryable_not_sent: bool,
        uncertain: bool,
        message: str = "连接飞书失败，请检查网络后再试。",
        stage: str = "",
        known_url: str = "",
    ):
        self.retryable_not_sent = retryable_not_sent
        self.uncertain = uncertain
        self.stage = stage
        self.known_url = known_url
        super().__init__(message)


def _reject_post_redirect(response) -> None:
    """POST 已经到达远端时拒绝任何 3xx，且不得自动重放请求体。"""

    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if 300 <= status_code < 400:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书返回了重定向响应，已停止处理且不会自动重试。",
        )


def _check(data: dict) -> dict:
    """飞书统一响应:code==0 才算成功,否则给可读中文。返回 data 字段。"""
    code = data.get("code")
    if code == 0:
        return data.get("data", {}) or {}
    msg = data.get("msg") or "未知错误"
    if code in (99991663, 99991661, 99991664):
        raise FeishuApiError(
            code,
            f"飞书鉴权失败(code {code})。请核对 app_id / app_secret。",
        )
    if code in (1254302, 1254040, 99991672) or "permission" in msg.lower() or "scope" in msg.lower():
        raise FeishuApiError(
            code,
            f"飞书权限不足(code {code})。请确认应用已开通现有权限并重新「登录授权」。",
        )
    raise FeishuApiError(code, f"飞书接口报错(code {code})。")


def authorize_url(state: str, redirect: str = REDIRECT_URI) -> str:
    """构造登录授权 URL(浏览器打开,用户登录并同意)。"""
    import urllib.parse
    q = urllib.parse.urlencode({
        "client_id": _cfg()["app_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{q}"


def _oauth_token(body: dict, timeout: int = 20) -> dict:
    """调 v2 oauth/token(标准 OAuth2 返回,成功带 access_token)。"""
    import requests
    try:
        r = requests.post(
            TOKEN_URL,
            json=body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"连接飞书失败(检查网络 / 代理):{exc}") from exc
    _reject_post_redirect(r)
    data = _json(r)
    if not data.get("access_token"):
        err = (data.get("error_description") or data.get("msg")
               or data.get("error") or "未知错误")
        raise RuntimeError(f"飞书授权失败:{err}")
    return data


def _store(data: dict) -> None:
    """access_token 存内存,refresh_token 落 keyring(保留 app 凭据)。"""
    with _auth_lock:
        rt = data.get("refresh_token")
        if rt:
            c = secrets.get_config(CONFIG_KEY)
            c["refresh_token"] = rt
            secrets.set_config(CONFIG_KEY, c)
        _user_cache.update(token=data["access_token"],
                           exp=time.time() + float(data.get("expires_in", 7200)),
                           binding=_credential_binding(_cfg()))


def exchange_code(code: str, redirect: str = REDIRECT_URI, *, cancelled=lambda: False) -> dict:
    """用授权码换 user_access_token + refresh_token。redirect 必须与授权时一致。"""
    with _auth_lock:
        c = _cfg()
    data = _oauth_token({
        "grant_type": "authorization_code",
        "client_id": c["app_id"],
        "client_secret": c["app_secret"],
        "code": code,
        "redirect_uri": redirect,
    })
    with _auth_lock:
        if cancelled() or c != _cfg():
            raise ValueError('本次连接已停止，未保存新的授权。')
        _store(data)
    return data


def logout() -> None:
    with _auth_lock:
        _user_cache.update(token="", exp=0.0)
        c = secrets.get_config(CONFIG_KEY)
        c.pop("refresh_token", None)
        secrets.set_config(CONFIG_KEY, c)


def _refresh() -> str:
    c = _cfg()
    if not c["refresh_token"]:
        raise RuntimeError("还没登录授权飞书。请到「集成 → 飞书知识库」点「登录授权」。")
    data = _oauth_token({
        "grant_type": "refresh_token",
        "client_id": c["app_id"],
        "client_secret": c["app_secret"],
        "refresh_token": c["refresh_token"],
    })
    _store(data)
    return data["access_token"]


def _user_token() -> str:
    """拿可用的 user_access_token(过期则用 refresh_token 续);未登录则报错。"""
    pinned = getattr(_user_sessions, 'token', None)
    if pinned is not None:
        return pinned
    with _auth_lock:
        if not is_configured():
            raise RuntimeError("还没填飞书应用凭据(App ID / Secret)。请到「集成 → 飞书知识库」里填。")
        if (_user_cache["token"] and _user_cache["exp"] - 60 > time.time()
                and _user_cache.get('binding') == _credential_binding(_cfg())):
            return _user_cache["token"]
        _user_cache.update(token='', exp=0.0)
        return _refresh()


def _request_not_sent(exc: BaseException, requests_module) -> bool:
    if isinstance(exc, requests_module.exceptions.ConnectTimeout):
        return True
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {"NameResolutionError", "NewConnectionError"}:
            return True
        current = current.__cause__ or current.__context__
    return False


_api_connections = threading.local()


@contextmanager
def api_session():
    """Reuse connections only within this worker's save; never share across threads."""
    if getattr(_api_connections, 'session', None) is not None:
        yield
        return
    import requests
    session = requests.Session()
    _api_connections.session = session
    try:
        yield
    finally:
        _api_connections.session = None
        session.close()


def _api(method: str, path: str, token: str, timeout: int = 30, **kw) -> dict:
    """通用 JSON API 调用 + 错误码→中文。返回 data 字段。"""
    import requests

    method_upper = method.upper()
    request_options = dict(kw)
    if method_upper == "POST":
        request_options["allow_redirects"] = False
    try:
        session = getattr(_api_connections, "session", None)
        request = session.request if session is not None else requests.request
        r = request(
            method, f"{_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout, **request_options,
        )
    except requests.RequestException as exc:
        not_sent = _request_not_sent(exc, requests)
        raise FeishuTransportError(
            retryable_not_sent=not_sent,
            uncertain=method_upper != "GET" and not not_sent,
        ) from exc
    if method_upper == "POST":
        _reject_post_redirect(r)
    try:
        raw = _json(r)
    except RuntimeError as exc:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=method_upper != "GET",
        ) from exc
    if not isinstance(raw, dict) or "code" not in raw:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=method_upper != "GET",
        )
    return _check(raw)


# ----------------------------- 公开能力 ----------------------------- #
def test_connection() -> str:
    spaces = list_spaces()
    return f"连接成功 ✓ 可访问 {len(spaces)} 个知识库"


def list_spaces() -> list[dict]:
    """列出应用有权限的知识库(做下拉)。返回 [{space_id, name}]。"""
    data = _api("GET", "/wiki/v2/spaces?page_size=50", _user_token())
    out = []
    for it in data.get("items", []) or []:
        out.append({"space_id": it.get("space_id"), "name": it.get("name") or "(未命名知识库)"})
    return out


def _extract_wiki_token(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"/wiki/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", s):
        return s
    return ""


def resolve_node(url_or_token: str) -> dict:
    """把粘贴的知识库页面 URL/token 解析成 {space_id, node_token, title}。"""
    tok = _extract_wiki_token(url_or_token)
    if not tok:
        raise RuntimeError("没认出知识库页面链接。请粘贴形如 https://xxx.feishu.cn/wiki/XXXX 的地址。")
    data = _api("GET", f"/wiki/v2/spaces/get_node?token={tok}&obj_type=wiki", _user_token())
    node = data.get("node", {}) or {}
    if not node.get("space_id"):
        raise RuntimeError("没解析到知识库节点(应用可能没有这个知识库的权限)。")
    return {
        "space_id": node.get("space_id"),
        "node_token": node.get("node_token"),
        "title": node.get("title") or "",
    }


# ----------------------------- Markdown 处理 ----------------------------- #
def strip_frontmatter(md: str) -> str:
    """去掉 Obsidian 风格的 YAML frontmatter(飞书导入不识别,留着会乱排版)。"""
    s = (md or "").lstrip("﻿").lstrip()
    if not s.startswith("---"):
        return md
    lines = s.split("\n")
    if lines[0].strip() != "---":
        return md
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return md  # 没有闭合,原样返回


def build_doc_markdown(transcript, title: str, summary: str | None = None) -> str:
    """复用 obsidian 的正文生成,但去掉 frontmatter,得到适合飞书导入的干净 Markdown。"""
    return strip_frontmatter(obsidian.build_note(transcript, title, summary))


# ----------------------------- 上传 → 导入 → 入库 ----------------------------- #
def save_markdown(md_text: str, title: str, space_id: str, parent_node: str = "") -> str:
    """把 Markdown 文稿存进飞书知识库,返回新建文档的飞书链接。

    md_text 可来自右栏已编辑文本(会先 strip_frontmatter)。space_id 必填;
    parent_node 为空 = 落到知识库根。
    """
    if not space_id:
        raise RuntimeError("还没选择目标知识库。请到「集成 → 飞书知识库」里选一个知识库。")
    token = _user_token()
    md = strip_frontmatter(md_text).strip() or "(空文稿)"
    fname = obsidian._safe_name(title) + ".md"
    file_token = _upload_md(token, fname, md)
    docx_token = _import_to_docx(token, file_token, obsidian._safe_name(title))
    return _move_to_wiki(token, space_id, parent_node, docx_token)


def _upload_md(token: str, fname: str, md: str) -> str:
    import requests

    blob = md.encode("utf-8")
    files = {
        "file_name": (None, fname),
        "parent_type": (None, "ccm_import_open"),
        "size": (None, str(len(blob))),
        "extra": (None, json.dumps({"obj_type": "docx", "file_extension": "md"})),
        "file": (fname, blob, "text/markdown"),
    }
    try:
        r = requests.post(
            f"{_BASE}/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            timeout=120,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"上传到飞书失败(检查网络 / 代理):{exc}") from exc
    _reject_post_redirect(r)
    data = _check(_json(r))
    ft = data.get("file_token")
    if not ft:
        raise RuntimeError("飞书上传未返回 file_token。")
    return ft


def _root_folder(token: str) -> str:
    """导入中转用的 Drive 落点(我的空间根目录 token)。"""
    data = _api("GET", "/drive/explorer/v2/root_folder/meta", token)
    return data.get("token", "")


def _import_to_docx(token: str, file_token: str, title: str) -> str:
    body = {
        "file_extension": "md",
        "file_token": file_token,
        "type": "docx",
        "file_name": title,
        "point": {"mount_type": 1, "mount_key": _root_folder(token)},
    }
    data = _api("POST", "/drive/v1/import_tasks", token, json=body)
    ticket = data.get("ticket")
    if not ticket:
        raise RuntimeError("飞书导入未返回 ticket。")
    return _poll_import(token, ticket)


def _poll_import(token: str, ticket: str, tries: int = 30, interval: float = 1.0) -> str:
    for _ in range(tries):
        data = _api("GET", f"/drive/v1/import_tasks/{ticket}", token)
        result = data.get("result", {}) or {}
        status = result.get("job_status")
        if status == 0:  # 成功
            tok = result.get("token")
            if not tok:
                raise RuntimeError("飞书导入成功但未返回文档 token。")
            return tok
        if status in (1, 2):  # 处理中
            time.sleep(interval)
            continue
        raise RuntimeError(f"飞书导入失败:{result.get('job_error_msg') or ('状态 ' + str(status))}")
    raise RuntimeError("飞书导入超时,请稍后重试。")


def _move_to_wiki(token: str, space_id: str, parent_node: str, docx_token: str) -> str:
    body = {"obj_type": "docx", "obj_token": docx_token}
    if parent_node:
        body["parent_wiki_token"] = parent_node
    data = _api("POST", f"/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki", token, json=body)
    nt = (data.get("node") or {}).get("node_token")  # 少数同步返回
    if not nt and data.get("task_id"):
        nt = _poll_move(token, data["task_id"])       # 异步任务:轮询确认并取新节点
    return f"https://feishu.cn/wiki/{nt}" if nt else "已存入飞书知识库"


def _poll_move(token: str, task_id: str, tries: int = 15, interval: float = 1.0) -> str:
    """轮询 move 异步任务,成功后返回文档在知识库里的新 node_token。"""
    for _ in range(tries):
        data = _api("GET", f"/wiki/v2/tasks/{task_id}?task_type=move", token)
        results = (data.get("task") or {}).get("move_result") or []
        if results:
            return ((results[0] or {}).get("node") or {}).get("node_token", "")
        time.sleep(interval)
    return ""  # 超时未确认(任务已提交,通常仍会完成)


# ----------------------------- 多维表格(Bitable)----------------------------- #
# 飞书 bitable 字段类型码:1=多行文本 2=数字 5=日期 15=超链接 17=附件
BT_TEXT, BT_NUMBER, BT_DATETIME, BT_URL, BT_ATTACHMENT = 1, 2, 5, 15, 17
# 当前 Base 批量写入合同的单批上限是 200。宁可多一个批次，也不要让大批量
# 在飞书入口直接被拒绝；同一目标仍会按顺序提交。
_BT_BATCH = 200
_BITABLE_MEDIA_PATH = "/drive/v1/medias/upload_all"
_BITABLE_MEDIA_MAX_BYTES = 20 * 1024 * 1024
_BITABLE_MEDIA_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_BITABLE_FILE_UPLOAD_ALL_MAX = 20_971_520
_BITABLE_FILE_MAX_BYTES = 2_000_000_000
_BITABLE_FILE_BLOCK_SIZE = 4_194_304
_BITABLE_MEDIA_MIN_INTERVAL = 0.25
_BITABLE_MEDIA_RETRY_CODES = frozenset({1061045, 1061006, 99991400})
_BITABLE_MEDIA_SAFE_RESTART_REASONS = frozenset({
    "media_prepare_uncertain",
    "media_part_uncertain",
    "media_connection_failed",
    "media_rate_limited",
    "media_upload_expired",
})
_bitable_media_lock = threading.Lock()
_bitable_media_last_start = 0.0
_bitable_table_locks_guard = threading.Lock()
_bitable_table_locks: dict[tuple[str, str], threading.RLock] = {}


def _bitable_table_write_lock(app_token: str, table_id: str) -> threading.RLock:
    """同一张表串行写，不同作品库可并行。

    附件仍走全局 4 QPS 门；这里只隔离结构和记录写入，避免一个大批次把所有
    其他分类作品库一起堵住。
    """

    key = (str(app_token or ""), str(table_id or ""))
    with _bitable_table_locks_guard:
        return _bitable_table_locks.setdefault(key, threading.RLock())


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_private_receipt_root(
    root: Path,
) -> tuple[int, list[int], tuple[tuple[int, str, int, int], ...]]:
    """创建并钉住私有收据目录，保留每层父目录用于换链复核。"""

    absolute = Path(os.path.abspath(root))
    if not absolute.is_absolute() or absolute == Path(absolute.anchor):
        raise OSError("音视频上传收据目录不安全")
    trusted_candidates = (Path(tempfile.gettempdir()), Path.home())
    applicable: list[Path] = []
    for candidate in trusted_candidates:
        try:
            absolute.relative_to(candidate)
        except ValueError:
            continue
        applicable.append(candidate)
    trusted = (
        max(applicable, key=lambda value: len(value.parts))
        if applicable
        else Path(absolute.anchor)
    )
    relative = absolute.relative_to(trusted)
    if not relative.parts:
        raise OSError("音视频上传收据目录不安全")

    descriptors: list[int] = []
    bindings: list[tuple[int, str, int, int]] = []
    try:
        current = os.open(trusted, _directory_open_flags())
        descriptors.append(current)
        for component in relative.parts:
            try:
                before = os.stat(component, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                before = os.stat(component, dir_fd=current, follow_symlinks=False)
            child = os.open(component, _directory_open_flags(), dir_fd=current)
            descriptors.append(child)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("音视频上传收据目录不安全")
            bindings.append((current, component, opened.st_dev, opened.st_ino))
            current = child
        os.fchmod(current, 0o700)
        return current, descriptors, tuple(bindings)
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _receipt_root_unchanged(
    bindings: tuple[tuple[int, str, int, int], ...],
) -> bool:
    for parent_fd, component, expected_device, expected_inode in bindings:
        try:
            current = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected_device, expected_inode)
        ):
            return False
    return True


@dataclass(frozen=True)
class BitableBatchReceipt:
    attempted: int
    succeeded: int
    failed: int
    failed_indexes: tuple[int, ...] = ()
    unconfirmed_indexes: tuple[int, ...] = ()
    cancelled_indexes: tuple[int, ...] = ()
    client_tokens: tuple[str, ...] = dataclass_field(default=(), repr=False)
    record_ids: tuple[str, ...] = ()
    record_id_by_index: tuple[tuple[int, str], ...] = ()

    @property
    def complete(self) -> bool:
        return (
            self.failed == 0
            and not self.unconfirmed_indexes
            and not self.cancelled_indexes
            and self.succeeded == self.attempted
        )


@dataclass(frozen=True)
class BitableCoverUploadReceipt:
    status: str
    attempts: int
    reason_code: str = ""
    file_token: str = dataclass_field(default="", repr=False)


@dataclass(frozen=True)
class BitableMediaFieldReceipt:
    status: str
    attempts: int = 0
    reason_code: str = ""
    field_id: str = dataclass_field(default="", repr=False)
    client_token: str = dataclass_field(default="", repr=False)


@dataclass(frozen=True)
class BitableMediaAttachmentReceipt:
    status: str
    mode: str
    attempts: int
    confirmed_parts: int = 0
    reason_code: str = ""
    file_token: str = dataclass_field(default="", repr=False)
    client_token: str = dataclass_field(default="", repr=False)


@dataclass(frozen=True)
class BitableMediaReceiptCleanupResult:
    scanned: int = 0
    deleted: int = 0
    kept_unexpired: int = 0
    preserved_unknown: int = 0
    errors: int = 0


class BitableMediaReceiptStore:
    """分片上传的 24 小时私有原子收据；不保存凭据或绝对路径。"""

    _LIFETIME_SECONDS = 24 * 60 * 60
    _MAX_RECEIPT_BYTES = 64 * 1024

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            from .models_manager import app_data_dir

            root = app_data_dir() / "feishu" / "media-upload-receipts"
        self.root = Path(root)

    def write(self, intent_hash: str, payload: dict) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", str(intent_hash or "")):
            raise ValueError("音视频上传收据标识无效")
        root_fd, descriptors, bindings = _open_private_receipt_root(self.root)
        target = f"{intent_hash}.json"
        nonce = str(uuid4()).replace("-", "")
        temp = f".{intent_hash}.{nonce}.tmp"
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = -1
        committed = False
        try:
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
            descriptor = os.open(
                temp,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fchmod(descriptor, 0o600)
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
            os.replace(temp, target, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            committed = True
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
            os.fsync(root_fd)
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
        except BaseException:
            if committed:
                try:
                    os.unlink(target, dir_fd=root_fd)
                    os.fsync(root_fd)
                except OSError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.unlink(temp, dir_fd=root_fd)
            except OSError:
                pass
            for directory in reversed(descriptors):
                try:
                    os.close(directory)
                except OSError:
                    pass

    def cleanup_expired(
        self, *, now: float | None = None
    ) -> BitableMediaReceiptCleanupResult:
        """删除能严格证明已到期的自有收据；陌生或不安全项一律保留。"""

        if not os.path.lexists(self.root):
            return BitableMediaReceiptCleanupResult()
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time):
            raise ValueError("音视频上传收据清理时间无效")

        root_fd, descriptors, bindings = _open_private_receipt_root(self.root)
        scanned = 0
        deleted = 0
        kept_unexpired = 0
        preserved_unknown = 0
        current_uid = os.geteuid()
        try:
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
            names = os.listdir(root_fd)
            for name in names:
                scanned += 1
                if not re.fullmatch(r"[0-9a-f]{64}\.json", name):
                    preserved_unknown += 1
                    continue

                descriptor = -1
                try:
                    before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != current_uid
                        or stat.S_IMODE(before.st_mode) != 0o600
                        or before.st_nlink != 1
                        or before.st_size <= 0
                        or before.st_size > self._MAX_RECEIPT_BYTES
                    ):
                        preserved_unknown += 1
                        continue
                    descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_fd,
                    )
                    opened = os.fstat(descriptor)
                    if not self._same_receipt_identity(before, opened):
                        preserved_unknown += 1
                        continue
                    chunks: list[bytes] = []
                    remaining = self._MAX_RECEIPT_BYTES + 1
                    while remaining > 0:
                        chunk = os.read(descriptor, min(8192, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    if len(raw) != opened.st_size or len(raw) > self._MAX_RECEIPT_BYTES:
                        preserved_unknown += 1
                        continue
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        preserved_unknown += 1
                        continue
                    if not self._is_owned_expiry_contract(name, payload):
                        preserved_unknown += 1
                        continue
                    if current_time < float(payload["expires_at"]):
                        kept_unexpired += 1
                        continue
                    if not _receipt_root_unchanged(bindings):
                        raise OSError("音视频上传收据目录发生变化")
                    current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not self._same_receipt_identity(opened, current):
                        preserved_unknown += 1
                        continue
                    os.unlink(name, dir_fd=root_fd)
                    deleted += 1
                except OSError:
                    preserved_unknown += 1
                finally:
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
            if deleted:
                os.fsync(root_fd)
            if not _receipt_root_unchanged(bindings):
                raise OSError("音视频上传收据目录发生变化")
            return BitableMediaReceiptCleanupResult(
                scanned=scanned,
                deleted=deleted,
                kept_unexpired=kept_unexpired,
                preserved_unknown=preserved_unknown,
            )
        finally:
            for directory in reversed(descriptors):
                try:
                    os.close(directory)
                except OSError:
                    pass

    def cleanup_expired_best_effort(
        self, *, now: float | None = None
    ) -> BitableMediaReceiptCleanupResult:
        """后台维护入口；清理异常不能阻断启动或新的上传事务。"""

        try:
            return self.cleanup_expired(now=now)
        except Exception:  # noqa: BLE001
            return BitableMediaReceiptCleanupResult(errors=1)

    @staticmethod
    def _same_receipt_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            left.st_dev,
            left.st_ino,
            left.st_mode,
            left.st_uid,
            left.st_nlink,
            left.st_size,
            left.st_mtime_ns,
        ) == (
            right.st_dev,
            right.st_ino,
            right.st_mode,
            right.st_uid,
            right.st_nlink,
            right.st_size,
            right.st_mtime_ns,
        )

    @classmethod
    def _is_owned_expiry_contract(cls, name: str, payload: object) -> bool:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return False
        intent_hash = name.removesuffix(".json")
        if payload.get("intent_hash") != intent_hash:
            return False

        created_at = payload.get("created_at")
        expires_at = payload.get("expires_at")
        updated_at = payload.get("updated_at", created_at)
        values = (created_at, updated_at, expires_at)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ):
            return False
        created = float(created_at)
        updated = float(updated_at)
        expires = float(expires_at)
        return (
            created <= updated <= expires
            and abs((expires - updated) - cls._LIFETIME_SECONDS) <= 0.001
        )


@dataclass(frozen=True)
class BitableExportIssue:
    record_index: int
    stable_key: str
    title: str
    reason_code: str
    retry_hint: str


@dataclass(frozen=True)
class BitableExportReceipt:
    url: str
    row_total: int
    row_success: int
    row_failed: int
    row_unconfirmed: int
    row_cancelled: int
    cover_candidate: int
    cover_success: int
    cover_skipped: int
    cover_failed: int
    cover_uncertain: int
    cancelled: bool
    media_candidate: int = 0
    media_success: int = 0
    media_skipped: int = 0
    media_failed: int = 0
    media_uncertain: int = 0
    row_duplicate_skipped: int = 0
    outcome_uncertain: bool = False
    uncertain_stage: str = ""
    issues: tuple[BitableExportIssue, ...] = ()
    destination_app_token: str = dataclass_field(default="", repr=False)
    destination_table_id: str = dataclass_field(default="", repr=False)
    destination_table_name: str = ""

    @property
    def complete(self) -> bool:
        return (
            self.row_success == self.row_total
            and self.row_failed == 0
            and self.row_unconfirmed == 0
            and self.row_cancelled == 0
            and self.cover_success == self.cover_candidate
            and self.cover_skipped == 0
            and self.cover_failed == 0
            and self.cover_uncertain == 0
            and self.media_success == self.media_candidate
            and self.media_skipped == 0
            and self.media_failed == 0
            and self.media_uncertain == 0
            and not self.cancelled
            and not self.outcome_uncertain
        )

    def safe_details(self) -> str:
        lines: list[str] = []
        for issue in self.issues:
            identity = issue.title or issue.stable_key or f"第 {issue.record_index + 1} 条"
            code = str(issue.reason_code or "")
            if "uncertain" in code:
                fact = "这项可能已经发送，当前无法确认"
                next_step = "先查看现有表格的对应作品，再决定是否重新导出"
            elif "cancel" in code or "not_attempted" in code:
                fact = "这项尚未发送"
                next_step = "已停止后续上传，已经写入的内容会保留"
            else:
                fact = "该项没有发送成功"
                next_step = "按标题定位作品，修复本地文件或权限后重新导出"
            lines.append(f"{identity[:120]}：{fact}。{next_step}。")
        return "\n".join(lines)

    def human_summary(self) -> tuple[str, str, str]:
        """只返回面向用户的事实和下一步，不暴露内部原因码。"""
        if self.outcome_uncertain:
            if self.url.startswith("https://"):
                return (
                    "飞书结果待确认",
                    "飞书可能已经创建了结果，Lulu 没拿到完整确认",
                    "先打开现有结果确认，不要立即重试",
                )
            return (
                "飞书结果待确认",
                "飞书可能已经创建了结果，但 Lulu 没拿到可打开地址",
                "到目标知识库按本次标题和时间确认",
            )
        if self.cover_uncertain:
            return (
                "封面上传待确认",
                "这张封面可能已经上传，为避免重复附件已停止自动重传",
                "先查看现有表格的对应作品，再决定是否重新导出",
            )
        if self.media_uncertain:
            return (
                "音视频附件待确认",
                "这份音视频可能已经上传或写入，Lulu 没拿到完整回执",
                "保留现有表格，先查看对应作品再决定是否恢复",
            )
        if self.row_unconfirmed:
            return (
                "部分记录待确认",
                f"有 {self.row_unconfirmed} 行可能已写入，当前无法确认",
                "先核对表内行数和标题，再处理缺失项",
            )
        if self.cancelled or self.row_cancelled:
            return (
                "已停止后续上传",
                "已经创建或写入的内容会保留",
                "打开现有结果确认，无需等待自动回滚",
            )
        if (
            self.row_failed
            or self.cover_failed
            or self.cover_skipped
            or self.media_failed
            or self.media_skipped
        ):
            return (
                "该项未发送成功",
                "该项没有发送成功",
                "按标题定位作品，修复本地文件或权限后重新导出",
            )
        return ("", "", "")


class BitablePartialWriteError(RuntimeError):
    def __init__(self, receipt: BitableBatchReceipt):
        self.receipt = receipt
        prefix = "飞书写入部分成功" if receipt.succeeded else "飞书写入失败"
        super().__init__(
            f"{prefix}：成功 {receipt.succeeded} 条，失败 {receipt.failed} 条，"
            f"未确认 {len(receipt.unconfirmed_indexes)} 条，"
            f"未发送 {len(receipt.cancelled_indexes)} 条。"
        )


class BitableTargetError(RuntimeError):
    """已有多维表格目标在任何写入前验证失败。"""


@dataclass(frozen=True)
class ExistingBitableTarget:
    app_token: str
    table_id: str
    table_name: str
    url: str


def create_wiki_bitable(space_id: str, parent_node: str, title: str) -> dict:
    """在知识库下新建一个多维表格节点。返回 {app_token, node_token, url}。"""
    if not space_id:
        raise RuntimeError("还没选择目标知识库。请到「集成 → 飞书知识库」里选一个知识库。")
    token = _user_token()
    body = {"obj_type": "bitable", "node_type": "origin", "title": title}
    if parent_node:
        body["parent_node_token"] = parent_node
    data = _api("POST", f"/wiki/v2/spaces/{space_id}/nodes", token, json=body)
    node = data.get("node") or {}
    app_token = node.get("obj_token")
    node_token = node.get("node_token")
    if not app_token:
        known_url = f"https://feishu.cn/wiki/{node_token}" if node_token else ""
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书可能已创建多维表格节点，但没有返回完整回执。",
            stage="wiki_node_create",
            known_url=known_url,
        )
    return {
        "app_token": app_token,
        "node_token": node_token,
        "url": f"https://feishu.cn/wiki/{node_token}" if node_token else "",
    }


def create_drive_bitable(title: str) -> dict:
    """在用户飞书云盘（我的文稿库）根目录创建多维表格。"""

    token = _user_token()
    data = _api("POST", "/bitable/v1/apps", token, json={"name": title})
    app = data.get("app") or {}
    app_token = str(app.get("app_token") or "")
    url = str(app.get("url") or "")
    if not app_token:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书可能已在云盘创建多维表格，但没有返回完整回执。",
            stage="drive_bitable_create",
            known_url=url,
        )
    return {
        "app_token": app_token,
        "node_token": "",
        "url": url or f"https://feishu.cn/base/{app_token}",
    }


def _bitable_field_payload(spec) -> dict:
    if isinstance(spec, dict):
        name = spec.get("field_name")
        field_type = spec.get("type")
        has_property = "property" in spec
        property_value = spec.get("property")
    elif isinstance(spec, (list, tuple)) and len(spec) in (2, 3):
        name, field_type = spec[:2]
        has_property = len(spec) == 3
        property_value = spec[2] if has_property else None
    else:
        raise ValueError("飞书字段定义格式无效。")
    if not isinstance(name, str) or not name.strip() or isinstance(field_type, bool):
        raise ValueError("飞书字段名称或类型无效。")
    payload = {"field_name": name.strip(), "type": int(field_type)}
    if has_property:
        payload["property"] = (
            dict(property_value)
            if isinstance(property_value, dict)
            else property_value
        )
    return payload


def create_bitable_table(app_token: str, name: str, fields: list) -> str:
    """在多维表格里新建一张数据表，并保留字段原生 property。"""
    token = _user_token()
    body = {"table": {
        "name": name,
        "fields": [_bitable_field_payload(field) for field in fields],
    }}
    data = _api("POST", f"/bitable/v1/apps/{app_token}/tables", token, json=body)
    table_id = data.get("table_id")
    if not table_id:
        raise RuntimeError("飞书未返回数据表 table_id。")
    return table_id


def _bitable_cell_is_blank(value: object) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, (list, tuple)):
        return all(_bitable_cell_is_blank(item) for item in value)
    if isinstance(value, dict):
        return all(_bitable_cell_is_blank(item) for item in value.values())
    return False


def _remove_initial_blank_bitable_records(
    app_token: str,
    table_id: str,
    token: str,
) -> int:
    """只清理刚创建 Base 自带的全空记录，任何真实值都失败关闭。"""

    records: list[dict] = []
    page_token = ""
    seen_pages: set[str] = set()
    while True:
        path = (
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            "?page_size=500"
        )
        if page_token:
            path += f"&page_token={quote(page_token, safe='')}"
        data = _api("GET", path, token)
        page = data.get("items", []) or []
        if not isinstance(page, list) or not all(
            isinstance(item, dict) for item in page
        ):
            raise RuntimeError("新建多维表格的初始记录回执异常。")
        records.extend(page)
        if not data.get("has_more"):
            break
        next_page = str(data.get("page_token") or "")
        if not next_page or next_page in seen_pages:
            raise RuntimeError("新建多维表格的初始记录分页异常。")
        seen_pages.add(next_page)
        page_token = next_page

    record_ids: list[str] = []
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict) or not _bitable_cell_is_blank(fields):
            raise RuntimeError("新建多维表格的初始记录不是全空，Lulu 已停止写入。")
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise RuntimeError("新建多维表格的空记录缺少 record_id。")
        record_ids.append(record_id)
    if len(record_ids) != len(set(record_ids)):
        raise RuntimeError("新建多维表格的空记录身份重复。")

    for offset in range(0, len(record_ids), _BT_BATCH):
        chunk = record_ids[offset:offset + _BT_BATCH]
        _api(
            "POST",
            (
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/"
                "batch_delete"
            ),
            token,
            json={"records": chunk},
        )
    return len(record_ids)


def _prepare_initial_bitable_table(
    app_token: str,
    name: str,
    fields: list,
    token: str,
) -> str:
    """复用新建 Base 自带的唯一空白表。

    飞书创建 Base 时已经附带一张空白数据表。Lulu 只重命名并就地
    配置它，不再新建第二张表后尝试回删。任一步失败都由上层结算
    为配置结果待核对，且不会继续写入记录。
    """

    table_specs = [
        _bitable_field_payload(field)
        for field in fields
    ]
    if not table_specs:
        raise ValueError("飞书数据表至少需要一个字段。")

    tables = [
        table
        for table in _list_bitable_tables(app_token, token)
        if isinstance(table, dict) and str(table.get("table_id") or "")
    ]
    if len(tables) != 1:
        raise RuntimeError("新建多维表格的初始数据表数量异常。")
    table_id = str(tables[0]["table_id"])

    _api(
        "PATCH",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}",
        token,
        json={"name": str(name or "").strip() or "作品库"},
    )

    existing_fields = [
        field
        for field in _list_bitable_fields(app_token, table_id, token)
        if isinstance(field, dict) and str(field.get("field_id") or "")
    ]
    if not existing_fields:
        raise RuntimeError("新建多维表格没有可复用的初始字段。")

    primary_id = str(existing_fields[0]["field_id"])
    _api(
        "PUT",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{primary_id}",
        token,
        json=table_specs[0],
    )

    # 新建 Base 理论上只有一个主字段；若飞书后续增加默认空字段，
    # 这些字段也属于本次刚创建的空表，可在写记录前安全移除。
    for field in existing_fields[1:]:
        field_id = str(field["field_id"])
        _api(
            "DELETE",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            token,
        )

    for spec in table_specs[1:]:
        client_token = str(uuid4())
        _api(
            "POST",
            (
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
                f"?client_token={client_token}"
            ),
            token,
            json=spec,
        )
    _remove_initial_blank_bitable_records(app_token, table_id, token)
    return table_id


def _list_bitable_fields(app_token: str, table_id: str, token: str) -> list[dict]:
    items: list[dict] = []
    page_token = ""
    seen: set[str] = set()
    while True:
        path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields?page_size=100"
        if page_token:
            path += f"&page_token={quote(page_token, safe='')}"
        data = _api("GET", path, token)
        items.extend(item for item in (data.get("items", []) or []) if isinstance(item, dict))
        if not data.get("has_more"):
            break
        next_token = str(data.get("page_token") or "")
        if not next_token or next_token in seen:
            raise FeishuTransportError(
                retryable_not_sent=False,
                uncertain=False,
                message="飞书字段列表分页回执异常。",
                stage="media_field_list",
            )
        seen.add(next_token)
        page_token = next_token
    return items


def _inspect_bitable_media_field(fields: Iterable[dict]) -> tuple[str, str]:
    matches = [field for field in fields if str(field.get("field_name") or "") == "音视频附件"]
    if not matches:
        return "missing", ""
    if len(matches) != 1 or matches[0].get("type") != BT_ATTACHMENT:
        return "conflict", ""
    field_id = str(matches[0].get("field_id") or "")
    return "ready", field_id


def ensure_bitable_media_field(
    app_token: str,
    table_id: str,
    *,
    confirmed: bool,
    token: str | None = None,
) -> BitableMediaFieldReceipt:
    """分页复核固定附件列；缺列时只在已确认后以稳定 key 创建。"""

    current_token = token or _user_token()
    try:
        status, field_id = _inspect_bitable_media_field(
            _list_bitable_fields(app_token, table_id, current_token)
        )
    except FeishuTransportError as exc:
        return BitableMediaFieldReceipt(
            "uncertain" if exc.uncertain else "failed",
            reason_code="media_field_list_uncertain" if exc.uncertain else "media_field_list_failed",
        )
    except Exception:
        return BitableMediaFieldReceipt("failed", reason_code="media_field_list_failed")
    if status == "ready":
        return BitableMediaFieldReceipt("ready", field_id=field_id)
    if status == "conflict":
        return BitableMediaFieldReceipt("conflict", reason_code="media_field_type_conflict")
    if not confirmed:
        return BitableMediaFieldReceipt(
            "confirmation_required",
            reason_code="media_field_confirmation_required",
        )

    client_token = str(uuid4())
    path = (
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        f"?client_token={client_token}"
    )
    body = {"field_name": "音视频附件", "type": BT_ATTACHMENT, "property": None}
    attempts = 0
    while attempts < 2:
        attempts += 1
        post_uncertain = False
        try:
            with _bitable_table_write_lock(app_token, table_id):
                _api("POST", path, current_token, json=body)
        except FeishuApiError as exc:
            if exc.code in {1061045, 1061006} and attempts < 2:
                time.sleep(0.2)
                continue
            return BitableMediaFieldReceipt(
                "failed",
                attempts,
                "media_field_create_failed",
                client_token=client_token,
            )
        except FeishuTransportError as exc:
            if exc.retryable_not_sent and attempts < 2:
                time.sleep(0.2)
                continue
            post_uncertain = exc.uncertain
            if not post_uncertain:
                return BitableMediaFieldReceipt(
                    "failed",
                    attempts,
                    "media_field_create_failed",
                    client_token=client_token,
                )
        except Exception:
            post_uncertain = True

        try:
            checked, checked_id = _inspect_bitable_media_field(
                _list_bitable_fields(app_token, table_id, current_token)
            )
        except Exception:
            return BitableMediaFieldReceipt(
                "uncertain",
                attempts,
                "media_field_create_uncertain",
                client_token=client_token,
            )
        if checked == "ready":
            return BitableMediaFieldReceipt(
                "ready",
                attempts,
                field_id=checked_id,
                client_token=client_token,
            )
        if checked == "conflict":
            return BitableMediaFieldReceipt(
                "conflict",
                attempts,
                "media_field_type_conflict",
                client_token=client_token,
            )
        if attempts < 2:
            # 成功响应但重读仍缺失，或不确定响应且重读未出现：同 key 只重试一次。
            time.sleep(0.2 if post_uncertain else 0.1)
            continue
        return BitableMediaFieldReceipt(
            "uncertain" if post_uncertain else "failed",
            attempts,
            "media_field_create_uncertain" if post_uncertain else "media_field_create_failed",
            client_token=client_token,
        )
    return BitableMediaFieldReceipt(
        "failed", attempts, "media_field_create_failed", client_token=client_token
    )


def _list_bitable_tables(app_token: str, token: str) -> list:
    data = _api("GET", f"/bitable/v1/apps/{app_token}/tables?page_size=100", token)
    return data.get("items", []) or []


def _list_all_bitable_tables(app_token: str, token: str) -> list[dict]:
    items: list[dict] = []
    page_token = ""
    seen: set[str] = set()
    while True:
        path = f"/bitable/v1/apps/{app_token}/tables?page_size=100"
        if page_token:
            path += f"&page_token={quote(page_token, safe='')}"
        data = _api("GET", path, token)
        items.extend(item for item in (data.get("items", []) or []) if isinstance(item, dict))
        if not data.get("has_more"):
            return items
        next_token = str(data.get("page_token") or "")
        if not next_token or next_token in seen:
            raise BitableTargetError("飞书数据表列表返回异常，Lulu 没有写入。")
        seen.add(next_token)
        page_token = next_token


def _parse_existing_bitable_url(source: str) -> tuple[str, str, str, str]:
    """只接受飞书多维表格的完整 HTTPS 链接。

    返回 ``(resource_kind, resource_token, table_id, normalized_url)``。
    资源 token 只用于官方 OpenAPI，不会请求用户粘贴的主机。
    """

    raw = str(source or "").strip()
    if len(raw) > 4096:
        raise BitableTargetError(
            "这条飞书链接异常过长。请重新复制浏览器地址栏里的完整地址。"
        )
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise BitableTargetError(
            "没认出这张多维表格的链接。请复制浏览器地址栏里的完整地址。"
        ) from exc
    host = (parsed.hostname or "").casefold()
    allowed_host = (
        host == "feishu.cn"
        or host.endswith(".feishu.cn")
        or host == "larksuite.com"
        or host.endswith(".larksuite.com")
    )
    if (
        parsed.scheme.casefold() != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BitableTargetError(
            "请粘贴飞书多维表格的完整 HTTPS 链接。Lulu 没有访问其他网站。"
        )
    resource = re.search(r"/(base|wiki)/([A-Za-z0-9]+)(?:/|$)", parsed.path)
    query = parse_qs(parsed.query)
    table_id = str((query.get("table") or query.get("table_id") or [""])[0]).strip()
    if not resource or not re.fullmatch(r"[A-Za-z0-9]+", table_id):
        raise BitableTargetError(
            "链接里没有具体数据表。请先在飞书中打开要长期积累的那张表，再复制地址。"
        )
    return resource.group(1), resource.group(2), table_id, raw


def _ensure_existing_bitable_fields_compatible(
    actual_fields: Iterable[dict],
    expected_fields: Iterable[object],
) -> None:
    actual_by_name: dict[str, list[dict]] = {}
    for field in actual_fields:
        name = str((field or {}).get("field_name") or "").strip()
        if name:
            actual_by_name.setdefault(name, []).append(field)

    missing: list[str] = []
    conflicting: list[str] = []
    for expected in expected_fields:
        payload = _bitable_field_payload(expected)
        name = payload["field_name"]
        matches = actual_by_name.get(name, [])
        if not matches:
            missing.append(name)
            continue
        if len(matches) != 1:
            conflicting.append(name)
            continue
        try:
            actual_type = int(matches[0].get("type"))
        except (TypeError, ValueError):
            conflicting.append(name)
            continue
        if actual_type != payload["type"]:
            conflicting.append(name)

    if not missing and not conflicting:
        return
    details: list[str] = []
    if missing:
        details.append("缺少字段：" + "、".join(missing[:8]))
    if conflicting:
        details.append("同名字段类型不一致：" + "、".join(conflicting[:8]))
    raise BitableTargetError(
        "目标表与 Lulu 当前字段不兼容（"
        + "；".join(details)
        + "）。为了不改坏已有表格，Lulu 没有写入，也没有自动增删字段。"
    )


def resolve_existing_bitable(
    source: str,
    expected_fields: Iterable[object],
) -> ExistingBitableTarget:
    """解析指定已有表，并在任何写入前校验表与字段。"""

    resource_kind, resource_token, table_id, normalized_url = (
        _parse_existing_bitable_url(source)
    )
    token = _user_token()
    app_token = resource_token
    if resource_kind == "wiki":
        data = _api(
            "GET",
            f"/wiki/v2/spaces/get_node?token={quote(resource_token, safe='')}&obj_type=wiki",
            token,
        )
        node = data.get("node") or {}
        obj_type = str(node.get("obj_type") or "")
        if obj_type and obj_type != "bitable":
            raise BitableTargetError("这个飞书知识库页面不是多维表格。")
        app_token = str(node.get("obj_token") or "")
        if not app_token:
            raise BitableTargetError(
                "没能解析这张多维表格。请确认当前飞书账号已获授权且有访问权限。"
            )

    tables = _list_all_bitable_tables(app_token, token)
    table = next(
        (item for item in tables if str((item or {}).get("table_id") or "") == table_id),
        None,
    )
    if table is None:
        raise BitableTargetError(
            "没找到链接指定的数据表。它可能已被删除，或当前飞书账号没有访问权限。"
        )
    _ensure_existing_bitable_fields_compatible(
        _list_bitable_fields(app_token, table_id, token),
        expected_fields,
    )
    return ExistingBitableTarget(
        app_token=app_token,
        table_id=table_id,
        table_name=str(table.get("name") or table.get("table_name") or "已有多维表格"),
        url=normalized_url,
    )


def find_existing_bitable_text_values(
    app_token: str,
    table_id: str,
    field_name: str,
    values: Iterable[str],
    *,
    token: str | None = None,
) -> set[str]:
    """让 Base 在云端精确筛选文本业务键，返回已经存在的值。

    每组最多 50 个条件，避免把长期作品库全量拉回本地；分页仍串行读取，
    直到确认这一组业务键的全部命中结果。
    """

    wanted = tuple(dict.fromkeys(
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    ))
    if not wanted:
        return set()
    clean_field = str(field_name or "").strip()
    if not clean_field:
        raise ValueError("飞书查重字段不能为空。")
    current_token = token or _user_token()
    found: set[str] = set()

    def cell_texts(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, dict):
            # URL 字段回执通常同时含 link/text；查重必须优先使用真实链接，
            # 不能拿用户可见标题去和原作品 URL 比较。
            link = value.get("link")
            if isinstance(link, str) and link.strip():
                return (link,)
            text = value.get("text")
            return (str(text),) if isinstance(text, str) else ()
        if isinstance(value, list):
            return tuple(
                text
                for item in value
                for text in cell_texts(item)
            )
        return ()

    for offset in range(0, len(wanted), 50):
        chunk = wanted[offset:offset + 50]
        page_token = ""
        seen_pages: set[str] = set()
        while True:
            path = (
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/"
                "search?page_size=500"
            )
            if page_token:
                path += f"&page_token={quote(page_token, safe='')}"
            body = {
                "field_names": [clean_field],
                "filter": {
                    "conjunction": "or",
                    "conditions": [
                        {
                            "field_name": clean_field,
                            "operator": "is",
                            "value": [value],
                        }
                        for value in chunk
                    ],
                },
            }
            data = _api("POST", path, current_token, json=body)
            for item in data.get("items", []) or []:
                fields = (item or {}).get("fields", {}) if isinstance(item, dict) else {}
                for value in cell_texts(fields.get(clean_field)):
                    if value in chunk:
                        found.add(value)
            if not data.get("has_more"):
                break
            next_page = str(data.get("page_token") or "")
            if not next_page or next_page in seen_pages:
                raise BitableTargetError(
                    "飞书查重分页回执异常，Lulu 没有新增记录。"
                )
            seen_pages.add(next_page)
            page_token = next_page
    return found


def delete_bitable_table(app_token: str, table_id: str) -> None:
    _api("DELETE", f"/bitable/v1/apps/{app_token}/tables/{table_id}", _user_token())


def _valid_bitable_image(file_name: str, mime_type: str, data: bytes) -> bool:
    if (
        not isinstance(file_name, str)
        or not file_name
        or len(file_name) > 250
        or "/" in file_name
        or "\\" in file_name
        or any(ord(char) < 32 for char in file_name)
        or mime_type not in _BITABLE_MEDIA_MIMES
        or not isinstance(data, bytes)
        or not (1 <= len(data) <= _BITABLE_MEDIA_MAX_BYTES)
    ):
        return False
    suffix = "." + file_name.rsplit(".", 1)[-1].casefold() if "." in file_name else ""
    if mime_type == "image/jpeg":
        return suffix == ".jpg" and data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")
    return (
        suffix == ".webp"
        and len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
    )


def _valid_file_token(value: object) -> str:
    token = value if isinstance(value, str) else ""
    if (
        not token
        or len(token) > 1024
        or any(ord(char) < 33 or char.isspace() for char in token)
    ):
        return ""
    return token


def _bitable_media_request(call: Callable[[], object]):
    """封面与音视频共享的单槽、4 QPS 调度门。"""

    global _bitable_media_last_start
    with _bitable_media_lock:
        now = time.monotonic()
        wait = _BITABLE_MEDIA_MIN_INTERVAL - (now - _bitable_media_last_start)
        if wait > 0:
            time.sleep(wait)
        _bitable_media_last_start = time.monotonic()
        return call()


def _upload_bitable_image_once(
    token: str,
    app_token: str,
    file_name: str,
    mime_type: str,
    data: bytes,
) -> str:
    import requests

    extra = json.dumps(
        {"drive_route_token": app_token},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    files = {
        "file_name": (None, file_name),
        "parent_type": (None, "bitable_image"),
        "parent_node": (None, app_token),
        "size": (None, str(len(data))),
        "extra": (None, extra),
        "file": (file_name, data, mime_type),
    }
    response = _bitable_media_request(
        lambda: requests.post(
            f"{_BASE}{_BITABLE_MEDIA_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            timeout=(10, 120),
            allow_redirects=False,
        )
    )
    _reject_post_redirect(response)
    try:
        raw = response.json()
    except Exception as exc:  # noqa: BLE001 - 不回显响应体
        raise FeishuApiError("invalid_response", "飞书素材上传返回异常。") from exc
    if not isinstance(raw, dict) or "code" not in raw:
        raise FeishuApiError("invalid_response", "飞书素材上传返回异常。")
    payload = _check(raw)
    file_token = _valid_file_token(payload.get("file_token"))
    if not file_token:
        raise FeishuApiError("missing_file_token", "飞书素材上传未返回有效回执。")
    return file_token


def upload_bitable_image(
    app_token: str,
    file_name: str,
    mime_type: str,
    data: bytes,
    *,
    token: str | None = None,
) -> BitableCoverUploadReceipt:
    """串行调用方使用的单附件上传；只对明确未发送或 1061045 重试一次。"""

    if not _valid_bitable_image(file_name, mime_type, data):
        return BitableCoverUploadReceipt(
            status="final_failed",
            attempts=0,
            reason_code="cover_upload_input_rejected",
        )
    current_token = token or _user_token()
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            file_token = _upload_bitable_image_once(
                current_token,
                app_token,
                file_name,
                mime_type,
                data,
            )
            return BitableCoverUploadReceipt(
                status="uploaded",
                attempts=attempts,
                file_token=file_token,
            )
        except FeishuTransportError:
            return BitableCoverUploadReceipt(
                status="upload_uncertain",
                attempts=attempts,
                reason_code="cover_upload_uncertain",
            )
        except FeishuApiError as exc:
            if exc.code == 1061045 and attempts < 2:
                time.sleep(0.2)
                continue
            reason = (
                "cover_upload_retry_exhausted"
                if exc.code == 1061045
                else "cover_upload_rejected"
            )
            uncertain = exc.code in {"invalid_response", "missing_file_token"}
            return BitableCoverUploadReceipt(
                status="upload_uncertain" if uncertain else "final_failed",
                attempts=attempts,
                reason_code=reason if not uncertain else "cover_upload_uncertain",
            )
        except Exception as exc:  # requests 异常在运行时导入，避免新增依赖
            import requests

            if isinstance(exc, requests.RequestException):
                not_sent = _request_not_sent(exc, requests)
                if not_sent and attempts < 2:
                    time.sleep(0.2)
                    continue
                return BitableCoverUploadReceipt(
                    status="final_failed" if not_sent else "upload_uncertain",
                    attempts=attempts,
                    reason_code=(
                        "cover_upload_connection_failed"
                        if not_sent
                        else "cover_upload_uncertain"
                    ),
                )
            return BitableCoverUploadReceipt(
                status="final_failed",
                attempts=attempts,
                reason_code="cover_upload_internal_failed",
            )


class _BitableMediaStepError(RuntimeError):
    def __init__(self, status: str, reason_code: str, attempts: int):
        self.status = status
        self.reason_code = reason_code
        self.attempts = attempts
        super().__init__(reason_code)


def _rate_limit_reset(response) -> float:
    headers = getattr(response, "headers", {}) or {}
    try:
        return max(0.0, float(headers.get("x-ogw-ratelimit-reset") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _post_bitable_media_once(
    token: str,
    path: str,
    *,
    json_body: dict | None = None,
    files: dict | None = None,
) -> dict:
    import requests

    options: dict = {
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": (10, 120),
        "allow_redirects": False,
    }
    if json_body is not None:
        options["json"] = json_body
    if files is not None:
        options["files"] = files
    response = _bitable_media_request(
        lambda: requests.post(f"{_BASE}{path}", **options)
    )
    _reject_post_redirect(response)
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code >= 500:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书素材上传结果待确认。",
        )
    if status_code == 429:
        raise FeishuApiError(
            99991400,
            "飞书素材接口正在限流。",
            retry_after=_rate_limit_reset(response),
        )
    try:
        raw = response.json()
    except Exception as exc:  # noqa: BLE001 - 严禁回显远端响应体
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书素材上传结果待确认。",
        ) from exc
    if not isinstance(raw, dict) or "code" not in raw:
        raise FeishuTransportError(
            retryable_not_sent=False,
            uncertain=True,
            message="飞书素材上传结果待确认。",
        )
    code = raw.get("code")
    if code == 99991400:
        raise FeishuApiError(
            99991400,
            "飞书素材接口正在限流。",
            retry_after=_rate_limit_reset(response),
        )
    if status_code >= 400 and code == 0:
        raise FeishuApiError(status_code, "飞书素材接口拒绝了请求。")
    return _check(raw)


def _run_bitable_media_step(
    stage: str,
    request: Callable[[], dict],
    *,
    is_cancelled: Callable[[], bool],
) -> tuple[dict, int]:
    attempts = 0
    while attempts < 2:
        if is_cancelled():
            raise _BitableMediaStepError("cancelled", "media_upload_cancelled", attempts)
        attempts += 1
        try:
            return request(), attempts
        except FeishuApiError as exc:
            if exc.code in _BITABLE_MEDIA_RETRY_CODES and attempts < 2:
                time.sleep(exc.retry_after or 0.2)
                continue
            reason = (
                "media_rate_limited"
                if exc.code in {99991400, 1061045}
                else "media_upload_expired"
                if exc.code == 1061021
                else "media_upload_rejected"
            )
            raise _BitableMediaStepError("explicit_failed", reason, attempts) from exc
        except FeishuTransportError as exc:
            if exc.retryable_not_sent and attempts < 2:
                time.sleep(0.2)
                continue
            raise _BitableMediaStepError(
                "uncertain" if exc.uncertain else "explicit_failed",
                f"media_{stage}_uncertain" if exc.uncertain else "media_connection_failed",
                attempts,
            ) from exc
        except Exception as exc:  # requests 在运行时导入，保持现有零新增依赖
            import requests

            if isinstance(exc, requests.RequestException):
                not_sent = _request_not_sent(exc, requests)
                if not_sent and attempts < 2:
                    time.sleep(0.2)
                    continue
                raise _BitableMediaStepError(
                    "explicit_failed" if not_sent else "uncertain",
                    "media_connection_failed" if not_sent else f"media_{stage}_uncertain",
                    attempts,
                ) from exc
            raise _BitableMediaStepError(
                "explicit_failed", "media_upload_internal_failed", attempts
            ) from exc
    raise _BitableMediaStepError("explicit_failed", "media_upload_retry_exhausted", attempts)


def _record_has_media_token(data: dict, file_token: str, field_name: str = "音视频附件") -> bool:
    record = data.get("record") if isinstance(data.get("record"), dict) else data
    fields = record.get("fields", {}) if isinstance(record, dict) else {}
    attachments = fields.get(field_name, []) if isinstance(fields, dict) else []
    return any(
        isinstance(item, dict) and item.get("file_token") == file_token
        for item in (attachments if isinstance(attachments, list) else [])
    )


def _bitable_media_mode(size: object, connection_unstable: bool = False) -> str:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return "invalid"
    if size > _BITABLE_FILE_MAX_BYTES:
        return "too_large"
    if connection_unstable or size > _BITABLE_FILE_UPLOAD_ALL_MAX:
        return "multipart"
    return "upload_all"


def _attach_bitable_media_token(
    app_token: str,
    table_id: str,
    record_id: str,
    file_token: str,
    token: str,
    *,
    is_cancelled: Callable[[], bool],
    field_name: str = "音视频附件",
) -> tuple[str, int, str, str]:
    if is_cancelled():
        return "cancelled", 0, "media_attach_cancelled", ""
    client_token = str(uuid4())
    path = (
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/"
        f"batch_update?client_token={client_token}"
    )
    payload = {
        "records": [{
            "record_id": record_id,
            "fields": {field_name: [{"file_token": file_token}]},
        }]
    }
    read_path = f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    attempts = 0
    while attempts < 2:
        if is_cancelled():
            return "cancelled", attempts, "media_attach_cancelled", client_token
        attempts += 1
        uncertain = False
        try:
            with _bitable_table_write_lock(app_token, table_id):
                _api("POST", path, token, json=payload)
            return "attached", attempts, "", client_token
        except FeishuApiError as exc:
            if exc.code in {1061045, 1061006} and attempts < 2:
                time.sleep(0.2)
                continue
            return "explicit_failed", attempts, "media_attach_rejected", client_token
        except FeishuTransportError as exc:
            if exc.retryable_not_sent and attempts < 2:
                time.sleep(0.2)
                continue
            if not exc.uncertain:
                return "explicit_failed", attempts, "media_attach_failed", client_token
            uncertain = True
        except Exception:
            uncertain = True
        if uncertain:
            try:
                current = _api("GET", read_path, token)
            except Exception:
                return "uncertain", attempts, "media_attach_uncertain", client_token
            if _record_has_media_token(current, file_token, field_name):
                return "attached", attempts, "", client_token
            if attempts < 2:
                time.sleep(0.2)
                continue
            return "uncertain", attempts, "media_attach_uncertain", client_token
    return "explicit_failed", attempts, "media_attach_failed", client_token


def upload_and_attach_bitable_media(
    app_token: str,
    table_id: str,
    record_id: str,
    candidate: object,
    *,
    token: str | None = None,
    connection_unstable: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
    receipt_store: BitableMediaReceiptStore | None = None,
    field_name: str = "音视频附件",
) -> BitableMediaAttachmentReceipt:
    """上传单个受管音视频并立即后补原记录；任何不确定响应都不盲重放。"""

    from .collect.bitable_media import (
        BitableMediaFileError,
        verified_bitable_media_stream,
    )

    cancelled = is_cancelled or (lambda: False)
    size = getattr(candidate, "size", 0)
    file_name = str(getattr(candidate, "file_name", "") or "")
    selected_mode = _bitable_media_mode(size, connection_unstable)
    if (
        selected_mode in {"invalid", "too_large"}
        or not file_name
        or len(file_name) > 250
        or "/" in file_name
        or "\\" in file_name
        or any(ord(char) < 32 for char in file_name)
        or not str(record_id or "")
    ):
        return BitableMediaAttachmentReceipt(
            "explicit_failed", "", 0, reason_code="media_upload_input_rejected"
        )
    mode = selected_mode
    current_token = token or _user_token()
    attempts = 0
    confirmed_parts = 0
    file_token = ""
    private_state: dict | None = None
    private_store: BitableMediaReceiptStore | None = None
    intent_hash = ""

    def persist_private(status: str, **updates) -> None:
        if private_state is None or private_store is None:
            return
        private_state.update(updates)
        private_state["status"] = status
        updated_at = time.time()
        private_state["updated_at"] = updated_at
        private_state["expires_at"] = (
            updated_at + BitableMediaReceiptStore._LIFETIME_SECONDS
        )
        private_store.write(intent_hash, private_state)

    try:
        with verified_bitable_media_stream(candidate) as stream:
            if mode == "upload_all":
                extra = json.dumps(
                    {"drive_route_token": app_token},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

                def upload_all_request() -> dict:
                    stream.seek(0)
                    return _post_bitable_media_once(
                        current_token,
                        "/drive/v1/medias/upload_all",
                        files={
                            "file_name": (None, file_name),
                            "parent_type": (None, "bitable_file"),
                            "parent_node": (None, app_token),
                            "size": (None, str(size)),
                            "checksum": (None, str(getattr(candidate, "adler32", ""))),
                            "extra": (None, extra),
                            "file": (file_name, stream),
                        },
                    )

                data, used = _run_bitable_media_step(
                    "upload_all", upload_all_request, is_cancelled=cancelled
                )
                attempts += used
                file_token = _valid_file_token(data.get("file_token"))
                if not file_token:
                    raise _BitableMediaStepError(
                        "uncertain", "media_upload_all_uncertain", 0
                    )
            else:
                private_store = receipt_store or BitableMediaReceiptStore()
                private_store.cleanup_expired_best_effort()
                extra = json.dumps(
                    {"drive_route_token": app_token},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                prepare_body = {
                    "file_name": file_name,
                    "parent_type": "bitable_file",
                    "parent_node": app_token,
                    "size": size,
                    "extra": extra,
                }
                data, used = _run_bitable_media_step(
                    "prepare",
                    lambda: _post_bitable_media_once(
                        current_token,
                        "/drive/v1/medias/upload_prepare",
                        json_body=prepare_body,
                    ),
                    is_cancelled=cancelled,
                )
                attempts += used
                upload_id = _valid_file_token(data.get("upload_id"))
                expected_blocks = math.ceil(size / _BITABLE_FILE_BLOCK_SIZE)
                if (
                    not upload_id
                    or data.get("block_size") != _BITABLE_FILE_BLOCK_SIZE
                    or data.get("block_num") != expected_blocks
                ):
                    raise _BitableMediaStepError(
                        "uncertain" if not upload_id else "explicit_failed",
                        "media_prepare_uncertain" if not upload_id else "media_prepare_mismatch",
                        0,
                    )
                created_at = time.time()
                intent_hash = hashlib.sha256(
                    "\0".join((
                        app_token,
                        table_id,
                        record_id,
                        field_name,
                        str(getattr(candidate, "stable_key", "")),
                        str(getattr(candidate, "sha256", "")),
                    )).encode("utf-8")
                ).hexdigest()
                private_state = {
                    "schema_version": 1,
                    "intent_hash": intent_hash,
                    "file_sha256": str(getattr(candidate, "sha256", "")),
                    "size": size,
                    "mime": str(getattr(candidate, "mime_type", "")),
                    "identity": [
                        int(getattr(candidate, "device", 0)),
                        int(getattr(candidate, "inode", 0)),
                        size,
                        int(getattr(candidate, "mtime_ns", 0)),
                    ],
                    "target": {
                        "app_token": app_token,
                        "table_id": table_id,
                        "record_id": record_id,
                        "field_name": field_name,
                    },
                    "upload_id": upload_id,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "expires_at": (
                        created_at + BitableMediaReceiptStore._LIFETIME_SECONDS
                    ),
                    "block_size": _BITABLE_FILE_BLOCK_SIZE,
                    "block_num": expected_blocks,
                    "confirmed_parts": [],
                    "file_token": "",
                    "client_token": "",
                    "reason_code": "",
                }
                try:
                    persist_private("preparing_parts")
                except Exception as exc:
                    raise _BitableMediaStepError(
                        "explicit_failed", "media_receipt_write_failed", 0
                    ) from exc
                stream.seek(0)
                for seq in range(expected_blocks):
                    if cancelled():
                        raise _BitableMediaStepError(
                            "cancelled", "media_upload_cancelled", 0
                        )
                    chunk = stream.read(_BITABLE_FILE_BLOCK_SIZE)
                    expected_size = min(
                        _BITABLE_FILE_BLOCK_SIZE,
                        size - seq * _BITABLE_FILE_BLOCK_SIZE,
                    )
                    if len(chunk) != expected_size:
                        raise _BitableMediaStepError(
                            "explicit_failed", "media_file_changed", 0
                        )
                    part_checksum = str(zlib.adler32(chunk) & 0xFFFFFFFF)
                    part_files = {
                        "upload_id": (None, upload_id),
                        "seq": (None, str(seq)),
                        "size": (None, str(len(chunk))),
                        "checksum": (None, part_checksum),
                        "file": (file_name, chunk),
                    }
                    _data, used = _run_bitable_media_step(
                        "part",
                        lambda current=part_files: _post_bitable_media_once(
                            current_token,
                            "/drive/v1/medias/upload_part",
                            files=current,
                        ),
                        is_cancelled=cancelled,
                    )
                    attempts += used
                    confirmed_parts += 1
                    try:
                        persist_private(
                            "uploading_parts",
                            confirmed_parts=list(range(confirmed_parts)),
                        )
                    except Exception as exc:
                        raise _BitableMediaStepError(
                            "explicit_failed", "media_receipt_write_failed", 0
                        ) from exc
                data, used = _run_bitable_media_step(
                    "finish",
                    lambda: _post_bitable_media_once(
                        current_token,
                        "/drive/v1/medias/upload_finish",
                        json_body={"upload_id": upload_id, "block_num": expected_blocks},
                    ),
                    is_cancelled=cancelled,
                )
                attempts += used
                file_token = _valid_file_token(data.get("file_token"))
                if not file_token:
                    raise _BitableMediaStepError(
                        "uncertain", "media_finish_uncertain", 0
                    )
                try:
                    persist_private("token_confirmed", file_token=file_token)
                except Exception as exc:
                    raise _BitableMediaStepError(
                        "explicit_failed", "media_receipt_write_failed", 0
                    ) from exc
    except BitableMediaFileError as exc:
        return BitableMediaAttachmentReceipt(
            "explicit_failed", mode, attempts, confirmed_parts, exc.reason_code
        )
    except _BitableMediaStepError as exc:
        if private_state is not None:
            try:
                persist_private(exc.status, reason_code=exc.reason_code)
            except Exception:
                pass
        return BitableMediaAttachmentReceipt(
            exc.status,
            mode,
            attempts + exc.attempts,
            confirmed_parts,
            exc.reason_code,
        )

    attach_status, attach_attempts, reason_code, client_token = _attach_bitable_media_token(
        app_token,
        table_id,
        record_id,
        file_token,
        current_token,
        is_cancelled=cancelled,
        field_name=field_name,
    )
    if private_state is not None:
        try:
            persist_private(
                attach_status,
                file_token=file_token,
                client_token=client_token,
            )
        except Exception:
            if attach_status != "attached":
                reason_code = reason_code or "media_receipt_write_failed"
    return BitableMediaAttachmentReceipt(
        attach_status,
        mode,
        attempts + attach_attempts,
        confirmed_parts,
        reason_code,
        file_token=file_token,
        client_token=client_token,
    )


def _batch_records(
    app_token: str,
    table_id: str,
    records: list,
    *,
    action: str,
    is_cancelled: Callable[[], bool] | None = None,
) -> BitableBatchReceipt:
    token = _user_token()
    cancelled = is_cancelled or (lambda: False)
    succeeded = 0
    failed_indexes: list[int] = []
    unconfirmed_indexes: list[int] = []
    cancelled_indexes: list[int] = []
    client_tokens: list[str] = []
    record_ids: list[str] = []
    record_id_by_index: list[tuple[int, str]] = []
    for i in range(0, len(records), _BT_BATCH):
        chunk = records[i:i + _BT_BATCH]
        indexes = list(range(i, i + len(chunk)))
        if cancelled():
            cancelled_indexes.extend(range(i, len(records)))
            break
        client_token = str(uuid4())
        client_tokens.append(client_token)
        path = (
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/"
            f"batch_{action}?client_token={client_token}"
        )
        attempt = 0
        stop_after_uncertain = False
        while attempt < 2:
            attempt += 1
            try:
                with _bitable_table_write_lock(app_token, table_id):
                    data = _api(
                        "POST",
                        path,
                        token,
                        json={"records": chunk},
                    )
            except FeishuApiError as exc:
                if exc.code == 1061045 and attempt < 2:
                    time.sleep(0.2)
                    continue
                failed_indexes.extend(indexes)
                break
            except FeishuTransportError as exc:
                if exc.retryable_not_sent and attempt < 2:
                    time.sleep(0.2)
                    continue
                if exc.uncertain:
                    unconfirmed_indexes.extend(indexes)
                    stop_after_uncertain = True
                else:
                    failed_indexes.extend(indexes)
                break
            except Exception:  # noqa: BLE001 - mock/旧调用方的确定性失败
                failed_indexes.extend(indexes)
                break
            else:
                succeeded += len(chunk)
                for offset, item in enumerate(data.get("records", []) or []):
                    record_id = str((item or {}).get("record_id") or "")
                    if record_id:
                        record_ids.append(record_id)
                        if offset < len(indexes):
                            record_id_by_index.append((indexes[offset], record_id))
                break
        if stop_after_uncertain:
            next_index = i + len(chunk)
            cancelled_indexes.extend(range(next_index, len(records)))
            break
    return BitableBatchReceipt(
        attempted=len(records),
        succeeded=succeeded,
        failed=len(failed_indexes),
        failed_indexes=tuple(failed_indexes),
        unconfirmed_indexes=tuple(unconfirmed_indexes),
        cancelled_indexes=tuple(cancelled_indexes),
        client_tokens=tuple(client_tokens),
        record_ids=tuple(record_ids),
        record_id_by_index=tuple(record_id_by_index),
    )


def batch_create_records(
    app_token: str,
    table_id: str,
    records: list,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> BitableBatchReceipt:
    """新增记录；一个批次一个稳定 UUIDv4，明确可重试错误最多重试一次。"""

    return _batch_records(
        app_token,
        table_id,
        records,
        action="create",
        is_cancelled=is_cancelled,
    )


def batch_update_records(
    app_token: str,
    table_id: str,
    records: list,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> BitableBatchReceipt:
    """只更新已有 record_id；附件后补不得调用新增记录。"""

    for record in records:
        if not isinstance(record, dict) or not str(record.get("record_id") or ""):
            raise ValueError("附件后补缺少已有 record_id。")
    return _batch_records(
        app_token,
        table_id,
        records,
        action="update",
        is_cancelled=is_cancelled,
    )


def _export_issue(source, reason_code: str, retry_hint: str) -> BitableExportIssue:
    def clean(value: object, limit: int) -> str:
        return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]

    return BitableExportIssue(
        record_index=int(getattr(source, "record_index", -1)),
        stable_key=clean(getattr(source, "stable_key", ""), 160),
        title=clean(getattr(source, "title", ""), 120),
        reason_code=reason_code,
        retry_hint=retry_hint,
    )


def _uncertain_setup_receipt(
    *,
    stage: str,
    url: str,
    row_total: int,
    cover_candidate: int,
    cover_skipped: int,
    media_candidate: int,
    media_skipped: int,
    issues: Iterable[BitableExportIssue],
) -> BitableExportReceipt:
    frozen_issues = tuple(issues)
    return BitableExportReceipt(
        url=str(url or ""),
        row_total=row_total,
        row_success=0,
        row_failed=0,
        row_unconfirmed=row_total,
        row_cancelled=0,
        cover_candidate=cover_candidate,
        cover_success=0,
        cover_skipped=cover_skipped + cover_candidate,
        cover_failed=0,
        cover_uncertain=0,
        cancelled=False,
        media_candidate=media_candidate,
        media_skipped=media_skipped + media_candidate,
        outcome_uncertain=True,
        uncertain_stage=stage,
        issues=frozen_issues,
    )


def export_collection_to_new_bitable(
    space_id: str,
    parent_node: str,
    title: str,
    table_name: str,
    fields: list,
    records: Iterable[dict],
    cover_candidates: Iterable[object] = (),
    preflight_skipped: Iterable[object] = (),
    media_candidates: Iterable[object] = (),
    media_preflight_skipped: Iterable[object] = (),
    *,
    destination_kind: str = "wiki",
    existing_target: ExistingBitableTarget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> BitableExportReceipt:
    """新建表或追加到已验证表，分别结算行、封面和受管音视频。"""

    cancelled = is_cancelled or (lambda: False)
    frozen_records = tuple(records)
    frozen_covers = tuple(cover_candidates)
    frozen_media = tuple(media_candidates)
    cover_issues = [
        _export_issue(
            item,
            str(getattr(item, "reason_code", "cover_file_rejected")),
            str(getattr(item, "retry_hint", "检查本地封面后再试")),
        )
        for item in preflight_skipped
    ]
    media_issues = [
        _export_issue(
            item,
            str(getattr(item, "reason_code", "media_file_rejected")),
            str(getattr(item, "retry_hint", "重新下载所选素材后再导出")),
        )
        for item in media_preflight_skipped
    ]
    issues = [*cover_issues, *media_issues]
    row_total = len(frozen_records)
    if cancelled():
        return BitableExportReceipt(
            url="",
            row_total=row_total,
            row_success=0,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=row_total,
            cover_candidate=len(frozen_covers),
            cover_success=0,
            cover_skipped=len(cover_issues) + len(frozen_covers),
            cover_failed=0,
            cover_uncertain=0,
            cancelled=True,
            media_candidate=len(frozen_media),
            media_skipped=len(media_issues) + len(frozen_media),
            issues=tuple(issues),
        )

    token = _user_token()
    if existing_target is not None:
        bt = {
            "app_token": existing_target.app_token,
            "url": existing_target.url,
        }
        app_token = existing_target.app_token
        table_id = existing_target.table_id
        resolved_table_name = existing_target.table_name
        # 目标解析与真实上传之间仍可能被用户改表；写入边界再校验一次。
        _ensure_existing_bitable_fields_compatible(
            _list_bitable_fields(app_token, table_id, token),
            fields,
        )
    else:
        resolved_table_name = table_name
        try:
            if destination_kind == "drive":
                bt = create_drive_bitable(title)
            elif destination_kind == "wiki":
                bt = create_wiki_bitable(space_id, parent_node, title)
            else:
                raise ValueError("未知的飞书新建表格目的地。")
        except FeishuTransportError as exc:
            if not exc.uncertain:
                raise
            return _uncertain_setup_receipt(
                stage=exc.stage or (
                    "drive_bitable_create"
                    if destination_kind == "drive"
                    else "wiki_node_create"
                ),
                url=exc.known_url,
                row_total=row_total,
                cover_candidate=len(frozen_covers),
                cover_skipped=len(cover_issues),
                media_candidate=len(frozen_media),
                media_skipped=len(media_issues),
                issues=issues,
            )
        app_token = bt["app_token"]
        known_url = str(bt.get("url") or "")
        try:
            table_id = _prepare_initial_bitable_table(
                app_token,
                table_name,
                fields,
                token,
            )
        except Exception:  # noqa: BLE001 - Base 已创建，保留已知链接供核对
            return _uncertain_setup_receipt(
                stage="table_prepare",
                url=known_url,
                row_total=row_total,
                cover_candidate=len(frozen_covers),
                cover_skipped=len(cover_issues),
                media_candidate=len(frozen_media),
                media_skipped=len(media_issues),
                issues=issues,
            )

    media_chain_ready = not frozen_media
    media_failed = 0
    media_uncertain = 0
    media_skipped = len(media_issues)
    if frozen_media:
        field_receipt = ensure_bitable_media_field(
            app_token,
            table_id,
            confirmed=existing_target is None,
            token=token,
        )
        media_chain_ready = field_receipt.status == "ready"
        if not media_chain_ready:
            reason = field_receipt.reason_code or "media_field_create_failed"
            if field_receipt.status == "uncertain":
                media_uncertain = len(frozen_media)
                hint = "保留现有表格，先核对附件列再决定是否恢复"
            else:
                media_failed = len(frozen_media)
                hint = "目标表附件列冲突或创建失败；Lulu 没有修改同名列"
            issues.extend(_export_issue(item, reason, hint) for item in frozen_media)

    cover_tokens: dict[int, str] = {}
    cover_success = 0
    cover_failed = 0
    cover_uncertain = 0
    cover_skipped = len(cover_issues)
    cancelled_midway = False
    for offset, candidate in enumerate(frozen_covers):
        if cancelled():
            cancelled_midway = True
            remaining = frozen_covers[offset:]
            cover_skipped += len(remaining)
            issues.extend(
                _export_issue(
                    item,
                    "cover_upload_cancelled",
                    "已停止后续上传；已完成内容不会自动删除",
                )
                for item in remaining
            )
            break
        receipt = upload_bitable_image(
            app_token,
            str(getattr(candidate, "file_name", "")),
            str(getattr(candidate, "mime_type", "")),
            bytes(getattr(candidate, "data", b"")),
            token=token,
        )
        record_index = int(getattr(candidate, "record_index", -1))
        if receipt.status == "uploaded" and 0 <= record_index < row_total:
            cover_tokens[record_index] = receipt.file_token
            cover_success += 1
            continue
        if receipt.status == "upload_uncertain":
            cover_uncertain += 1
            issues.append(
                _export_issue(
                    candidate,
                    receipt.reason_code,
                    "为避免重复附件，本次不会自动重传",
                )
            )
            remaining = frozen_covers[offset + 1:]
            cover_skipped += len(remaining)
            issues.extend(
                _export_issue(
                    item,
                    "cover_upload_not_attempted_after_uncertain",
                    "先打开现有表格确认，再决定是否重新导出",
                )
                for item in remaining
            )
            break
        cover_failed += 1
        issues.append(
            _export_issue(
                candidate,
                receipt.reason_code,
                "检查封面文件或飞书权限后重新导出",
            )
        )

    if cancelled_midway or cancelled():
        return BitableExportReceipt(
            url=bt["url"],
            row_total=row_total,
            row_success=0,
            row_failed=0,
            row_unconfirmed=0,
            row_cancelled=row_total,
            cover_candidate=len(frozen_covers),
            cover_success=cover_success,
            cover_skipped=cover_skipped,
            cover_failed=cover_failed,
            cover_uncertain=cover_uncertain,
            cancelled=True,
            media_candidate=len(frozen_media),
            media_failed=media_failed,
            media_uncertain=media_uncertain,
            media_skipped=(
                media_skipped
                if not media_chain_ready
                else media_skipped + len(frozen_media)
            ),
            issues=tuple(issues),
            destination_app_token=app_token,
            destination_table_id=table_id,
            destination_table_name=resolved_table_name,
        )

    prepared_records: list[dict] = []
    for record_index, record in enumerate(frozen_records):
        fields_payload = dict((record or {}).get("fields") or {})
        file_token = cover_tokens.get(record_index, "")
        if file_token:
            fields_payload["封面"] = [{"file_token": file_token}]
        else:
            fields_payload.pop("封面", None)
        prepared_records.append({"fields": fields_payload})

    batch = batch_create_records(
        app_token,
        table_id,
        prepared_records,
        is_cancelled=cancelled,
    )
    record_ids = dict(batch.record_id_by_index)
    if not record_ids and len(batch.record_ids) == row_total:
        record_ids = dict(enumerate(batch.record_ids))

    media_success = 0
    cancelled_media = False
    deferred_media: list[tuple[object, str]] = []

    def can_restart_media(receipt: BitableMediaAttachmentReceipt) -> bool:
        return (
            receipt.mode == "multipart"
            and receipt.status in {"uncertain", "explicit_failed"}
            and receipt.reason_code in _BITABLE_MEDIA_SAFE_RESTART_REASONS
        )

    def settle_media(candidate: object, receipt: BitableMediaAttachmentReceipt) -> None:
        nonlocal media_success, media_failed, media_uncertain, media_skipped
        nonlocal cancelled_media
        if receipt.status == "attached":
            media_success += 1
            return
        if receipt.status == "uncertain":
            media_uncertain += 1
            hint = "保留现有表格，先查看对应作品再决定是否恢复"
        elif receipt.status == "cancelled":
            media_skipped += 1
            cancelled_media = True
            hint = "已停止后续上传；已经写入的内容会保留"
        else:
            media_failed += 1
            hint = "重新下载受管素材或检查飞书权限后再导出"
        issues.append(_export_issue(candidate, receipt.reason_code, hint))

    def mark_media_not_attempted(candidates: list[object]) -> None:
        nonlocal media_skipped, cancelled_media
        if not candidates:
            return
        cancelled_media = True
        media_skipped += len(candidates)
        issues.extend(
            _export_issue(
                item,
                "media_upload_not_attempted_after_cancel",
                "已停止后续上传；已经写入的内容会保留",
            )
            for item in candidates
        )

    if media_chain_ready:
        for offset, candidate in enumerate(frozen_media):
            if cancelled():
                pending = [*frozen_media[offset:], *(item for item, _ in deferred_media)]
                mark_media_not_attempted(pending)
                deferred_media.clear()
                break
            record_index = int(getattr(candidate, "record_index", -1))
            record_id = record_ids.get(record_index, "")
            if not record_id:
                media_skipped += 1
                issues.append(
                    _export_issue(
                        candidate,
                        "media_record_not_confirmed",
                        "该作品行尚未确认，音视频没有发送",
                    )
                )
                continue
            receipt = upload_and_attach_bitable_media(
                app_token,
                table_id,
                record_id,
                candidate,
                token=token,
                connection_unstable=True,
                is_cancelled=cancelled,
            )
            if can_restart_media(receipt):
                deferred_media.append((candidate, record_id))
                continue
            settle_media(candidate, receipt)
            if receipt.status == "cancelled":
                pending = [
                    *frozen_media[offset + 1:],
                    *(item for item, _ in deferred_media),
                ]
                mark_media_not_attempted(pending)
                deferred_media.clear()
                break

        for offset, (candidate, record_id) in enumerate(deferred_media):
            if cancelled():
                mark_media_not_attempted(
                    [item for item, _ in deferred_media[offset:]]
                )
                break
            receipt = upload_and_attach_bitable_media(
                app_token,
                table_id,
                record_id,
                candidate,
                token=token,
                connection_unstable=True,
                is_cancelled=cancelled,
            )
            settle_media(candidate, receipt)
            if receipt.status == "cancelled":
                mark_media_not_attempted(
                    [item for item, _ in deferred_media[offset + 1:]]
                )
                break

    return BitableExportReceipt(
        url=bt["url"] or "已创建多维表格",
        row_total=row_total,
        row_success=batch.succeeded,
        row_failed=batch.failed,
        row_unconfirmed=len(batch.unconfirmed_indexes),
        row_cancelled=len(batch.cancelled_indexes),
        cover_candidate=len(frozen_covers),
        cover_success=cover_success,
        cover_skipped=cover_skipped,
        cover_failed=cover_failed,
        cover_uncertain=cover_uncertain,
        cancelled=bool(batch.cancelled_indexes) or cancelled_media,
        media_candidate=len(frozen_media),
        media_success=media_success,
        media_skipped=media_skipped,
        media_failed=media_failed,
        media_uncertain=media_uncertain,
        issues=tuple(issues),
        destination_app_token=app_token,
        destination_table_id=table_id,
        destination_table_name=resolved_table_name,
    )


def export_to_new_bitable(space_id: str, parent_node: str, title: str,
                          table_name: str, fields: list, records: list) -> str:
    """知识库下新建多维表格，复用自带空表后批量写入。"""
    token = _user_token()
    bt = create_wiki_bitable(space_id, parent_node, title)
    app_token = bt["app_token"]
    table_id = _prepare_initial_bitable_table(
        app_token,
        table_name,
        fields,
        token,
    )
    if records:
        receipt = batch_create_records(app_token, table_id, records)
        if not receipt.complete:
            raise BitablePartialWriteError(receipt)
    return bt["url"] or "已创建多维表格"
