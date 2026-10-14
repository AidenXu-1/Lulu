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


class ApiSessionTests(unittest.TestCase):
    def test_scoped_connection_reused_and_closed_after_error(self):
        from unittest.mock import MagicMock
        session=MagicMock();response=MagicMock();response.json.return_value={'code':0,'data':{}}
        session.request.return_value=response
        with patch('requests.Session',return_value=session),patch('requests.request') as ordinary:
            with self.assertRaisesRegex(RuntimeError,'stop'):
                with feishu.api_session():
                    feishu._api('GET','/fake','fake')
                    with feishu.api_session():feishu._api('GET','/fake','fake')
                    raise RuntimeError('stop')
            self.assertEqual(session.request.call_count,2);session.close.assert_called_once()
            ordinary.return_value=response;feishu._api('GET','/fake','fake');ordinary.assert_called_once()


class MappingTests(unittest.TestCase):
    def test_collected_details_are_serialized_without_overwriting_manual_tags(self):
        keys=('platform','content_kind','author_verify','tags','original_transcript_text')
        fields=[field(key,m.SOURCE_BY_KEY[key].name,m.SOURCE_BY_KEY[key].types[0]) for key in keys]
        fields.append(field('manual','标签'))
        chosen,_=m.suggest_mapping(fields)
        record=WorkArchiveRecord(platform='douyin',content_kind='video',author_verify='认证',
            tags=('学习','创作'),original_transcript_text='原文')
        values=m.mapped_values(record,fields,chosen)
        self.assertEqual(values,{'来源平台':'抖音','作品类型':'视频','作者认证':'认证',
            '作品话题':'学习、创作','原始转录':'原文'})
        self.assertEqual(m.source_value(WorkArchiveRecord(),'content_kind'),'')
        self.assertEqual(m.source_value(WorkArchiveRecord(),'tags'),'')
    def test_excluded_metadata_is_not_automatched_even_from_saved_mapping(self):
        excluded={'work_id':('作品ID',1),'author_id':('作者平台号',1),
            'author_fans':('作者粉丝数',1),'author_total_likes':('作者总获赞',2),
            'author_works':('作者作品数',2),'language':('语种',1)}
        fields=[field('title','标题')]+[field(key,name,kind) for key,(name,kind) in excluded.items()]
        saved={key:{'field_id':key,'type':kind} for key,(_,kind) in excluded.items()}
        chosen,issues=m.suggest_mapping(fields,saved)
        self.assertFalse(set(excluded)&set(chosen))
        self.assertEqual(m.mapped_values(WorkArchiveRecord(title='保留作品',work_id='id',
            author_id='handle',author_fans=123,author_total_likes=45,author_works=6,language='zh'),
            fields,chosen),{'标题':'保留作品'})

    def test_aliases_and_manual_analysis(self):
        chosen,issues=m.suggest_mapping(FIELDS)
        self.assertEqual(issues,[])
        self.assertEqual(chosen['title']['field_id'],'a')
        payload=m.mapped_values(WorkArchiveRecord(title='一',like_count=0,transcript_text='完整正文'),FIELDS,chosen)
        self.assertEqual(payload,{'视频名称':'一','点赞数':0,'逐字稿文案':'完整正文'})
    def test_ambiguous_and_wrong_type_do_not_guess(self):
        chosen,issues=m.suggest_mapping([field('a','点赞数',2),field('b','点赞量',2),field('c','转录文稿',20)])
        self.assertIsNone(chosen['like_count']);self.assertIn('like_count',issues);self.assertIn('transcript_text',issues)
    def test_saved_id_survives_rename_not_delete_or_type_change(self):
        chosen,_=m.suggest_mapping(FIELDS)
        renamed=[dict(x,field_name='我的视频') if x['field_id']=='a' else x for x in FIELDS]
        resolved,issues=m.suggest_mapping(renamed,chosen)
        self.assertNotIn('title',issues);self.assertEqual(resolved['title']['field_id'],'a')
        renamed[0]['field_id']='new'
        self.assertIn('title',m.suggest_mapping(renamed,chosen)[1])
        renamed[0]=field('a','视频名称',2)
        self.assertIn('title',m.suggest_mapping(renamed,chosen)[1])
    def test_previously_skipped_field_not_automatically_added(self):
        chosen,_=m.suggest_mapping([field('a','标题')])
        result,_=m.suggest_mapping([field('a','标题'),field('b','点赞',2)],chosen)
        self.assertIsNone(result['like_count'])
    def test_protected_formula_duplicate_destinations_block(self):
        for f in (field('a','主题'),field('a','标题',1,is_computed=True)):
            with self.assertRaises(ValueError):m.validate_mapping([f],{'title':{'field_id':'a','type':1}})
        with self.assertRaises(ValueError):m.validate_mapping([field('a','标题')],{'title':{'field_id':'a','type':1},'description':{'field_id':'a','type':1}})
    def test_exact_name_type_conflict_never_silently_uses_alias(self):
        chosen,issues=m.suggest_mapping([field('a','点赞',1),field('b','点赞数',2)])
        self.assertIsNone(chosen['like_count']);self.assertIn('like_count',issues)

    def test_saved_column_renamed_as_manual_or_made_readonly_is_blocked(self):
        saved={'title':{'field_id':'a','type':1}}
        for f in (field('a','主题'),field('a','标题',is_readonly=True)):
            chosen,issues=m.suggest_mapping([f],saved)
            self.assertIsNone(chosen['title']);self.assertIn('title',issues)
            with self.assertRaises(ValueError):m.validate_mapping([f],saved)

    def test_empty_values_do_not_create_empty_rows(self):
        chosen,_=m.suggest_mapping(FIELDS)
        with self.assertRaises(ValueError):m.mapped_values(WorkArchiveRecord(),FIELDS,chosen)
    def test_user_templates_have_correct_order_and_manual_blanks(self):
        self.assertEqual([x[0] for x in m.template_fields('topic')],['标题','发布日期','时长','点赞','收藏','评论','分享','原作品链接','音视频直链','逐字稿文案','主题','话题/开头拆解','封面','标签','作者昵称','采集日期'])
        self.assertEqual([x[0] for x in m.template_fields('research')],['标题','作者昵称','发布日期','时长','点赞','收藏','评论','分享','原作品链接','音视频直链','转录文稿','封面','音视频附件','采集时间'])
        self.assertNotIn('recommend_count',m.SOURCE_BY_KEY)
        for kind in ('topic','research'):
            fields=[field(str(i),spec[0],spec[1]) for i,spec in enumerate(m.template_fields(kind))]
            chosen,_=m.suggest_mapping(fields)
            values=m.mapped_values(WorkArchiveRecord(title='作品',author_name='作者',published_at_ms=1700000000000,captured_at_ms=1800000000000),fields,chosen)
            self.assertEqual(values['作者昵称'],'作者')
            self.assertEqual(values['发布日期'],1700000000000)
            self.assertEqual(values['采集日期' if kind=='topic' else '采集时间'],1800000000000)
            self.assertFalse({'主题','话题/开头拆解','标签','推荐'} & values.keys())

    def test_template_number_properties_follow_native_feishu_contract(self):
        # Official app-table-field/guide: integer thousands is "1,000".
        # CLI's style.thousands_separator is a different representation.
        for kind in ('topic', 'research'):
            numeric=[feishu._bitable_field_payload(x) for x in m.template_fields(kind) if x[1]==2]
            self.assertEqual(len(numeric),4)
            for payload in numeric:
                self.assertEqual(payload['property'],{'formatter':'1,000'})

    def test_failed_template_setup_exposes_created_base_and_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            store=s.SaveStore(Path(folder));selection={'template_id':'topic','name':'测试','operation_id':'op'}
            with patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_api',return_value={'app':{'app_token':'NEW','url':'https://feishu.cn/base/NEW'}}) as api,patch.object(feishu,'_prepare_initial_bitable_table',side_effect=feishu.FeishuApiError(1254001,'飞书接口报错(code 1254001)。')):
                with self.assertRaisesRegex(feishu.FeishuApiError,'已创建.*https://feishu.cn/base/NEW'):
                    s._create_table(selection,store,'user')
                self.assertEqual(store.read(s.digest(['user','new-table','op']))['state'],'created')
                with self.assertRaisesRegex(ValueError,'上次新建表格尚未确认'):
                    s._create_table(selection,store,'user')
                self.assertEqual(api.call_count,1)

    def test_builtin_templates_survive_source_unavailability(self):
        from src.core.feishu_templates import template_definition
        with tempfile.TemporaryDirectory() as folder:
            for kind in ('topic','research'):
                definition=template_definition(kind)
                actual=[field(str(i),spec[0],spec[1]) for i,spec in enumerate(definition['fields'])]
                calls=[]
                def api(method,path,token,**kwargs):
                    calls.append((method,path,kwargs))
                    if method=='POST' and path=='/bitable/v1/apps':return {'app':{'app_token':'NEW'}}
                    if method=='GET' and path=='/bitable/v1/apps/NEW/tables/T/views':return {'items':[{'view_id':'V'}]}
                    if method=='PATCH' and path=='/bitable/v1/apps/NEW/tables/T/views/V':return {}
                    raise AssertionError('Unexpected source access: '+path)
                with patch.object(feishu,'_api',side_effect=api),patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_prepare_initial_bitable_table',return_value='T') as prepare,patch.object(s,'table_fields',return_value=actual):
                    s._create_table({'template_id':kind,'name':'新库','operation_id':kind},s.SaveStore(Path(folder)),'user')
                    self.assertEqual(prepare.call_args.args[1:3],(definition['table_name'],definition['fields']))
                    self.assertEqual(calls[-1][2]['json'],{'view_name':definition['view_name']})
                changed=template_definition(kind);changed['fields'].clear()
                self.assertTrue(template_definition(kind)['fields'])
    def test_folder_and_base_links_are_validated(self):
        self.assertEqual(s.parse_resource('https://my.feishu.cn/drive/folder/Abc',('folder','wiki'))[:2],('folder','Abc'))
        for url in ('http://my.feishu.cn/base/abc','https://feishu.cn.evil.test/base/abc','https://user@my.feishu.cn/base/abc'):
            with self.assertRaises(ValueError):s.parse_resource(url)


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
    def test_updates_preserve_manual_fields_and_missing_values(self):
        record=WorkArchiveRecord(source_id='local1',title='A',like_count=9)
        self.assertTrue(self.save(record).complete)
        self.rows['rec1'].update({'主题':'用户分析','收藏':88})
        self.selection['operation_id']='two'
        self.assertTrue(self.save(replace(record,like_count=12)).complete)
        self.assertEqual(len(self.rows),1);self.assertEqual(self.rows['rec1']['主题'],'用户分析');self.assertEqual(self.rows['rec1']['收藏'],88)
    def test_copy_creates_one_then_retry_does_not_duplicate(self):
        record=WorkArchiveRecord(source_id='local1',title='A')
        self.save(record);self.selection.update(operation_id='two',repeat_mode='copy')
        self.save(record);self.save(record)
        self.assertEqual(len(self.rows),2)
    def test_batch_partial_retry_only_unfinished_rows(self):
        records=[WorkArchiveRecord(source_id='a',title='A'),WorkArchiveRecord(source_id='b',title='B')]
        self.fail_title='B';result=self.save(*records)
        self.assertEqual((result.row_success,result.row_failed),(1,1))
        self.fail_title=None;result=self.save(*records)
        self.assertTrue(result.complete);self.assertEqual(len(self.rows),2)
        self.assertEqual(sum('batch_create' in p for _,p,_ in self.calls),3)
    def test_account_change_zero_write(self):
        self.selection['account_id']='OTHER'
        with self.assertRaises(ValueError):self.save(WorkArchiveRecord(title='A'))
        self.assertEqual(self.calls,[])
    def test_unknown_response_blocks_repeated_create(self):
        original=self.api
        def lost(method,path,token,**kw):
            result=original(method,path,token,**kw)
            if 'batch_create' in path:raise feishu.FeishuTransportError(retryable_not_sent=False,uncertain=True,message='超时')
            return result
        with patch.object(feishu,'_api',side_effect=lost):
            result=self.save(WorkArchiveRecord(source_id='a',title='A'))
        self.assertEqual(result.row_unconfirmed,1)
        result=self.save(WorkArchiveRecord(source_id='a',title='A'))
        self.assertEqual(result.row_unconfirmed,1);self.assertEqual(len(self.rows),1)
    def test_deleted_record_never_silently_recreated(self):
        record=WorkArchiveRecord(source_id='a',title='A');self.save(record);self.rows.clear()
        self.selection['operation_id']='two';result=self.save(record)
        self.assertEqual(result.row_failed,1);self.assertEqual(len(self.rows),0)
    def test_two_local_manuscripts_same_work_keep_separate_identity(self):
        self.save(WorkArchiveRecord(source_id='a',title='A',work_url='https://www.douyin.com/video/123456'),WorkArchiveRecord(source_id='b',title='B',work_url='https://www.douyin.com/video/123456'))
        self.assertEqual(len(self.rows),2)
    def test_all_rows_preflight_before_any_row_write(self):
        with self.assertRaises(ValueError):self.save(WorkArchiveRecord(title='A'),WorkArchiveRecord())
        self.assertEqual(self.calls,[])
    def test_cancel_empty_management_does_not_claim_created(self):
        with self.assertRaisesRegex(ValueError,'已停止'):
            s.save_records((),self.selection,store=self.store,cancelled=lambda:True)
        self.assertEqual(self.calls,[])

    def test_cancel_zero_write(self):
        result=s.save_records((WorkArchiveRecord(title='A'),),self.selection,store=self.store,cancelled=lambda:True)
        self.assertEqual(result.row_cancelled,1);self.assertEqual(self.calls,[])


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
    def test_new_62_no_search_one_batch(self):
        result=self.save();self.assertTrue(result.complete)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.calls[0][2]['json']['records']),62)
    def test_existing_bulk_search_updates_and_preserves_manual_cells(self):
        self.selection['mode']='existing'
        self.remote['old']={'原作品链接':{'link':self.records[0].work_url},'主题':'manual'}
        result=self.save();self.assertTrue(result.complete);self.assertEqual(result.updated,1)
        self.assertEqual(self.remote['old']['主题'],'manual');self.assertEqual(len(self.remote),62)
        self.assertEqual(sum(p.endswith('/records/search') for _,p,_ in self.calls),2)
        self.assertEqual(sum('batch_' in p for _,p,_ in self.calls),2)
    def test_reordered_create_response_maps_by_work_link(self):
        self.reverse=True;self.save()
        for record in self.records:
            receipt=self.store.read(s._receipt_key('A','APP/TABLE',record))
            self.assertEqual(self.remote[receipt['record_id']]['标题'],record.title)
    def test_missing_response_marks_whole_batch_uncertain_no_repeat(self):
        self.incomplete=True;result=self.save()
        self.assertEqual(result.row_unconfirmed,62);count=len(self.calls)
        self.save();self.assertEqual(len(self.calls),count)
    def test_definite_failure_can_retry(self):
        self.fail=feishu.FeishuApiError(1254000,'写入失败')
        self.assertEqual(self.save().row_failed,62)
        self.fail=None;self.assertTrue(self.save().complete);self.assertEqual(len(self.remote),62)
    def test_copy_retry_never_duplicates(self):
        self.selection['repeat_mode']='copy';self.save();self.save()
        self.assertEqual(len(self.remote),62)
    def test_resumed_new_target_must_search(self):
        with patch.object(s,'_create_table',return_value=self.target):self.save()
        self.assertEqual(sum(p.endswith('/records/search') for _,p,_ in self.calls),2)
    def test_duplicate_remote_work_not_overwritten(self):
        self.selection['mode']='existing'
        for rid in ['x','y']:self.remote[rid]={'原作品链接':{'link':self.records[0].work_url},'主题':'manual'}
        result=self.save();self.assertEqual(result.row_failed,1)
        self.assertEqual(self.remote['x']['主题'],'manual');self.assertNotIn('标题',self.remote['x'])
    def test_repeated_update_uses_batch_read_and_one_write(self):
        self.save();self.calls.clear();self.selection['operation_id']='two'
        result=self.save(tuple(replace(r,like_count=999) for r in self.records))
        self.assertTrue(result.complete);self.assertEqual(result.updated,62)
        self.assertEqual(len(self.calls),2)
    def test_deleted_saved_row_not_recreated(self):
        self.save();del self.remote['r0'];self.selection['operation_id']='two'
        result=self.save();self.assertEqual(result.row_failed,1);self.assertEqual(len(self.remote),61)
    def test_cancel_between_batches_preserves_completed(self):
        from src.core import feishu_save_batch as batch
        original=self.api
        def cancel(*a,**kw):
            result=original(*a,**kw)
            if 'batch_create' in a[1]:self.cancel=True
            return result
        with patch.object(batch,'BATCH_SIZE',30),patch.object(feishu,'_api',side_effect=cancel):result=self.save()
        self.assertEqual((result.row_success,result.row_cancelled),(30,32))
    def test_failed_second_batch_retry_does_not_repeat_first(self):
        from src.core import feishu_save_batch as batch
        original=self.api
        def fail(*a,**kw):
            if 'batch_create' in a[1] and len(self.remote)>=30:raise feishu.FeishuApiError(1254000,'失败')
            return original(*a,**kw)
        with patch.object(batch,'BATCH_SIZE',30),patch.object(feishu,'_api',side_effect=fail):result=self.save()
        self.assertEqual((result.row_success,result.row_failed),(30,32))
        self.assertTrue(self.save().complete);self.assertEqual(len(self.remote),62)
    def test_search_pagination_loop_stops_before_writes(self):
        self.selection['mode']='existing'
        with patch.object(feishu,'_api',return_value={'items':[],'has_more':True,'page_token':'same'}):
            with self.assertRaisesRegex(ValueError,'分页异常'):self.save()
        self.assertEqual(len(self.remote),0)
    def test_cancel_after_search_writes_nothing(self):
        self.selection['mode']='existing';original=self.api
        def cancel(*a,**kw):
            result=original(*a,**kw);self.cancel=True;return result
        with patch.object(feishu,'_api',side_effect=cancel):result=self.save()
        self.assertEqual(result.row_cancelled,62);self.assertEqual(len(self.remote),0)


