"""Offline acceptance for user tables and shared saving; no system credentials/network."""
from __future__ import annotations
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.core import feishu, feishu_mapping as m, feishu_save as s, feishu_documents as docs
from src.core.feishu_storage import WorkArchiveRecord


def field(fid,name,kind=1,**kwargs):return {'field_id':fid,'field_name':name,'type':kind,**kwargs}
FIELDS=[field('a','视频名称'),field('b','点赞数',2),field('c','逐字稿文案'),field('d','主题'),field('e','话题/开头拆解'),field('f','收藏',2)]






























class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=s.SaveStore(Path(self.tmp.name)/'receipts')
        self.calls=[];self.rows={};self.fail_title=None
        self.selection={'account_id':'ACCOUNT','format':'table','mode':'existing','app_token':'APP','table_id':'TABLE','name':'我的表','url':'https://my.feishu.cn/base/APP?table=TABLE','field_map':m.suggest_mapping(FIELDS)[0],'repeat_mode':'update','operation_id':'one'}
        self.patches=[patch.object(s,'account_info',return_value={'account_id':'ACCOUNT'}),patch.object(s,'table_fields',return_value=FIELDS),patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_api',side_effect=self.api)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def api(self,method,path,token,**kw):
        self.calls.append((method,path,kw))
        if method=='GET' and '/records/' in path:
            rid=path.rsplit('/',1)[-1]
            if rid not in self.rows:raise feishu.FeishuApiError(1254043,'记录不存在')
            return {'record':{'record_id':rid,'fields':self.rows[rid]}}
        if 'batch_create' in path or 'batch_update' in path:
            row=kw['json']['records'][0]
            if row['fields'].get('视频名称')==self.fail_title:raise feishu.FeishuApiError(1254000,'写入失败')
            rid=row.get('record_id') or 'rec'+str(len(self.rows)+1)
            self.rows.setdefault(rid,{}).update(row['fields'])
            return {'records':[{'record_id':rid}]}
        if '/records/search' in path:return {'items':[]}
        raise AssertionError((method,path,kw))
    def save(self,*records):return s.save_records(tuple(records),self.selection,store=self.store)



class HomepageBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=s.SaveStore(Path(self.tmp.name)/'receipts')
        self.fields=[field('title','标题'),field('url','原作品链接',15),field('like','点赞',2)]
        self.target=dict(app_token='APP',table_id='TABLE',url='https://my.feishu.cn/base/APP',field_map=m.suggest_mapping(self.fields)[0])
        self.selection=dict(self.target,account_id='A',format='table',mode='new',template_id='research',repeat_mode='update',operation_id='op')
        self.records=tuple(WorkArchiveRecord(platform='douyin',work_id=str(i),work_url=f'https://example.com/{i}',title=str(i),like_count=i) for i in range(62))
        self.calls=[];self.remote={};self.fail=None;self.reverse=False;self.incomplete=False
        self.cancel=False
        for patcher in [patch.object(s,'account_info',return_value={'account_id':'A'}),patch.object(s,'table_fields',return_value=self.fields),patch.object(s,'_create_table',return_value=dict(self.target,_fresh=True)),patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_api',side_effect=self.api)]:
            patcher.start();self.addCleanup(patcher.stop)
    def api(self,method,path,token,**kw):
        self.calls.append((method,path,kw))
        if path.endswith('/records/search'):
            wanted={x['value'][0] for x in kw['json']['filter']['conditions']}
            return {'items':[dict(record_id=k,fields=v) for k,v in self.remote.items() if v['原作品链接']['link'] in wanted]}
        if path.endswith('/records/batch_get'):
            return {'records':[dict(record_id=k,fields=self.remote[k]) for k in kw['json']['record_ids'] if k in self.remote]}
        if method=='GET':
            rid=path.rsplit('/',1)[-1]
            if rid not in self.remote:raise feishu.FeishuApiError(1254043,'记录不存在')
            return {'record':dict(record_id=rid,fields=self.remote[rid])}
        if self.fail:raise self.fail
        rows=[]
        for row in kw['json']['records']:
            rid=row.get('record_id') or 'r'+str(len(self.remote))
            self.remote.setdefault(rid,{}).update(row['fields'])
            rows.append(dict(record_id=rid,fields=dict(row['fields'])))
        if self.reverse:rows.reverse()
        if self.incomplete:rows=rows[:-1]
        return {'records':rows}
    def save(self,records=None):
        return s.save_records(records or self.records,self.selection,store=self.store,cancelled=lambda:self.cancel)


class DocumentTests(unittest.TestCase):
    def test_remote_edits_are_zero_write(self):
        with tempfile.TemporaryDirectory() as root:
            store=s.SaveStore(Path(root)/'receipts');record=WorkArchiveRecord(source_id='local',title='A',transcript_text='正文')
            selection={'destination_kind':'drive','folder_token':'','operation_id':'next','repeat_mode':'update'}
            location=s.digest(['drive','',None,None]);key=s._receipt_key('ACCOUNT','doc:'+location,record)
            store.write(key,{'state':'done','doc_id':'DOC','url':'https://my.feishu.cn/docx/DOC','remote_hash':'old','operation_id':'old'})
            with patch.object(feishu,'_user_token',return_value='fake'),patch.object(docs,'snapshot',return_value=(3,[{'text':'用户修改'}])),patch.object(feishu,'_api') as api:
                with self.assertRaisesRegex(ValueError,'已被编辑'):docs._save_document(record,selection,'ACCOUNT',store,lambda:False)
                api.assert_not_called()



class SaveBoundaryTests(unittest.TestCase):
    setUp = SaveTests.setUp
    api = SaveTests.api
    save = SaveTests.save


class DocumentWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=s.SaveStore(Path(self.tmp.name)/'receipts');self.rows=[];self.rev=1;self.calls=[];self.title=''
        self.selection={'destination_kind':'drive','folder_token':'FOLDER','operation_id':'first','repeat_mode':'update','single_document':True,'name':'自定义文稿'}
        self.record=WorkArchiveRecord(source_id='entry',title='A',transcript_text='第一段\n第二段')
        for p in (patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_api',side_effect=self.api)):
            p.start();self.addCleanup(p.stop)
    def api(self,method,path,token,**kw):
        self.calls.append((method,path,kw))
        if method=='POST' and path=='/docx/v1/documents':
            self.title=kw['json']['title']
            return {'document':{'document_id':'DOC','revision_id':self.rev}}
        if method=='GET' and path.endswith('/blocks'):return {'items':[{'block_id':'DOC','children':['b'+str(i) for i in range(len(self.rows))], 'page':{'elements':[{'text_run':{'content':self.title}}]}}]+self.rows}
        if method=='GET':return {'document':{'revision_id':self.rev}}
        self.assertEqual(kw['params']['document_revision_id'],self.rev)
        if method=='POST':self.rows.extend(kw['json']['children'])
        elif method=='DELETE':del self.rows[kw['json']['start_index']:kw['json']['end_index']]
        elif method=='PATCH':self.title=kw['json']['update_text_elements']['elements'][0]['text_run']['content']
        self.rev+=1;return {'document_revision_id':self.rev}
    def save(self,records=None):return docs.save_documents(records or (self.record,),self.selection,'ACCOUNT',self.store,lambda:False,lambda _:None)
    def test_create_folder_update_revision_and_retry(self):
        self.assertTrue(self.save().complete)
        self.assertEqual(self.calls[0][2]['json'],{'title':'自定义文稿','folder_token':'FOLDER'})
        self.assertTrue(self.save().complete)
        self.assertEqual(sum(p=='/docx/v1/documents' and m=='POST' for m,p,_ in self.calls),1)
        self.selection['operation_id']='second';self.record=replace(self.record,transcript_text='修改后正文')
        result=self.save();self.assertTrue(result.complete);self.assertEqual(result.updated,1)
        writes=[(m,p) for m,p,_ in self.calls if m!='GET']
        self.assertTrue(writes[-2][1].endswith('/children'));self.assertEqual(writes[-1][0],'DELETE')
        self.assertEqual(len(self.rows),1);self.assertEqual(self.rows[0]['text']['elements'][0]['text_run']['content'],'修改后正文')
    def test_partial_doc_has_link_and_stops_batch(self):
        original=self.api
        def lost(method,path,token,**kw):
            if path.endswith('/children'):raise feishu.FeishuTransportError(retryable_not_sent=False,uncertain=True,message='写入待确认')
            return original(method,path,token,**kw)
        with patch.object(feishu,'_api',side_effect=lost):result=self.save((self.record,replace(self.record,source_id='second')))
        self.assertEqual((result.row_unconfirmed,result.row_cancelled),(1,1));self.assertEqual(result.url,'https://feishu.cn/docx/DOC')
        count=len(self.calls);self.assertEqual(self.save().row_unconfirmed,1);self.assertEqual(len(self.calls),count)
    def test_rejected_creation_is_retryable(self):
        with patch.object(feishu,'_api',side_effect=feishu.FeishuApiError(999,'无权限')):self.assertEqual(self.save().row_failed,1)
        self.assertTrue(self.save().complete)
    def test_concurrent_edit_never_deletes_original(self):
        self.save();self.selection['operation_id']='second';self.record=replace(self.record,transcript_text='新正文')
        original=self.api
        def changed(method,path,token,**kw):
            if method=='POST' and path.endswith('/children'):raise feishu.FeishuApiError(1770035,'版本已变化')
            return original(method,path,token,**kw)
        with patch.object(feishu,'_api',side_effect=changed):result=self.save()
        self.assertEqual(result.row_unconfirmed,1);self.assertFalse(any(m=='DELETE' for m,_,_ in self.calls));self.assertEqual(len(self.rows),2)

    def test_update_applies_changed_name_and_retry_keeps_one_exact_document(self):
        self.record=replace(self.record,transcript_text='第一段\n\n第二段')
        self.assertTrue(self.save().complete)
        # The same operation ID must not hide a changed requested title.
        self.selection['name']='验收新名称 **保留字面符号**'
        result=self.save()
        self.assertTrue(result.complete);self.assertEqual(result.updated,1)
        self.assertEqual(self.title,self.selection['name'])
        self.assertEqual(len(self.rows),3)
        self.assertEqual(self.rows[1]['text']['elements'][0]['text_run']['content'],'')
        calls=len(self.calls);self.assertTrue(self.save().complete);self.assertEqual(len(self.calls),calls)
        self.assertEqual(sum(m=='POST' and p=='/docx/v1/documents' for m,p,_ in self.calls),1)

    def test_rejected_title_update_keeps_original_body_and_reports_unconfirmed(self):
        self.save();before=list(self.rows);self.calls.clear()
        self.selection.update(operation_id='renamed',name='新名称')
        original=self.api
        def rejected(method,path,token,**kw):
            if method=='PATCH':raise feishu.FeishuApiError(1770035,'版本已变化')
            return original(method,path,token,**kw)
        with patch.object(feishu,'_api',side_effect=rejected):result=self.save()
        self.assertFalse(result.complete);self.assertEqual(result.row_unconfirmed,1)
        self.assertEqual(self.rows,before);self.assertFalse(any(m in {'POST','DELETE'} for m,_,_ in self.calls))

    def test_success_response_without_actual_title_change_is_not_complete(self):
        self.save();self.selection.update(operation_id='renamed',name='新名称')
        original=self.api
        def ignored(method,path,token,**kw):
            if method=='PATCH':return {'document_revision_id':self.rev}
            return original(method,path,token,**kw)
        with patch.object(feishu,'_api',side_effect=ignored):result=self.save()
        self.assertFalse(result.complete);self.assertEqual(result.row_unconfirmed,1)

if __name__=='__main__':unittest.main()
