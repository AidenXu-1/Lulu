"""Explicit connection snapshots; credentials never leave the native/Core boundary."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .. import secrets
from .store import WorkspaceStore, identity, encode, now
from hashlib import sha256


def _directory_key(config):
    return 'model-directory:' + sha256(encode([config[k] for k in
        ('provider', 'mode', 'base', 'protocol', 'credential_id')]).encode()).hexdigest()


def _bases(provider, mode):
    return {mode['base'].rstrip('/'), *(r['base'].rstrip('/') for r in mode.get('regions', []))}


def catalog():
    data=json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
    for provider in data['providers']:
        provider['discovery']=provider['id'] in {'deepseek','kimi','mimo','openrouter','custom'}
    return data


def validate_url(value):
    if not isinstance(value,str):
        raise ValueError('接口地址无效')
    url = urlparse(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("接口需要不带密钥、查询参数或用户名的 HTTPS 地址")
    return value.rstrip("/")


def connections(store=None):
    return (store or WorkspaceStore()).preference("connections") or []


def available_connections(store=None):
    """New-chat choices; deselected snapshots remain readable by old chats."""
    store = store or WorkspaceStore()
    groups = store.preference('model-connection-groups') or {}
    managed = set(store.preference('model-managed-connections') or [])
    enabled = {cid for ids in groups.values() for cid in ids}
    removed = set(store.preference('model-removed-credentials') or [])
    return [c for c in connections(store) if c['credential_id'] not in removed
            and (c['id'] not in managed or c['id'] in enabled)]


def connection(connection_id, store=None):
    identity(connection_id)
    value = next((c for c in connections(store) if c["id"] == connection_id), None)
    if value is None:
        raise ValueError("请先选择并设置云端模型")
    return value


def validated_connection(value, store=None):
    """Build one immutable snapshot without changing preferences."""
    store = store or WorkspaceStore()
    allowed = {"provider", "mode", "base", "protocol", "model", "window", "max_output", "threshold", "credential_id", "label"}
    if type(value) is not dict or set(value) != allowed:
        raise ValueError("模型连接字段无效")
    data = catalog()
    provider = next((p for p in data["providers"] if p["id"] == value["provider"]), None)
    if not provider or value["mode"] not in {"api", *(p["id"] for p in provider["plans"])}:
        raise ValueError("供应商或接入方式无效")
    # Coding subscriptions whose documented permitted clients exclude Lulu must
    # remain discoverable as information, not as a disguised general-purpose API.
    mode = next((p for p in provider['plans'] if p['id'] == value['mode']), provider)
    if mode.get('available') is False:
        raise ValueError(mode['unavailable_reason'])
    if value["protocol"] not in {"openai", "anthropic"}:
        raise ValueError("接口协议无效")
    if not isinstance(value["model"], str) or not 0 < len(value["model"].strip()) < 200:
        raise ValueError("请选择准确的模型 ID")
    if type(value["window"]) is not int or not 8192 <= value["window"] <= 10_000_000:
        raise ValueError("上下文容量尚未核实")
    if type(value["max_output"]) is not int or not 256 <= value["max_output"] <= min(value["window"] // 2, 32768):
        raise ValueError("回答空间无效")
    if value["threshold"] not in (0.7, 0.8, 0.85):
        raise ValueError("自动压缩比例无效")
    credential_id = identity(value["credential_id"])
    if not credential_id.startswith("workspace-key-"):
        raise ValueError("请选择工作区专用的安全凭证")
    output = dict(value, base=validate_url(value["base"]), id="connection-" + uuid4().hex)
    known = next((m for m in data['models'].get(value['provider']+':'+value['mode'],data['models'].get(value['provider'],[]))
                  if m['id']==value['model']),None)
    fetched = store.preference(_directory_key(output)) or {}
    discovered = next((m for m in fetched.get('models', []) if m['id'] == value['model']), None)
    limit = discovered or (known if output['base'] in _bases(provider, mode) else None)
    if limit:
        if limit.get('window') and value['window']>limit['window']:
            raise ValueError('填写的容量超过当前官方目录，请先获取模型或核实接口')
        if limit.get('max_output') and value['max_output'] > limit['max_output']:
            raise ValueError('预留回答空间超过模型支持的输出长度')
        output['max_input']=limit.get('max_input')
        output['tools_supported']=limit.get('tools_supported', True)
        output['catalog_source']=(output['base']+'/models') if discovered else (known.get('source') or provider['docs'])
        output['catalog_checked_at']=fetched.get('fetched_at') if discovered else data.get('checked_at','2026-09-22')
    output["label"] = str(value["label"] or value["model"])[:100]
    return output


def save_connection(value, store=None):
    store = store or WorkspaceStore()
    output = validated_connection(value, store)
    allowed = set(value)
    # Immutable snapshots let an existing chat keep its chosen endpoint/model.
    from .. import library
    with library._transaction():
        existing = connections(store)
        # Retrying the same save must not create duplicate choices. Modified
        # configurations still receive a new snapshot; active chats stay intact.
        match = next((c for c in existing if all(c.get(k) == output[k] for k in allowed)), None)
        if match:
            return match
        store.preference("connections", existing + [output])
    return output


def key_for(config):
    key = secrets.get_key(config["credential_id"], require_access=True)
    if not key:
        raise ValueError("尚未设置此连接的 API 密钥，请到云端模型设置添加")
    return key


def discover(connection_id=None, store=None, value=None):
    import httpx
    store = store or WorkspaceStore()
    if connection_id:
        config = connection(connection_id, store)
    else:
        if type(value) is not dict or set(value)!={'provider','mode','base','protocol','credential_id'}:
            raise ValueError('模型目录请求无效')
        config=dict(value,base=validate_url(value['base']))
        if not identity(config['credential_id']).startswith('workspace-key-'):
            raise ValueError('请先添加此接口的安全凭证')
    provider=next((p for p in catalog()['providers'] if p['id']==config['provider']),None)
    if not provider or not provider['discovery'] or config['mode']!='api':
        raise ValueError('此接入方式尚无已确认的模型列表接口，请使用官方预设目录')
    if config['provider']!='custom' and config['base'].rstrip('/') not in _bases(provider, provider):
        raise ValueError('此地址的列表接口未核实；可将连接设为自定义后主动获取')
    if config["protocol"] != "openai":
        raise ValueError("此接口暂不支持自动拉取，请使用官方预设目录")
    try:
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            response = client.get(config["base"] + "/models", headers={"Authorization": "Bearer " + key_for(config)})
    except httpx.HTTPError:
        raise RuntimeError('模型目录暂时无法获取，已保留填写内容，可使用官方预设') from None
    if response.status_code != 200:
        raise RuntimeError(f"模型目录请求返回 HTTP {response.status_code}，现有设置保持不变")
    if len(response.content) > 16 * 1024 * 1024:
        raise ValueError("模型目录响应过大")
    known = {m["id"]: m for m in catalog()["models"].get(config["provider"], [])}
    rows = []
    try:
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ValueError()
    except ValueError:
        raise RuntimeError('模型目录格式无法识别，原配置已保留') from None
    for model in data['data']:
        if not isinstance(model,dict):
            continue
        mid = model.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        modalities=(model.get('architecture') or {}).get('output_modalities')
        if modalities and 'text' not in modalities:
            continue
        if config['provider']=='mimo' and mid not in known:
            continue
        ctx=model.get('context')
        length = model.get("context_length") or (ctx.get('length') if type(ctx) is dict else None) or known.get(mid, {}).get("window")
        top = model.get('top_provider') or {}
        output = top.get('max_completion_tokens') or known.get(mid, {}).get('max_output')
        row={"id": mid, "label": model.get("name") or mid,
             "window": length if type(length) is int and 8192<=length<=10_000_000 else None,
             "max_input":known.get(mid,{}).get('max_input'),
             "max_output":output if type(output) is int and output>0 else None}
        if isinstance(model.get('supported_parameters'),list):
            row['tools_supported']='tools' in model['supported_parameters']
        rows.append(row)
    store.preference(_directory_key(config), {'fetched_at':now(), 'models':rows})
    return rows