class DocumentTests(unittest.TestCase):
    def test_long_body_is_not_truncated_and_lists_headings_links(self):
        text='# 标题\n- 要点\n正文'+('甲'*10000)+'\n[链接](https://example.com)'
        blocks=docs.document_blocks(WorkArchiveRecord(transcript_text=text))
        self.assertEqual(blocks[0]['block_type'],3);self.assertEqual(blocks[1]['block_type'],12)
        content=''.join(el['text_run']['content'] for b in blocks for k,v in b.items() if isinstance(v,dict) for el in v.get('elements',[]))
        self.assertEqual(content.count('甲'),10000)
        self.assertEqual(blocks[-1]['text']['elements'][0]['text_run']['text_element_style']['link']['url'],'https://example.com')
    def test_no_transcript_no_blank_doc(self):
        with self.assertRaises(ValueError):docs.document_blocks(WorkArchiveRecord(title='A'))
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
    def test_new_table_invalid_row_does_not_create_base(self):
        self.selection.update(mode='new',template_id='topic')
        with patch.object(s,'_create_table') as create:
            with self.assertRaises(ValueError):self.save(WorkArchiveRecord())
            create.assert_not_called()
    def test_add_columns_invalid_row_does_not_change_schema(self):
        self.selection.update(add_fields=['description'],schema_fingerprint=m.schema_fingerprint(FIELDS))
        with self.assertRaises(ValueError):self.save(WorkArchiveRecord())
        self.assertEqual(self.calls,[])
    def test_not_sent_can_retry(self):
        record=WorkArchiveRecord(source_id='a',title='A')
        with patch.object(feishu,'_api',side_effect=feishu.FeishuTransportError(retryable_not_sent=True,uncertain=False,message='未发出')):
            self.assertEqual(self.save(record).row_failed,1)
        self.assertTrue(self.save(record).complete);self.assertEqual(len(self.rows),1)
    def test_cancelled_attachment_cannot_report_complete(self):
        fields=FIELDS+[field('media','音视频附件',17)]
        self.selection['field_map']=m.suggest_mapping(fields)[0]
        with patch.object(s,'table_fields',return_value=fields):
            result=s.save_records((WorkArchiveRecord(title='A',media_local_path='/fake/managed.mp4'),),self.selection,store=self.store,cancelled=lambda:bool(self.rows))
        self.assertEqual(result.row_success,1);self.assertEqual(result.attachment_failed,1);self.assertFalse(result.complete)
    def test_account_targets_roundtrip_separately(self):
        from src.config import _normalize_feishu_storage
        data={'feishu_table_targets':[dict(target_id='one',name='A',url='https://my.feishu.cn/base/A?table=T',account_id='first',field_map={'title':{'field_id':'f','type':1}}),dict(target_id='two',name='B',url='https://my.feishu.cn/base/A?table=T',account_id='second')]}
        _normalize_feishu_storage(data)
        self.assertEqual(len(data['feishu_table_targets']),2)
        self.assertEqual(data['feishu_table_targets'][0]['field_map']['title']['field_id'],'f')


class DocumentWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=s.SaveStore(Path(self.tmp.name)/'receipts');self.rows=[];self.rev=1;self.calls=[]
        self.selection={'destination_kind':'drive','folder_token':'FOLDER','operation_id':'first','repeat_mode':'update','single_document':True,'name':'自定义文稿'}
        self.record=WorkArchiveRecord(source_id='entry',title='A',transcript_text='第一段\n第二段')
        for p in (patch.object(feishu,'_user_token',return_value='fake'),patch.object(feishu,'_api',side_effect=self.api)):
            p.start();self.addCleanup(p.stop)
    def api(self,method,path,token,**kw):
        self.calls.append((method,path,kw))
        if method=='POST' and path=='/docx/v1/documents':return {'document':{'document_id':'DOC','revision_id':self.rev}}
        if method=='GET' and path.endswith('/blocks'):return {'items':[{'block_id':'DOC','children':['b'+str(i) for i in range(len(self.rows))]}]+self.rows}
        if method=='GET':return {'document':{'revision_id':self.rev}}
        self.assertEqual(kw['params']['document_revision_id'],self.rev)
        if method=='POST':self.rows.extend(kw['json']['children'])
        elif method=='DELETE':del self.rows[kw['json']['start_index']:kw['json']['end_index']]
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

if __name__=='__main__':unittest.main()
