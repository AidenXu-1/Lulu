"""Batch homepage rows while preserving per-work receipts and manual columns."""
from __future__ import annotations

from uuid import uuid4
from . import feishu, feishu_mapping as mapping

BATCH_SIZE = 200
SEARCH_SIZE = 50


def link_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('link') or value.get('text') or ''
    if isinstance(value, list):
        return ''.join(link_value(item) for item in value)
    return ''


def eligible(records, fields, field_map):
    # A real source link lets us associate returned rows without trusting order.
    # Local manuscripts may share a source work; keep their independent path.
    if len(records) < 2:
        return False
    selected = mapping.validate_mapping(fields, field_map)
    return ('work_url' in selected and all(not r.source_id and r.work_url for r in records)
            and len({r.work_url for r in records}) == len(records)
            and len({r.work_key for r in records}) == len(records))


def find_matches(target, field_name, urls, cancelled):
    matches = {url: set() for url in urls}
    for offset in range(0, len(urls), SEARCH_SIZE):
        wanted = urls[offset:offset + SEARCH_SIZE]
        page = ''
        seen = set()
        while True:
            if cancelled():
                return None
            params = {'page_size': 500}
            if page:
                params['page_token'] = page
            response = feishu._api('POST', f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/search",
                feishu._user_token(), params=params, json={'field_names': [field_name], 'filter': {
                    'conjunction': 'or', 'conditions': [
                        {'field_name': field_name, 'operator': 'is', 'value': [url]} for url in wanted]}})
            for item in response.get('items') or []:
                url = link_value(item.get('fields', {}).get(field_name))
                rid = item.get('record_id')
                if url not in wanted or not isinstance(rid, str) or not rid:
                    raise ValueError('飞书查重结果不完整，请重新读取表格后再试。')
                matches[url].add(rid)
            if not response.get('has_more'):
                break
            page = response.get('page_token')
            if not page or page in seen:
                raise ValueError('飞书查重分页异常，本次尚未写入。')
            seen.add(page)
    return matches


def existing_ids(target, ids, cancelled):
    found = set()
    for offset in range(0, len(ids), 100):
        if cancelled():
            return None
        chunk = ids[offset:offset+100]
        response = feishu._api('POST', f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/batch_get",
            feishu._user_token(), json={'record_ids': chunk})
        for row in response.get('records') or []:
            rid = row.get('record_id')
            if rid not in chunk or rid in found:
                raise ValueError('飞书记录核对结果异常，本次尚未写入。')
            found.add(rid)
    return found


