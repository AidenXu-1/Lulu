"""Cloud settings shared by the model page, without passive credential access."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import secrets
from .agent_workspace import providers as workspace
from .agent_workspace.store import WorkspaceStore, encode, identity, now
from .engines.providers import PRESETS

_FIELDS = frozenset({'purpose', 'provider', 'mode', 'base', 'protocol', 'region',
                     'credentialId', 'key', 'modelIds', 'defaultModel', 'label', 'modelId'})
_MAX_RESPONSE = 2 * 1024 * 1024
_PUBLIC = Path(__file__).parents[1] / 'ui' / 'model_settings' / 'public.json'


class Service:
    def __init__(self, cfg, *, store=None):
        self.cfg = cfg
        self._store = store
        self._credentials = {}  # Newly saved opaque handles, bound to one form.
        self._save_lock = threading.RLock()

    @property
    def store(self):
        if self._store is None:
            self._store = WorkspaceStore()
        return self._store

    def snapshot(self):
        public = json.loads(_PUBLIC.read_text(encoding='utf-8'))
        catalog = workspace.catalog()
        known = {p['id']: p for p in catalog['providers']}
        choices = []
        for item in public['providers']:
            authoritative = known.get(item['id'])
            if authoritative is None:
                continue
            item = deepcopy(item)
            previews = {m['id']: m for m in item['modes']}
            modes = []
            for mode in [dict(authoritative, id='api'), *authoritative['plans']]:
                view = deepcopy(previews.get(mode['id'], {}))
                regions = mode.get('regions', [])
                view.update(id=mode['id'], label=view.get('label', mode.get('name', '按量计费 API')),
                            base=[r['base'] for r in regions] if regions else mode['base'],
                            protocol=mode['protocol'], available=mode.get('available', True),
                            unavailableReason=mode.get('unavailable_reason', ''),
                            catalog=(item['id'] + ':' + mode['id'] if item['id'] + ':' + mode['id'] in catalog['models'] else item['id']),
                            regions=[r['name'] for r in regions], keys=view.get('keys', ''))
                modes.append(view)
            item['modes'] = modes
            # Regional choices follow the live adapter, never an unimplemented preview.
            item['regions'] = [r['name'] for r in authoritative.get('regions', [])]
            choices.append(item)
        cloud = deepcopy(self.store.preference('model-settings-cloud-configs') or {})
        for view in public['asrProviders']:
            pid = 'custom_openai' if view['id'] == 'custom_asr' else view['id']
            cloud.setdefault(view['id'], dict(provider=view['id'], credentialId=pid,
                                             modelId=PRESETS[pid].default_model,
                                             configured=None, active=self.cfg.engine_name == pid))
            cloud[view['id']]['active'] = self.cfg.engine_name == pid
            if (pid == 'custom_openai' and cloud[view['id']].get('configured') is not False
                    and not cloud[view['id']].get('base')):
                cached = secrets.cached_public_config(pid)
                try:
                    cloud[view['id']].update(self._public_custom_asr(cached))
                except ValueError:
                    cloud[view['id']].update(requiresImport=True, configured=None)
        removed = set(self.store.preference('model-removed-credentials') or [])
        return dict(providers=choices, asrProviders=public['asrProviders'], catalogue=catalog['models'],
                    connections=workspace.available_connections(self.store),
                    storedConnections=[c for c in workspace.connections(self.store) if c['credential_id'] not in removed], cloudConfigs=cloud,
                    defaultConnectionId=self.store.preference('default-connection') or '')

    def execute(self, operation, args):
        if operation not in {'test_connection', 'discover_models', 'save_connection', 'load_saved_configuration', 'remove_credential'}:
            raise ValueError('此连接操作不可用')
        if type(args) is not dict or set(args) - _FIELDS:
            raise ValueError('连接设置字段无效')
        if operation == 'load_saved_configuration':
            with self._save_lock:
                return self._load_saved_configuration(args)
        if operation == 'remove_credential':
            with self._save_lock:
                return self._remove_credential(args)
        config = self._config(args)
        if operation == 'save_connection':
            with self._save_lock:
                return self._save_asr(config, args) if config['purpose'] == 'extract' else self._save_assistant(config, args)
        return self._check(config, args, discover=operation == 'discover_models')

    def _remove_credential(self, args):
        """Delete the selected system credential; retain old chat descriptions."""
        if set(args) - {'purpose', 'provider', 'mode', 'base', 'protocol', 'region', 'credentialId'}:
            raise ValueError('密钥删除字段无效')
        purpose = args.get('purpose')
        pid = args.get('provider')
        cid = args.get('credentialId')
        region = args.get('region', 0)
        if type(region) is not int or region < 0:
            raise ValueError('账号地区无效')
        old_engine = self.cfg.engine_name
        switched = False
        if purpose == 'extract':
            pid = 'custom_openai' if pid == 'custom_asr' else pid
            preset = PRESETS.get(pid)
            if (not preset or not preset.needs_key or args.get('mode', 'api') != 'api'
                    or args.get('region', 0) != 0 or cid != pid):
                raise ValueError('请选择此转录服务自己的密钥')
            if old_engine == pid:
                from ..config import DEFAULT_ENGINE
                self.cfg.engine_name = DEFAULT_ENGINE
                try:
                    saved = self.cfg.save()
                except Exception:
                    saved = False
                if not saved:
                    self.cfg.engine_name = old_engine
                    raise RuntimeError('本地转录设置未能保存，密钥未删除')
                switched = True
        elif purpose == 'assistant':
            if not identity(cid).startswith('workspace-key-'):
                raise ValueError('请选择已保存的密钥')
            refs = [c for c in workspace.connections(self.store) if c['credential_id'] == cid]
            fields = ('provider', 'mode', 'base', 'protocol')
            same = lambda c: all(c.get(k) == args.get(k) for k in fields)
            if refs:
                if not all(same(c) for c in refs):
                    raise ValueError('此密钥不属于当前服务连接')
            else:
                config = self._config(args)
                if self._credentials.get(cid) != self._binding(config):
                    raise ValueError('此密钥不属于当前服务连接')
        else:
            raise ValueError('请选择模型用途')
        try:
            # The compatibility delete_key may silently fall back to memory.
            # The verified writer with an empty value confirms actual removal.
            secrets.set_key_verified(cid, '')
        except Exception:
            if switched:
                self.cfg.engine_name = old_engine
                try:
                    restored = self.cfg.save()
                except Exception:
                    restored = False
                if not restored:
                    raise RuntimeError('密钥删除未确认，默认转录设置也未能恢复，请检查设置后重试') from None
            raise RuntimeError('系统安全存储未能确认删除，原连接保留，请重试') from None
        self._credentials.pop(cid, None)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                def read(key, fallback):
                    row = db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
                    return json.loads(row[0]) if row else fallback
                changes = {}
                if purpose == 'assistant':
                    removed = set(read('model-removed-credentials', [])) | {cid}
                    all_connections = read('connections', [])
                    ids = {c['id'] for c in all_connections if c['credential_id'] == cid}
                    groups = {k: [i for i in values if i not in ids] for k, values in read('model-connection-groups', {}).items()}
                    managed = set(read('model-managed-connections', [])) | ids
                    default = read('default-connection', '')
                    if default in ids:
                        enabled = {i for values in groups.values() for i in values}
                        default = next((c['id'] for c in all_connections if c['credential_id'] not in removed
                                        and (c['id'] not in managed or c['id'] in enabled)), '')
                    changes.update({'model-removed-credentials': sorted(removed), 'model-connection-groups': groups,
                                    'model-managed-connections': sorted(managed), 'default-connection': default})
                    for c in all_connections:
                        if c['credential_id'] == cid:
                            db.execute('DELETE FROM preferences WHERE key=?', (workspace._directory_key(c),))
                else:
                    public_id = 'custom_asr' if pid == 'custom_openai' else pid
                    cloud = read('model-settings-cloud-configs', {})
                    cloud[public_id] = dict(cloud.get(public_id, {}), provider=public_id, credentialId=pid,
                                            configured=False, hasCredential=False, active=False, requiresImport=False)
                    changes['model-settings-cloud-configs'] = cloud
                for name, value in changes.items():
                    db.execute('INSERT INTO preferences(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (name, encode(value)))
        except Exception:
            raise RuntimeError('密钥已从本机移除，但连接列表暂未更新，请重试删除') from None
        public = self.snapshot()
        return {k: public[k] for k in ('connections', 'storedConnections', 'cloudConfigs', 'defaultConnectionId')}

    @staticmethod
    def _public_custom_asr(value):
        if type(value) is not dict:
            raise ValueError('请点击读取已有连接，或填写新的连接')
        try:
            base = workspace.validate_url(value.get('base_url', ''))
        except ValueError:
            raise ValueError('原连接的地址无法安全读取，请重新填写接口地址和模型 ID；原密钥保留') from None
        model = value.get('model')
        if not isinstance(model, str) or not 0 < len(model.strip()) < 200 or any(ch in model for ch in '\r\n\0'):
            raise ValueError('原连接未记录完整的模型 ID，请重新填写；原密钥保留')
        return dict(provider='custom_asr', credentialId='custom_openai', base=base,
                    modelId=model.strip(), configured=bool(value.get('api_key') or value.get('configured')),
                    requiresImport=False)

    def _load_saved_configuration(self, args):
        # Legacy endpoint/model/key share one encrypted record. Only this explicit
        # action opens it; the public migration never writes the credential itself.
        if (args.get('purpose') != 'extract' or args.get('provider') != 'custom_asr'
                or args.get('mode', 'api') != 'api' or args.get('region', 0) != 0):
            raise ValueError('此连接不需要导入旧配置')
        try:
            legacy = secrets.get_config('custom_openai', require_access=True)
        except RuntimeError:
            raise RuntimeError('暂时无法读取已有连接，原配置保留，请稍后重试') from None
        value = self._public_custom_asr(legacy)
        value['active'] = self.cfg.engine_name == 'custom_openai'
        cloud = deepcopy(self.store.preference('model-settings-cloud-configs') or {})
        cloud['custom_asr'] = value
        self.store.preference('model-settings-cloud-configs', cloud)
        return dict(credentialId='custom_openai', cloudConfigs=cloud)

    def key_url(self, args):
        """Return only a packaged account/key page, never an arbitrary UI URL."""
        if type(args) is not dict or set(args) - {'purpose', 'provider', 'mode', 'region'}:
            raise ValueError('密钥页面参数无效')
        region = args.get('region', 0)
        if type(region) is not int or region < 0:
            raise ValueError('服务地区无效')
        public = json.loads(_PUBLIC.read_text(encoding='utf-8'))
        if args.get('purpose') == 'extract':
            provider = next((p for p in public['asrProviders'] if p['id'] == args.get('provider')), None)
            if provider is None or region or args.get('mode', 'api') != 'api':
                raise ValueError('转录服务无效')
            value = provider.get('keys', '')
        elif args.get('purpose') == 'assistant':
            provider = next((p for p in public['providers'] if p['id'] == args.get('provider')), None)
            authoritative = next((p for p in workspace.catalog()['providers'] if p['id'] == args.get('provider')), None)
            if provider is None or authoritative is None:
                raise ValueError('服务商无效')
            mode_id = args.get('mode') or 'api'
            actual = authoritative if mode_id == 'api' else next((m for m in authoritative['plans'] if m['id'] == mode_id), None)
            if actual is None or region >= max(1, len(actual.get('regions', []))):
                raise ValueError('服务类型或地区无效')
            mode = next((m for m in provider['modes'] if m['id'] == mode_id), {})
            value = mode.get('keys', '')
        else:
            raise ValueError('模型用途无效')
        if isinstance(value, list):
            value = value[region] if region < len(value) else ''
        if not isinstance(value, str) or not value:
            raise ValueError('此服务暂无已确认的密钥创建页面，请前往服务商账户设置')
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('密钥页面地址未通过检查')
        return value

    def _config(self, args):
        purpose = args.get('purpose')
        if purpose not in {'extract', 'assistant'}:
            raise ValueError('请选择模型用途')
        region = args.get('region', 0)
        if type(region) is not int or region < 0:
            raise ValueError('服务地区无效')
        if purpose == 'extract':
            pid = 'custom_openai' if args.get('provider') == 'custom_asr' else args.get('provider')
            preset = PRESETS.get(pid)
            if not preset or not preset.needs_key or region or args.get('mode', 'api') != 'api':
                raise ValueError('转录服务设置无效')
            base = args.get('base', '') if pid == 'custom_openai' else preset.base_url
            if base:
                base = workspace.validate_url(base)
            model = str(args.get('modelId') or preset.default_model).strip()
            if not model or len(model) >= 200:
                raise ValueError('请填写服务商提供的转录模型 ID')
            if pid != 'custom_openai' and model != preset.default_model:
                raise ValueError('此转录服务使用已适配的固定模型')
            return dict(purpose=purpose, provider=pid, mode='api', base=base, protocol='openai',
                        region=0, model=model, kind=preset.kind)
        provider = next((p for p in workspace.catalog()['providers'] if p['id'] == args.get('provider')), None)
        if not provider:
            raise ValueError('请选择服务商')
        mode_id = args.get('mode') or 'api'
        if provider['id'] == 'custom' and mode_id == 'custom':
            mode_id = 'api'
        mode = provider if mode_id == 'api' else next((m for m in provider['plans'] if m['id'] == mode_id), None)
        if mode is None:
            raise ValueError('此服务类型不可用')
        if mode.get('available') is False:
            raise ValueError(mode['unavailable_reason'])
        regions = mode.get('regions', [])
        if region >= max(1, len(regions)):
            raise ValueError('此服务地区暂未适配')
        expected = regions[region]['base'] if regions else mode['base']
        if provider['id'] == 'custom':
            base = workspace.validate_url(args.get('base', ''))
            protocol = args.get('protocol') or 'openai'
            if protocol not in {'openai', 'anthropic'}:
                raise ValueError('请选择支持的接口协议')
        else:
            base = workspace.validate_url(expected)
            if args.get('base') and workspace.validate_url(args['base']) != base:
                raise ValueError('请使用所选服务和地区的官方接口，自定义地址请另选自定义服务')
            protocol = mode['protocol']
            if args.get('protocol') and args['protocol'] != protocol:
                raise ValueError('此服务的接口协议由 Lulu 自动匹配')
        return dict(purpose=purpose, provider=provider['id'], mode=mode_id, base=base,
                    protocol=protocol, region=region)

    @staticmethod
    def _binding(config):
        return tuple(config[k] for k in ('purpose', 'provider', 'mode', 'base', 'protocol', 'region'))

    def _key(self, config, args):
        new = args.get('key') or ''
        if not isinstance(new, str) or len(new) > 8192 or any(ch in new for ch in '\r\n\0'):
            raise ValueError('API 密钥格式无效')
        if new.strip():
            return new.strip(), '', True
        cid = args.get('credentialId') or (config['provider'] if config['purpose'] == 'extract' else '')
        if config['purpose'] == 'extract':
            if cid != config['provider']:
                raise ValueError('请选择此转录服务自己的密钥')
            saved = secrets.get_config(cid, require_access=True)
            if config['provider'] == 'custom_openai':
                public = self._public_custom_asr(saved)
                if public['base'] != config['base']:
                    raise ValueError('接口地址已更改，请为新地址输入密钥，原连接保留')
            value = saved.get('api_key') or ''
        else:
            if not identity(cid).startswith('workspace-key-'):
                raise ValueError('请先输入此服务的 API 密钥')
            if cid in (self.store.preference('model-removed-credentials') or []):
                raise ValueError('此密钥已删除，请重新添加密钥')
            binding = self._binding(config)
            existing = any(c.get('credential_id') == cid and all(c.get(k) == config[k]
                for k in ('provider', 'mode', 'base', 'protocol')) for c in workspace.connections(self.store))
            if self._credentials.get(cid) != binding and not existing:
                raise ValueError('此密钥不属于当前连接，请重新选择')
            value = secrets.get_key(cid, require_access=True) or ''
        if not value:
            raise ValueError('请先输入 API 密钥；已有连接可点击后从系统安全存储读取')
        return value, cid, False

    def _persist_assistant_key(self, config, key, cid, new):
        with self._save_lock:
            if new:
                cid = 'workspace-key-' + uuid4().hex
                secrets.set_key_verified(cid, key)
            elif cid in (self.store.preference('model-removed-credentials') or []):
                raise ValueError('此密钥已删除，请重新添加密钥')
            self._credentials[cid] = self._binding(config)
            return cid

    @staticmethod
    def _request_directory(base, key):
        import httpx
        deadline = time.monotonic() + 30
        try:
            with httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False, trust_env=False) as client:
                with client.stream('GET', base + '/models', headers={'Authorization': 'Bearer ' + key}) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f'连接检查返回 HTTP {response.status_code}，请检查密钥和服务类型')
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise RuntimeError('模型目录读取超时，检查已停止')
                        if len(body) + len(chunk) > _MAX_RESPONSE:
                            raise RuntimeError('模型目录响应过大，检查已停止')
                        body.extend(chunk)
            result = json.loads(body)
            if type(result) is not dict or type(result.get('data')) is not list:
                raise ValueError()
            return result['data']
        except httpx.HTTPError:
            raise RuntimeError('暂时无法连接服务商，请检查网络和接口地址，填写内容已保留') from None
        except (ValueError, UnicodeError):
            raise RuntimeError('服务商返回的模型目录无法识别，填写内容已保留') from None

    def _parse_models(self, config, values):
        known = {m['id']: m for m in workspace.catalog()['models'].get(config['provider'], [])}
        result = []
        for value in values[:5000]:
            if type(value) is not dict or not isinstance(value.get('id'), str) or not 0 < len(value['id']) < 200:
                continue
            mid = value['id']
            architecture = value.get('architecture') or {}
            modalities = architecture.get('output_modalities') if type(architecture) is dict else None
            if modalities is not None and (type(modalities) is not list or 'text' not in modalities):
                continue
            if config['provider'] == 'mimo' and mid not in known:
                continue
            context = value.get('context') or {}
            length = value.get('context_length') or (context.get('length') if type(context) is dict else None) or known.get(mid, {}).get('window')
            top = value.get('top_provider') or {}
            output = (top.get('max_completion_tokens') if type(top) is dict else None) or known.get(mid, {}).get('max_output')
            result.append(dict(id=mid, label=str(value.get('name') or mid)[:200],
                               window=length if type(length) is int and 8192 <= length <= 10_000_000 else None,
                               max_output=output if type(output) is int and output > 0 else None,
                               max_input=known.get(mid, {}).get('max_input'),
                               tools_supported='tools' in value['supported_parameters'] if type(value.get('supported_parameters')) is list else known.get(mid, {}).get('tools_supported', mid in known)))
        return result

    def _check(self, config, args, *, discover):
        key, cid, new = self._key(config, args)
        if config['purpose'] == 'extract':
            if discover:
                raise ValueError('转录服务使用已适配的模型，无需获取文稿模型目录')
            # ASR authentication can require audio upload. Never invent an empty
            # transcription or a billable generation just to light a green badge.
            can_query = config['kind'] == 'openai_compat'
        else:
            provider = next(p for p in workspace.catalog()['providers'] if p['id'] == config['provider'])
            can_query = provider['discovery'] and config['mode'] == 'api' and config['protocol'] == 'openai'
        rows = []
        if can_query:
            rows = self._parse_models(config, self._request_directory(config['base'], key))
            message = '模型目录已连通。目录可能公开可读，尚未验证实际模型调用权限；未发送文稿或音频。'
        elif discover:
            raise ValueError('此服务没有已确认的模型目录接口，请使用下方已适配的模型')
        else:
            message = '接口配置格式已检查。此服务暂无已确认的无消耗鉴权接口，尚未验证调用权限；未发送文稿或音频。'
        if config['purpose'] == 'assistant':
            cid = self._persist_assistant_key(config, key, cid, new)
            if can_query:
                directory_config = dict(config, credential_id=cid)
                self.store.preference(workspace._directory_key(directory_config), {'fetched_at': now(), 'models': rows})
        else:
            # Testing an ASR draft must not overwrite the currently selected key.
            cid = '' if new else cid
        return dict(credentialId=cid, status='partial', message=message, models=rows)

    def _save_assistant(self, config, args):
        selected = args.get('modelIds') or ([args['modelId']] if args.get('modelId') else [])
        if type(selected) is not list or not selected or len(selected) > 200 or any(not isinstance(m, str) or not 0 < len(m) < 200 for m in selected):
            raise ValueError('请至少选择一个已确认规格的模型')
        selected = list(dict.fromkeys(selected))
        default = args.get('defaultModel') or selected[0]
        if default not in selected:
            raise ValueError('默认模型需要在已选列表中')
        key, cid, new = self._key(config, args)
        data = workspace.catalog()
        known = {m['id']: m for m in data['models'].get(config['provider'] + ':' + config['mode'], data['models'].get(config['provider'], []))}
        fetched = (self.store.preference(workspace._directory_key(dict(config, credential_id=cid))) or {}).get('models', []) if cid else []
        # A pre-V2 connection can have verified capacity without a model-directory
        # endpoint. Reuse its persisted specification only with the same opaque
        # credential and complete endpoint scope, never from UI-supplied limits.
        specs = {c['model']: dict(c, id=c['model']) for c in workspace.connections(self.store)
                 if cid and c.get('credential_id') == cid and all(c.get(k) == config[k]
                     for k in ('provider', 'mode', 'base', 'protocol'))}
        # Custom endpoints never inherit official-service capacity claims.
        if config['provider'] != 'custom':
            specs.update(known)
        for model in fetched:
            if type(model.get('window')) is int or model['id'] not in specs:
                specs[model['id']] = model
        if any(type(specs.get(mid, {}).get('window')) is not int or not 8192 <= specs[mid]['window'] <= 10_000_000 for mid in selected):
            raise ValueError('部分模型的上下文容量尚未确认，请先获取模型；无法确认的型号暂不能保存')
        cid = self._persist_assistant_key(config, key, cid, new)
        values = []
        for mid in selected:
            spec = specs[mid]
            values.append(dict(provider=config['provider'], mode=config['mode'], base=config['base'], protocol=config['protocol'],
                               model=mid, window=spec['window'], max_output=min(spec.get('max_output') or 4096, spec['window'] // 2, 32768),
                               threshold=.8, credential_id=cid, label=spec.get('label') or mid))
        outputs = [workspace.validated_connection(v, self.store) for v in values]
        group = sha256(encode([config[k] for k in ('provider', 'mode', 'base', 'protocol')]).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            def preference(key, fallback):
                row = db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
                return json.loads(row[0]) if row else fallback
            all_connections = preference('connections', [])
            groups = preference('model-connection-groups', {})
            managed = set(preference('model-managed-connections', []))
            chosen = []
            for value, output in zip(values, outputs):
                match = next((c for c in all_connections if all(c.get(k) == value[k] for k in value)), None)
                if match is None:
                    all_connections.append(output)
                    match = output
                chosen.append(match)
            # First V2 save also adopts matching pre-V2 choices into this group.
            managed.update(c['id'] for c in all_connections if all(c.get(k) == config[k] for k in ('provider', 'mode', 'base', 'protocol')))
            groups[group] = [c['id'] for c in chosen]
            default_id = next(c['id'] for c in chosen if c['model'] == default)
            for field, value in [('connections', all_connections), ('model-connection-groups', groups),
                                 ('model-managed-connections', sorted(managed)), ('default-connection', default_id)]:
                db.execute('INSERT INTO preferences(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (field, encode(value)))
        public = self.snapshot()
        return dict(credentialId=cid, connections=public['connections'], storedConnections=public['storedConnections'], defaultConnectionId=default_id)

    def _save_asr(self, config, args):
        key, _cid, _new = self._key(config, args)
        pid = config['provider']
        original = secrets.get_key(pid, require_access=True)
        old_engine = self.cfg.engine_name
        secret_config = dict(api_key=key)
        if pid == 'custom_openai':
            secret_config.update(base_url=config['base'], model=config['model'])
        cloud = deepcopy(self.store.preference('model-settings-cloud-configs') or {})
        public_id = 'custom_asr' if pid == 'custom_openai' else pid
        for item in cloud.values():
            item['active'] = False
        cloud[public_id] = dict(provider=public_id, credentialId=pid, modelId=config['model'], base=config['base'], configured=True, active=True)
        try:
            secrets.set_key_verified(pid, encode(secret_config))
            self.cfg.engine_name = pid
            if not self.cfg.save():
                raise RuntimeError('配置未能保存')
            self.store.preference('model-settings-cloud-configs', cloud)
        except Exception:
            self.cfg.engine_name = old_engine
            try:
                secrets.set_key_verified(pid, original or '')
                if not self.cfg.save():
                    raise RuntimeError('rollback failed')
            except Exception:
                raise RuntimeError('保存未完成，原密钥或默认模型的恢复也未能确认，请保留当前设置后重试') from None
            raise RuntimeError('连接未能保存，已恢复原密钥和默认模型') from None
        return dict(credentialId=pid, connections=workspace.available_connections(self.store),
                    defaultConnectionId=self.store.preference('default-connection') or '', cloudConfigs=cloud)
