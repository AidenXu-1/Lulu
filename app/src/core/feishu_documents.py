"""Readable document export and revision-guarded updates; no reverse synchronization."""
from __future__ import annotations
import re
from uuid import uuid4
from . import feishu
from .feishu_save import SaveResult, digest, _receipt_key


def text_elements(text: str) -> list[dict]:
    # Preserve unsupported Markdown literally; never discard user text.
    result = []
    pattern = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`')
    pos = 0
    for match in pattern.finditer(text):
        if match.start()>pos:
            result.append({'text_run':{'content':text[pos:match.start()]}})
        if match[1] is not None:
            run={'content':match[1], 'text_element_style':{'link':{'url':match[2]}}}
        elif match[3] is not None:
            run={'content':match[3], 'text_element_style':{'bold':True}}
        else:
            run={'content':match[4], 'text_element_style':{'inline_code':True}}
        result.append({'text_run':run})
        pos=match.end()
    if pos<len(text) or not result:
        result.append({'text_run':{'content':text[pos:]}})
    return result


def document_blocks(record) -> list[dict]:
    text = record.transcript_text
    if not text.strip():
        raise ValueError('这篇内容还没有文稿，可先转录或选择存入多维表格。')
    if record.work_url:
        text = f'[原作品]({record.work_url})\n\n' + text
    result=[]
    for line in text.split('\n'):
        kind, name, value = 2, 'text', line
        heading=re.match(r'^(#{1,6})\s+(.+)$',line)
        if heading:
            kind=len(heading[1])+2; name='heading'+str(len(heading[1])); value=heading[2]
        elif re.match(r'^\s*[-*+]\s+',line):
            kind,name,value=12,'bullet',re.sub(r'^\s*[-*+]\s+','',line)
        elif re.match(r'^\s*\d+[.)]\s+',line):
            kind,name,value=13,'ordered',re.sub(r'^\s*\d+[.)]\s+','',line)
        # Keep all long text; split well below the service block limit.
        for start in range(0,max(1,len(value)),1800):
            content=value[start:start+1800]
            result.append({'block_type':kind,name:{'elements':text_elements(content)}})
    return result


def snapshot(doc_id: str) -> tuple[int,list[dict]]:
    token=feishu._user_token()
    info=feishu._api('GET',f'/docx/v1/documents/{doc_id}',token)['document']
    revision=info['revision_id']
    rows=[]; cursor=''; visited=set()
    while True:
        params={'page_size':500,'document_revision_id':revision}
        if cursor: params['page_token']=cursor
        data=feishu._api('GET',f'/docx/v1/documents/{doc_id}/blocks',token,params=params)
        rows.extend(data.get('items') or [])
        if not data.get('has_more'): break
        cursor=str(data.get('page_token') or '')
        if not cursor or cursor in visited: raise ValueError('文档读取不完整，请稍后重试。')
        visited.add(cursor)
    latest=feishu._api('GET',f'/docx/v1/documents/{doc_id}',token)['document']['revision_id']
    if latest!=revision: raise ValueError('文档正在被编辑，请稍后再保存。')
    return revision,rows


def document_key(account, selection, record):
    location=digest([selection.get('destination_kind'),selection.get('folder_token'),selection.get('space_id'),selection.get('node_token')])
    return _receipt_key(account,'doc:'+location,record)


def _save_document(record, selection, account, store, cancelled):
    blocks=document_blocks(record)
    key=document_key(account,selection,record)
    old=store.read(key)
    if old.get('state') in {'pending','uncertain'}:
        raise ValueError('上次文档保存结果待核对，请先打开原文档检查：'+old.get('url','飞书云文档'))
    same_operation=old.get('operation_id')==selection['operation_id']
    updating=bool(old.get('doc_id') and (selection.get('repeat_mode')=='update' or same_operation))
    if same_operation and old.get('state')=='done' and old.get('content_hash')==digest(blocks):
        return old['url'],False
    token=feishu._user_token()
    original_children=[]
    if updating:
        doc_id=old['doc_id']; url=old['url']
        revision,current=snapshot(doc_id)
        if digest(current)!=old.get('remote_hash'):
            raise ValueError('飞书文档已被编辑，已保留远端内容。请选择另存一份。')
        root=next((b for b in current if b.get('block_id')==doc_id),None)
        if root is None: raise ValueError('文档结构无法确认，请另存一份。')
        original_children=root.get('children') or []
    state={'state':'pending','operation_id':selection['operation_id'],'content_hash':digest(blocks),'url':old.get('url','') if updating else ''}
    store.write(key,state)
    try:
        if not updating:
            title=selection.get('name') if selection.get('single_document') else record.title
            title=str(title or 'Lulu 文稿')
            if selection.get('destination_kind')=='wiki':
                body={'obj_type':'docx','node_type':'origin','title':title}
                if selection.get('node_token'): body['parent_node_token']=selection['node_token']
                node=feishu._api('POST',f"/wiki/v2/spaces/{selection['space_id']}/nodes",token,json=body)['node']
                doc_id=node['obj_token']; url='https://feishu.cn/wiki/'+node['node_token']
                revision,_=snapshot(doc_id)
            else:
                body={'title':title}
                if selection.get('folder_token'): body['folder_token']=selection['folder_token']
                doc=feishu._api('POST','/docx/v1/documents',token,json=body)['document']
                doc_id=doc['document_id']; revision=doc['revision_id']; url='https://feishu.cn/docx/'+doc_id
            state.update(doc_id=doc_id,url=url)
            store.write(key,state)
        else:
            state.update(doc_id=doc_id,url=url)
            store.write(key,state)
        # Append new body first, then delete only the unchanged old child range.
        for start in range(0,len(blocks),40):
            if cancelled(): raise ValueError('已停止，文档可能部分写入，请打开核对。')
            data=feishu._api('POST',f'/docx/v1/documents/{doc_id}/blocks/{doc_id}/children',token,
                params={'document_revision_id':revision,'client_token':str(uuid4())},json={'children':blocks[start:start+40],'index':-1})
            revision=data['document_revision_id']
        if original_children:
            data=feishu._api('DELETE',f'/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete',token,
                params={'document_revision_id':revision,'client_token':str(uuid4())},json={'start_index':0,'end_index':len(original_children)})
            revision=data['document_revision_id']
        final_revision,current=snapshot(doc_id)
        if final_revision!=revision:
            raise ValueError('文档在保存时发生变化，请打开核对。')
        state.update(state='done',remote_hash=digest(current))
        store.write(key,state)
        return url,updating
    except Exception as exc:
        state['state']='failed' if not state.get('doc_id') and (isinstance(exc,feishu.FeishuApiError) or isinstance(exc,feishu.FeishuTransportError) and exc.retryable_not_sent) else 'uncertain'
        store.write(key,state)
        raise


def save_documents(records,selection,account,store,cancelled,progress):
    result=SaveResult(len(records))
    # Preflight complete bodies before creating any files.
    for record in records: document_blocks(record)
    for index,record in enumerate(records):
        if cancelled():
            result.row_cancelled=len(records)-index; break
        progress(f'正在存入文档 {index+1}/{len(records)}：{record.title}')
        try:
            url,updated=_save_document(record,selection,account,store,cancelled)
            result.urls.append(url); result.row_success+=1; result.updated+=int(updated)
        except Exception as exc:
            receipt=store.read(document_key(account,selection,record))
            uncertain=receipt.get('state') in {'pending','uncertain'}
            result.row_unconfirmed+=int(uncertain);result.row_failed+=int(not uncertain)
            if receipt.get('url') and receipt['url'] not in result.urls: result.urls.append(receipt['url'])
            result.errors.append(record.title+'：'+(str(exc) if isinstance(exc,(ValueError,feishu.FeishuApiError,feishu.FeishuTransportError)) else '文档保存未完成，请打开飞书核对。'))
            if uncertain:
                result.row_cancelled=len(records)-index-1
                break
    return result