def save_rows(records, target, fields, account, selection, store, result, cancelled, progress, *, fresh=False):
    from . import feishu_save as save
    selected = mapping.validate_mapping(fields, target['field_map'])
    link_field = selected['work_url']['field_name']
    plans = []
    # Read every durable state first. Unknown prior outcomes stop the operation.
    for record in records:
        key = save._receipt_key(account, target['app_token']+'/'+target['table_id'], record)
        old = store.read(key)
        values = mapping.mapped_values(record, fields, target['field_map'])
        same = old.get('operation_id') == selection['operation_id']
        update = selection.get('repeat_mode') == 'update' or same
        plans.append(dict(record=record, key=key, old=old, values=values,
                          content_hash=save.digest(values), same=same, update=update,
                          record_id=old.get('record_id', '') if update else ''))
    unknown = [p for p in plans if p['old'].get('state') in {'pending', 'uncertain'}]
    if unknown:
        result.row_unconfirmed = len(unknown)
        result.row_cancelled = len(plans)-len(unknown)
        result.errors.append('上次写入结果尚未确认，请先在飞书核对；Lulu 已阻止重复新增。')
        return result
    urls = [p['record'].work_url for p in plans if p['update'] and not p['record_id']]
    matches = {url: set() for url in urls}
    if urls and not fresh:
        progress(f'正在核对 {len(urls)} 条作品的保存位置…')
        matches = find_matches(target, link_field, urls, cancelled)
    if matches is None or cancelled():
        result.row_cancelled = len(plans)
        return result
    known_ids = [p['record_id'] for p in plans if p['record_id']]
    if len(set(known_ids)) != len(known_ids):
        raise ValueError('保存记录对应重复，请先在飞书核对。')
    present = existing_ids(target, known_ids, cancelled)
    if present is None or cancelled():
        result.row_cancelled = len(plans)
        return result
    ready = []
    for plan in plans:
        if cancelled():
            result.row_cancelled = len(plans)-result.row_failed
            return result
        record = plan['record']
        try:
            if plan['record_id']:
                if plan['record_id'] not in present:
                    raise ValueError('原记录已删除或不可访问，未自动新建，请先核对飞书。')
            elif plan['update']:
                found = matches.get(record.work_url, set())
                if len(found) > 1:
                    raise ValueError('目标表中有多条同来源记录，请选择另存一份，避免覆盖错误记录。')
                plan['record_id'] = next(iter(found), '')
            plan['unchanged'] = (plan['same'] and plan['old'].get('state') == 'done'
                                 and plan['old'].get('content_hash') == plan['content_hash'])
            ready.append(plan)
        except (ValueError, feishu.FeishuApiError, feishu.FeishuTransportError) as exc:
            result.row_failed += 1
            result.errors.append(record.title+'：'+str(exc))
    completed = []
    stopped = False
    for action in ('create', 'update'):
        group = [p for p in ready if not p['unchanged'] and bool(p['record_id']) == (action == 'update')]
        for offset in range(0, len(group), BATCH_SIZE):
            chunk = group[offset:offset+BATCH_SIZE]
            if cancelled() or stopped:
                result.row_cancelled += len(chunk)
                continue
            progress(f'正在批量存入 {len(chunk)} 条作品…')
            client_token = str(uuid4())
            rows = []
            # Persist pending for the entire request BEFORE sending any bytes.
            for plan in chunk:
                receipt = dict(state='pending', record_id=plan['record_id'],
                    content_hash=plan['content_hash'], operation_id=selection['operation_id'],
                    client_token=client_token,
                    attachments=plan['old'].get('attachments', {}) if plan['record_id'] else {})
                plan['receipt'] = receipt
                store.write(plan['key'], receipt)
                row = {'fields': plan['values']}
                if plan['record_id']:
                    row['record_id'] = plan['record_id']
                rows.append(row)
            if cancelled():
                for plan in chunk:
                    plan['receipt']['state'] = 'failed'
                    store.write(plan['key'], plan['receipt'])
                result.row_cancelled += len(chunk)
                continue
            try:
                response = feishu._api('POST', f"/bitable/v1/apps/{target['app_token']}/tables/{target['table_id']}/records/batch_{action}?client_token={client_token}",
                    feishu._user_token(), json={'records': rows})
                returned = response.get('records') or []
                ids = [r.get('record_id') for r in returned if isinstance(r, dict)]
                if len(returned) != len(chunk) or len(ids) != len(chunk) or any(not isinstance(r, str) or not r for r in ids) or len(set(ids)) != len(ids):
                    raise feishu.FeishuTransportError(retryable_not_sent=False, uncertain=True)
                if action == 'update':
                    if set(ids) != {p['record_id'] for p in chunk}:
                        raise feishu.FeishuTransportError(retryable_not_sent=False, uncertain=True)
                    by_source = {p['record'].work_url: p['record_id'] for p in chunk}
                else:
                    by_source = {link_value(r.get('fields', {}).get(link_field)): r['record_id'] for r in returned}
                    if set(by_source) != {p['record'].work_url for p in chunk}:
                        raise feishu.FeishuTransportError(retryable_not_sent=False, uncertain=True)
            except Exception as exc:
                definite = isinstance(exc, feishu.FeishuApiError) or (isinstance(exc, feishu.FeishuTransportError) and exc.retryable_not_sent)
                state = 'failed' if definite else 'uncertain'
                for plan in chunk:
                    plan['receipt']['state'] = state
                    store.write(plan['key'], plan['receipt'])
                if definite:
                    result.row_failed += len(chunk)
                else:
                    result.row_unconfirmed += len(chunk)
                    stopped = True
                message = str(exc) if isinstance(exc, (feishu.FeishuApiError, feishu.FeishuTransportError)) else '本批保存结果待确认，请先在飞书核对。'
                result.errors.append(f'{len(chunk)} 条作品：'+message)
                continue
            # Response validated as a whole. If saving a receipt fails, pending
            # entries still prevent blind replay of records already created.
            for plan in chunk:
                plan['receipt'].update(state='done', record_id=by_source[plan['record'].work_url])
                store.write(plan['key'], plan['receipt'])
                result.row_success += 1
                result.updated += int(action == 'update')
                completed.append(plan)
    for plan in ready:
        if plan['unchanged']:
            plan['receipt'] = plan['old']
            result.row_success += 1
            result.updated += 1
            result.unchanged += 1
            completed.append(plan)
    # Rows settle before attachments, so a missing file never delays subsequent rows.
    for plan in completed:
        if plan['record'].cover_local_path or plan['record'].media_local_path:
            progress(f'正在补充附件：{plan["record"].title}')
        problems = save._attachments(plan['record'], target, fields, plan['receipt'], account, store, cancelled)
        if problems:
            result.attachment_failed += 1
            result.errors.append(plan['record'].title+'：'+'；'.join(problems))
    return result
