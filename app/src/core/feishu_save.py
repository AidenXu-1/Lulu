"""Unified manual Feishu saves. Worker-only networking, explicit mappings and durable receipts."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Callable
from urllib.parse import parse_qs, urlsplit, quote
from uuid import uuid4

from . import feishu
from . import feishu_mapping as mapping
from .feishu_storage import WorkArchiveRecord, bitable_url_with_table

_SAVE_LOCK = threading.RLock()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class SaveStore:
    """Receipts contain IDs/hashes, never credentials or transcript bodies. No automatic expiry."""
    def __init__(self, root: Path | None = None):
        if root is None:
            from .models_manager import app_data_dir
            root = app_data_dir() / 'feishu' / 'save-receipts'
        self.root = Path(root)
        self.writer = feishu.BitableMediaReceiptStore(self.root)

    def read(self, key: str) -> dict:
        if not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('保存记录标识无效。')
        path = self.root / (key + '.json')
        if not os.path.lexists(path):
            return {}
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd) as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError('保存记录无法读取，请保留现场后重试。')
        return value

    def write(self, key: str, value: dict):
        self.writer.write(key, value)


def account_info() -> dict:
    info = feishu._api('GET', '/authen/v1/user_info', feishu._user_token())
    identity = str(info.get('open_id') or '')
    if not identity:
        raise ValueError('没有取得当前飞书身份，请重新连接。')
    return {'account_id': digest([identity, info.get('tenant_key', '')]), 'name': info.get('name') or '飞书账号'}


def parse_resource(url: str, allowed=('base', 'wiki')) -> tuple[str, str, str]:
    try:
        p = urlsplit(str(url).strip())
        host = (p.hostname or '').lower()
        match = re.fullmatch(r'/(?:drive/)?(' + '|'.join(allowed) + r')/([A-Za-z0-9]+)/?', p.path)
        table = (parse_qs(p.query).get('table') or parse_qs(p.query).get('table_id') or [''])[0]
        valid_host = host == 'feishu.cn' or host.endswith('.feishu.cn')
        if p.scheme != 'https' or not valid_host or p.username or p.password or not match or (table and not re.fullmatch('[A-Za-z0-9]+', table)):
            raise ValueError
        return match[1], match[2], table
    except (ValueError, TypeError):
        raise ValueError('请粘贴飞书的完整链接。') from None


def discover_tables(url: str) -> dict:
    kind, resource, selected = parse_resource(url)
    token = feishu._user_token()
    if kind == 'wiki':
        node = feishu._api('GET', '/wiki/v2/spaces/get_node', token, params={'token':resource})['node']
        if node.get('obj_type') != 'bitable':
            raise ValueError('请选择多维表格链接。')
        resource = node['obj_token']
    tables = feishu._list_all_bitable_tables(resource, token)
    if not tables:
        raise ValueError('这张多维表格没有可访问的数据表。')
    if selected and not any(t.get('table_id') == selected for t in tables):
        raise ValueError('链接中的数据表已删除或不可访问，请重新选择。')
    return {'app_token':resource, 'tables':tables, 'table_id':selected or (tables[0]['table_id'] if len(tables)==1 else ''), 'url':url}


def table_fields(app_token: str, table_id: str) -> list[dict]:
    return feishu._list_bitable_fields(app_token, table_id, feishu._user_token())


def resolve_location(url: str) -> dict:
    if not url.strip():
        return {'destination_kind':'drive', 'folder_token':'', 'name':'云文档默认位置'}
    kind, resource, _ = parse_resource(url, ('folder', 'wiki'))
    if kind == 'folder':
        # Querying children validates ordinary folder access without changing it.
        feishu._api('GET', '/drive/v1/files', feishu._user_token(), params={'folder_token':resource, 'page_size':1})
        return {'destination_kind':'drive', 'folder_token':resource, 'name':'所选云文档文件夹'}
    node = feishu.resolve_node(url)
    return {'destination_kind':'wiki', 'space_id':node['space_id'], 'node_token':node['node_token'], 'name':node.get('title') or '所选知识库位置'}


@dataclass
class SaveResult:
    row_total: int
    row_success: int = 0
    row_failed: int = 0
    row_unconfirmed: int = 0
    row_cancelled: int = 0
    updated: int = 0
    unchanged: int = 0
    urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    attachment_failed: int = 0

    @property
    def complete(self):
        return self.row_success == self.row_total and not self.attachment_failed

    @property
    def url(self):
        return self.urls[0] if self.urls else self.target.get('url', '')

    @property
    def destination_app_token(self):
        return self.target.get('app_token','')

    @property
    def destination_table_id(self):
        return self.target.get('table_id','')

    @property
    def destination_table_name(self):
        return self.target.get('name','')


def source_identity(record: WorkArchiveRecord) -> str:
    return record.source_id or record.work_key or ('content:' + digest([record.title, record.transcript_text]))


def _receipt_key(account: str, target: str, record: WorkArchiveRecord) -> str:
    return digest([account, target, source_identity(record)])


def _create_table(selection: dict, store: SaveStore, account: str) -> dict:
    from .feishu_templates import template_definition
    job_key = digest([account, 'new-table', selection['operation_id']])
    previous = store.read(job_key)
    if previous.get('state') == 'ready':
        return previous['target']
    if previous.get('state') in {'pending', 'uncertain', 'created'}:
        raise ValueError('上次新建表格尚未确认，请打开已知链接核对后，将它作为已有表格使用：' + previous.get('url', '飞书云文档'))
    template = template_definition(selection.get('template_id', 'topic'))
    fields = template['fields']
    store.write(job_key, {'state':'pending'})
    try:
        if selection.get('destination_kind') == 'wiki':
            base = feishu.create_wiki_bitable(selection['space_id'], selection.get('node_token',''), selection['name'])
        else:
            body = {'name':selection['name']}
            if selection.get('folder_token'):
                body['folder_token'] = selection['folder_token']
            base = feishu._api('POST', '/bitable/v1/apps', feishu._user_token(), json=body).get('app') or {}
        if not base.get('app_token'):
            raise ValueError('飞书未返回新表标识，请先核对云文档。')
        url = base.get('url') or 'https://feishu.cn/base/' + base['app_token']
        store.write(job_key, {'state':'created', 'url':url})
        table_name = template['table_name']
        table_id = feishu._prepare_initial_bitable_table(base['app_token'], table_name, fields, feishu._user_token())
        view_path = f"/bitable/v1/apps/{base['app_token']}/tables/{table_id}/views"
        views = feishu._api('GET', view_path, feishu._user_token(), params={'page_size':100})
        items = views.get('items') or []
        if views.get('has_more') or len(items) != 1 or not items[0].get('view_id'):
            raise ValueError('新表的默认视图不符合预期，请在飞书核对后继续。')
        feishu._api('PATCH', view_path+'/'+items[0]['view_id'], feishu._user_token(),
                    json={'view_name':template['view_name']})
        actual = table_fields(base['app_token'], table_id)
        chosen, _ = mapping.suggest_mapping(actual)
        target = {'app_token':base['app_token'], 'table_id':table_id, 'url':bitable_url_with_table(url,table_id), 'name':selection['name'], 'field_map':chosen, 'account_id':account, 'template_id':selection['template_id']}
        store.write(job_key, {'state':'ready', 'target':target})
        return dict(target, _fresh=True)
    except feishu.FeishuApiError as exc:
        # If the Base exists, preserve its URL; do not create a second one on retry.
        previous = store.read(job_key)
        if previous.get('state') == 'pending':
            store.write(job_key, {'state':'failed'})
        elif previous.get('state') == 'created':
            raise feishu.FeishuApiError(exc.code,
                '作品库已创建，但模板配置未完成；本次内容尚未存入。'
                f'请先核对已有表格，避免重复新建（code {exc.code}）：' + previous.get('url', ''),
                retry_after=exc.retry_after) from exc
        raise


def _existing_match(target: dict, selected: dict, record: WorkArchiveRecord) -> str:
    """Only homepage works may adopt pre-existing rows by an exact source link."""
    if record.source_id or not record.work_url or 'work_url' not in selected:
        return ''
    field_name = selected['work_url']['field_name']
    body = {'filter':{'conjunction':'and', 'conditions':[{'field_name':field_name, 'operator':'is', 'value':[record.work_url]}]}, 'field_names':[field_name]}
    result = feishu._api('POST', f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/search", feishu._user_token(), params={'page_size':2}, json=body)
    items = result.get('items') or []
    if len(items)>1 or result.get('has_more'):
        raise ValueError('目标表中有多条同来源记录，请选择另存一份，避免覆盖错误记录。')
    return str(items[0].get('record_id') or '') if items else ''


def _write_row(record: WorkArchiveRecord, target: dict, fields: list[dict], account: str, selection: dict, store: SaveStore) -> tuple[dict, bool, bool]:
    selected = mapping.validate_mapping(fields, target['field_map'])
    values = mapping.mapped_values(record, fields, target['field_map'])
    key = _receipt_key(account, target['app_token'] + '/' + target['table_id'], record)
    old = store.read(key)
    content_hash = digest(values)
    if old.get('state') in {'pending', 'uncertain'}:
        raise ValueError('上次写入结果尚未确认，请先在飞书核对；Lulu 已阻止重复新增。')
    same_operation = old.get('operation_id') == selection['operation_id']
    update = selection.get('repeat_mode') == 'update' or same_operation
    record_id = old.get('record_id','') if update else ''
    if update and not record_id:
        record_id = _existing_match(target, selected, record)
    if record_id:
        # A removed/inaccessible row is an error, never silently recreated.
        feishu._api('GET', f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/{record_id}", feishu._user_token())
    if same_operation and old.get('state') == 'done' and old.get('content_hash') == content_hash:
        return old, bool(record_id), True
    row = {'fields':values}
    action = 'update' if record_id else 'create'
    if record_id:
        row['record_id'] = record_id
    receipt = {'state':'pending', 'record_id':record_id, 'content_hash':content_hash, 'operation_id':selection['operation_id'], 'client_token':str(uuid4()), 'attachments':old.get('attachments',{}) if record_id else {}}
    store.write(key, receipt)
    path = f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/batch_{action}?client_token={receipt['client_token']}"
    try:
        response = feishu._api('POST',path,feishu._user_token(),json={'records':[row]})
        rows = response.get('records') or []
        returned_id = str(rows[0].get('record_id') or '') if len(rows)==1 else ''
        if not returned_id:
            raise feishu.FeishuTransportError(retryable_not_sent=False,uncertain=True,message='飞书未返回完整保存结果，请先核对。')
        receipt.update(state='done', record_id=returned_id)
        store.write(key,receipt)
        return receipt, bool(record_id), False
    except feishu.FeishuApiError:
        receipt['state']='failed'
        store.write(key,receipt)
        raise
    except Exception as exc:
        receipt['state']='failed' if isinstance(exc, feishu.FeishuTransportError) and exc.retryable_not_sent else 'uncertain'
        store.write(key,receipt)
        raise


def _attachments(record, target, fields, receipt, account, store, cancelled):
    """Only already-managed local files; no remote downloads during a save."""
    from .collect import export
    from .collect.bitable_media import prepare_bitable_media
    from types import SimpleNamespace
    chosen = mapping.validate_mapping(fields, target['field_map'])
    failed = []
    attached = dict(receipt.get('attachments') or {})
    key = _receipt_key(account, target['app_token']+'/'+target['table_id'],record)
    if cancelled():
        return ['附件已停止，可重试补充'] if any(k in chosen and getattr(record,k,'') for k in ('cover_local_path','media_local_path')) else []
    if 'cover_local_path' in chosen and record.cover_local_path and not cancelled():
        try:
            path = export.resolve_managed_cover_file(record.cover_local_path)
            if path is None:
                raise ValueError('封面不在 Lulu 受管目录')
            data = path.read_bytes()
            mime = export._cover_payload_mime(path,data)
            field_name = chosen['cover_local_path']['field_name']
            mark = digest([hashlib.sha256(data).hexdigest(), chosen['cover_local_path']['field_id']])
            if attached.get('cover') != mark:
                upload = feishu.upload_bitable_image(target['app_token'], 'cover'+path.suffix.lower(), mime, data)
                if upload.status != 'uploaded':
                    raise ValueError('封面上传未完成')
                result = feishu.batch_update_records(target['app_token'],target['table_id'],[{'record_id':receipt['record_id'],'fields':{field_name:[{'file_token':upload.file_token}]}}])
                if result.succeeded != 1:
                    raise ValueError('封面关联未完成')
                attached['cover']=mark
        except Exception:
            failed.append('封面未完成，可重试补充')
    if 'media_local_path' in chosen and record.media_local_path:
        try:
            if cancelled(): raise ValueError('已停止')
            candidates, issues = prepare_bitable_media((SimpleNamespace(media_status='downloaded', media_local_path=record.media_local_path, stable_key=record.work_key, title=record.title),))
            if not candidates: raise ValueError('受管音视频校验未通过')
            candidate = candidates[0]
            mark = digest([candidate.sha256, chosen['media_local_path']['field_id']])
            if attached.get('media') != mark:
                result = feishu.upload_and_attach_bitable_media(target['app_token'],target['table_id'],receipt['record_id'],candidate,is_cancelled=cancelled,field_name=chosen['media_local_path']['field_name'])
                if result.status != 'attached':
                    raise ValueError('音视频附件未完成')
                attached['media']=mark
        except Exception:
            failed.append('音视频附件未完成，可重试补充')
    receipt['attachments']=attached
    store.write(key,receipt)
    return failed


def save_records(records: tuple[WorkArchiveRecord,...], selection: dict, *, store=None, cancelled: Callable[[],bool]=lambda:False, progress: Callable[[str],None]=lambda text:None) -> SaveResult:
    store = store or SaveStore()
    result = SaveResult(len(records))
    # One process-wide queue prevents duplicate saves from different source pages.
    with _SAVE_LOCK, feishu.api_session(), feishu.user_session():
        account = account_info()['account_id']
        if selection.get('account_id') != account:
            raise ValueError('飞书账号发生变化，请重新选择保存位置。')
        if cancelled():
            if not records:raise ValueError('已停止，未创建或添加作品库。')
            result.row_cancelled=len(records)
            return result
        if selection.get('format') == 'document':
            from .feishu_documents import save_documents
            return save_documents(records, selection, account, store, cancelled, progress)
        target = dict(selection)
        if selection['mode']=='new':
            prospective = [{'field_id':str(i),'field_name':f[0],'type':f[1]} for i,f in enumerate(mapping.template_fields(selection.get('template_id','topic')))]
            prospective_map, _ = mapping.suggest_mapping(prospective)
            for record in records:
                mapping.mapped_values(record,prospective,prospective_map)
            target = _create_table(selection,store,account)
        fields = table_fields(target['app_token'],target['table_id'])
        if selection.get('add_fields'):
            if mapping.schema_fingerprint(fields) != selection.get('schema_fingerprint'):
                raise ValueError('表格列已变化，请重新读取并确认新增列。')
            # Validate before any schema change. Only the explicitly chosen missing columns are added.
            for key in selection['add_fields']:
                source = mapping.SOURCE_BY_KEY.get(key)
                if source is None or any(f['field_name'] == source.name for f in fields):
                    raise ValueError('新增列与现有列冲突，请重新检查。')
            prospective = list(fields)
            prospective_map = dict(target['field_map'])
            for key in selection['add_fields']:
                source = mapping.SOURCE_BY_KEY[key]
                prospective.append({'field_id':'new:'+key,'field_name':source.name,'type':source.types[0]})
                prospective_map[key] = {'field_id':'new:'+key,'type':source.types[0]}
            mapping.validate_mapping(prospective,prospective_map)
            for record in records:
                mapping.mapped_values(record,prospective,prospective_map)
            for key in selection['add_fields']:
                source = mapping.SOURCE_BY_KEY[key]
                created = feishu._api('POST',f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/fields",feishu._user_token(),json={'field_name':source.name,'type':source.types[0]})['field']
                fields.append(created)
                target['field_map'][key] = {'field_id':created['field_id'],'type':mapping.field_type(created)}
            selection['add_fields'] = []
        mapping.validate_mapping(fields,target['field_map'])
        # Validate every row before the first row write; absent content never creates blank rows.
        for record in records:
            mapping.mapped_values(record,fields,target['field_map'])
        fresh = bool(target.pop('_fresh', False))
        result.target=target
        result.urls=[target['url']]
        from . import feishu_save_batch
        if feishu_save_batch.eligible(records, fields, target['field_map']):
            return feishu_save_batch.save_rows(records, target, fields, account, selection, store, result, cancelled, progress, fresh=fresh)
        for index, record in enumerate(records):
            if cancelled():
                result.row_cancelled=len(records)-index
                break
            progress(f'正在存入 {index+1}/{len(records)}：{record.title}')
            try:
                receipt, updated, unchanged = _write_row(record,target,fields,account,selection,store)
                result.row_success+=1
                result.updated+=int(updated)
                result.unchanged+=int(unchanged)
                problems = _attachments(record,target,fields,receipt,account,store,cancelled)
                if problems:
                    result.attachment_failed+=1
                    result.errors.append(record.title+'：'+'；'.join(problems))
            except Exception as exc:
                safe = isinstance(exc,(ValueError,feishu.FeishuApiError,feishu.FeishuTransportError))
                message = str(exc) if safe else '保存未完成，请重试或核对飞书。'
                state = store.read(_receipt_key(account,target['app_token']+'/'+target['table_id'],record)).get('state')
                if state in {'pending','uncertain'}:
                    result.row_unconfirmed+=1
                else:
                    result.row_failed+=1
                result.errors.append(f'{record.title}：{message}')
                if state in {'pending','uncertain'}:
                    result.row_cancelled=len(records)-index-1
                    break
    return result
