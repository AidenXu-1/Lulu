"""API Key 安全存储:封装 keyring(跨平台,业务无感)。

keyring 在不同系统自动用各自的原生安全存储:
- macOS  → 钥匙串 Keychain
- Windows → 凭据管理器 Credential Manager
- Linux  → Secret Service / KWallet

调用方只用 get_key/set_key/delete_key,日后换后端不影响业务。
keyring 不可用时降级为「进程内内存态 + 告警」,绝不让存取 key 崩掉主流程。
绝不打印 / 日志 / 外传 key 本身。
"""

from __future__ import annotations

import json
import logging
import sys
import threading

_SERVICE = "Lulu"
_log = logging.getLogger(__name__)
_memory: dict[str, str] = {}  # keyring 不可用时的临时降级存储(本进程内)
_cache: dict[str, str | None] = {}  # 本次运行已读取的凭据；不写入磁盘
_lock = threading.RLock()


def _backend():
    try:
        if sys.platform == "darwin":
            # keyring 的 macOS set_password 会先删旧条目再新增，连同用户通过
            # “始终允许”保存的条目 ACL 一起重置。复用授权存储已经验证的
            # SecItemUpdate 后端：已有条目原位更新，缺失时才创建。
            from .license.store import _MacOSExplicitKeyring

            return _MacOSExplicitKeyring()
        import keyring  # 懒导入,未装也不影响其它功能

        return keyring
    except Exception:  # noqa: BLE001
        return None


def get_key(provider: str, *, require_access: bool = False) -> str | None:
    if provider in _cache:
        return _cache[provider]
    with _lock:
        if provider in _cache:
            return _cache[provider]
        return _read_key(provider, require_access=require_access)


def configuration_loaded(provider: str) -> bool:
    """被动 UI 只查询本次运行是否已读取，绝不访问钥匙串。"""
    return provider in _cache


def cached_public_config(provider: str) -> dict | None:
    """Read already-loaded ASR metadata only; never opens the system key store."""
    with _lock:
        if provider not in _cache:
            return None
        raw = _cache[provider]
        try:
            value = json.loads(raw) if raw else {}
            if type(value) is not dict:
                value = {'api_key': raw}
        except (ValueError, TypeError):
            value = {'api_key': raw}
        return {**{name: value.get(name, '') for name in ('base_url', 'model')},
                'configured': bool(value.get('api_key'))}


def _read_key(provider: str, *, require_access: bool) -> str | None:
    kr = _backend()
    if kr is not None:
        try:
            v = kr.get_password(_SERVICE, provider)
            _cache[provider] = v or _memory.get(provider)
            return _cache[provider]
        except Exception as exc:  # noqa: BLE001
            _log.warning("keyring 读取失败,降级内存:%s", type(exc).__name__)
            if require_access:
                raise RuntimeError("CREDENTIAL_READ_UNAVAILABLE") from None
    elif require_access:
        raise RuntimeError("CREDENTIAL_READ_UNAVAILABLE")
    return _memory.get(provider)


def set_key(provider: str, key: str) -> None:
    with _lock:
        _write_key(provider, key)


def _write_key(provider: str, key: str) -> None:
    key = (key or "").strip()
    kr = _backend()
    if kr is not None:
        try:
            if key:
                kr.set_password(_SERVICE, provider, key)
            else:
                try:
                    kr.delete_password(_SERVICE, provider)
                except Exception:  # noqa: BLE001
                    pass
            _memory.pop(provider, None)
            _cache[provider] = key or None
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("keyring 写入失败,降级内存:%s", type(exc).__name__)
    _cache.pop(provider, None)
    if key:
        _memory[provider] = key
    else:
        _memory.pop(provider, None)


def delete_key(provider: str) -> None:
    set_key(provider, "")


def set_key_verified(provider: str, key: str) -> None:
    """Explicit settings action: require durable storage and confirm by readback.

    Unlike the compatibility writer, a settings form must never report that a
    memory-only fallback survived a restart. Errors contain no credential data.
    """
    with _lock:
        kr = _backend()
        if kr is None:
            raise RuntimeError('系统安全存储暂不可用，未保存密钥')
        value = (key or '').strip()
        try:
            if value:
                kr.set_password(_SERVICE, provider, value)
            elif kr.get_password(_SERVICE, provider) is not None:
                kr.delete_password(_SERVICE, provider)
            if (kr.get_password(_SERVICE, provider) or '') != value:
                raise RuntimeError('readback mismatch')
        except Exception:
            _cache.pop(provider, None)
            raise RuntimeError('系统安全存储未能确认保存，请重试；原连接保持不变') from None
        _memory.pop(provider, None)
        _cache[provider] = value or None


def has_key(provider: str) -> bool:
    return bool(get_key(provider))


# ---- 多字段配置:把每家的整份配置(api_key / base_url / appid / token …)以 JSON 存一条 ----
def get_config(provider: str, *, require_access: bool = False) -> dict:
    raw = get_key(provider, require_access=require_access)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"api_key": raw}
    except Exception:  # noqa: BLE001
        # 兼容老格式(早期直接存的纯 key 字符串)
        return {"api_key": raw}


def set_config(provider: str, config: dict) -> None:
    clean = {k: v for k, v in (config or {}).items() if str(v or "").strip()}
    set_key(provider, json.dumps(clean, ensure_ascii=False) if clean else "")
