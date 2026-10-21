"""Temporary files and fake transport only; no network, secrets or real recordings."""
import csv
from datetime import datetime, timezone
import gzip
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from src.core.live_recording.comments import CommentLog, export_comments, log_path, read_comments
from src.core.live_recording.contracts import LiveError, validate_request
from src.core.live_recording.danmaku import (CommentRecorder, DouyinCommentConnection,
    decode_frame, encode_field, protobuf, connection_url)
from src.core.live_recording.douyin import parse_room
from src.core.live_recording.service import uid
from src.core.live_recording.index import LiveIndex
from tests import test_live_recording as fixtures

def chat(number=1, nickname='观众🙂', text='多少钱？\n第二行'):
    return {'kind':'chat','received_at':'2026-10-17T08:10:11.123+08:00',
            'elapsed_seconds':number,'nickname':nickname,'text':text,'message_id':str(number)}

def frame(messages):
    chunks=[]
    for entry in messages:
        user=encode_field(3,entry['nickname'].encode())
        body=encode_field(2,user)+encode_field(3,entry['text'].encode())
        chunks.append(encode_field(1,encode_field(1,b'WebcastChatMessage')+
                      encode_field(2,body)+encode_field(3,int(entry['message_id']))))
    response=b''.join(chunks)+encode_field(9,1)+encode_field(5,b'fixture')
    return encode_field(2,1234)+encode_field(8,gzip.compress(response))

class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.record={'id':uid('recording'),'directory':str(self.root)}
        self.log=CommentLog(self.record)

    def test_unicode_search_pagination_and_equal_text_from_different_senders(self):
        self.log.append([chat(i,nickname='昵称'+str(i),text='相同文字🙂') for i in range(1,411)])
        result=read_comments(self.record)
        self.assertEqual((result['total'],len(result['messages']),result['pages']),(410,200,3))
        self.assertEqual(result['messages'][0]['message_id'],'211')
        self.assertEqual(len(read_comments(self.record,page=3)['messages']),10)
        self.assertEqual(read_comments(self.record,'昵称409')['matching'],1)
        self.assertEqual(read_comments(self.record,'不存在',1000000)['messages'],[])
        self.log.append([chat(411)])
        self.assertEqual(read_comments(self.record)['messages'][-1]['text'],'多少钱？\n第二行')

    def test_unchanged_and_owned_append_reads_do_not_reparse_history(self):
        from src.core.live_recording import comments
        cache = comments.CommentReadCache()
        self.log.append([chat(i) for i in range(1, 411)])
        with patch.object(comments.json, 'loads', wraps=comments.json.loads) as loads:
            first = read_comments(self.record, cache=cache, log=self.log)
            initial = loads.call_count
            self.assertEqual(first['total'], 410)
            for _ in range(30):
                self.assertEqual(read_comments(self.record, cache=cache, log=self.log), first)
            self.assertEqual(loads.call_count, initial)
            self.log.append([chat(411), chat(412)])
            changed = read_comments(self.record, cache=cache, log=self.log)
            self.assertEqual(loads.call_count - initial, 2)
            self.assertEqual((changed['total'], changed['messages'][-1]['message_id']), (412, '412'))
        for query, span, page in (('', None, 3), ('观众', (20, 320), 2), ('不存在', None, 1)):
            self.assertEqual(read_comments(self.record, query, page, time_range=span, cache=cache, log=self.log),
                             read_comments(self.record, query, page, time_range=span))

    def test_comment_cache_invalidates_replacement_truncation_and_external_edit(self):
        from src.core.live_recording import comments
        cache = comments.CommentReadCache()
        self.log.append([chat(i, text='原文') for i in range(1, 411)])
        read_comments(self.record, cache=cache, log=self.log)
        path = log_path(self.root)
        original = path.read_bytes()
        path.write_bytes(original.replace('原文'.encode(), '改动'.encode()))
        self.log.append([chat(411)])  # Same writer sees an external prefix modification.
        self.assertEqual(read_comments(self.record, cache=cache, log=self.log)['messages'][0]['text'], '改动')
        path.rename(path.with_suffix('.preserved'))
        CommentLog(self.record).append([chat(99, text='替换')])
        self.assertEqual(read_comments(self.record, cache=cache)['total'], 1)
        path.write_bytes(b'{"partial"')
        with self.assertRaises(LiveError):
            read_comments(self.record, cache=cache)
        path.write_bytes(original)
        self.assertEqual(read_comments(self.record, cache=cache)['total'], 410)
        with path.open('ab') as file:
            file.write(b'{"partial"')
        with self.assertRaises(LiveError):
            read_comments(self.record, cache=cache)

    def test_incremental_timeline_matches_full_read_and_cache_keeps_file_guards(self):
        from src.core.live_recording.comments import CommentReadCache
        origin = 1792176500.
        self.record.update(started=origin, ended=None)
        cache = CommentReadCache()
        def status(at, state):
            return dict(kind='status', state=state,
                        received_at=datetime.fromtimestamp(origin+at, timezone.utc).isoformat())
        for offset in range(0, 2400, 300):
            self.log.append([status(offset, 'reconnecting')] +
                [chat(i) for i in range(offset+1, offset+300)] +
                [status(offset+299, 'recording')])
            # Growing duration changes bin widths; gaps and old-page offsets must agree.
            for page in (1, 2):
                self.assertEqual(read_comments(self.record, page=page, cache=cache, log=self.log),
                                 read_comments(self.record, page=page))
        path = log_path(self.root)
        alias = self.root/'alias'
        os.link(path, alias)
        with self.assertRaises(LiveError):
            read_comments(self.record, cache=cache, log=self.log)
        alias.unlink()
        read_comments(self.record, cache=cache, log=self.log)
        kept = path.with_suffix('.kept')
        path.rename(kept)
        path.symlink_to(kept)
        with self.assertRaises(OSError):
            read_comments(self.record, cache=cache, log=self.log)
        self.assertTrue(kept.is_file())

    def test_whole_scene_timeline_search_and_range_remain_independent_of_paging(self):
        self.log.append([chat(i,nickname='昵称'+str(i)) for i in range(1,411)])
        result=read_comments(self.record,time_range=(100,301))
        self.assertEqual((result['total'],result['matching'],result['pages']),(410,201,2))
        self.assertEqual([r['elapsed_seconds'] for r in result['messages']],list(range(101,301)))
        self.assertEqual(read_comments(self.record,page=2,time_range=(100,301))['messages'][0]['elapsed_seconds'],100)
        timeline=result['timeline']
        self.assertLessEqual(len(timeline['bins']),80)
        self.assertEqual(sum(b['count'] for b in timeline['bins']),410)
        searched=read_comments(self.record,'昵称409',time_range=(100,301))
        self.assertEqual((searched['matching'],searched['query_matching'],searched['messages']),(0,1,[]))
        self.assertEqual(sum(b['matching'] for b in searched['timeline']['bins']),1)
        self.assertEqual(sum(b['count'] for b in searched['timeline']['bins']),410)

    def test_timeline_distinguishes_zero_messages_and_incomplete_connection(self):
        origin=1792176500.0
        self.record.update(started=origin,ended=origin+50)
        status=lambda at,state:{'kind':'status','state':state,
            'received_at':datetime.fromtimestamp(origin+at,timezone.utc).isoformat()}
        self.log.append([chat(5),chat(18),status(19,'reconnecting'),status(26,'recording'),
                         chat(27),chat(45),status(46,'reconnecting')])
        bins=read_comments(self.record)['timeline']['bins']
        self.assertEqual(bins[4]['count'],0)
        self.assertTrue(bins[4]['interrupted'])
        self.assertEqual(bins[6]['count'],0)
        self.assertFalse(bins[6]['interrupted'])
        self.assertTrue(bins[-1]['interrupted'])
        # All statuses affect coverage even when the detail list retains only its last 30.
        self.log.append([status(47+i/100,state) for i in range(40)
                         for state in ('recording','reconnecting')])
        result=read_comments(self.record)
        self.assertEqual(len(result['events']),30)
        self.assertTrue(result['timeline']['bins'][4]['interrupted'])

    def test_export_csv_formula_guard_multiline_and_no_overwrite(self):
        self.log.append([chat(1,nickname='=NAME',text='=HYPERLINK("x")'),chat(2)])
        self.log.append([{'kind':'status','state':'reconnecting','received_at':'2026-10-17T08:11:00+08:00'}])
        txt,csvfile=self.root/'out.txt',self.root/'out.csv'
        export_comments(self.record,txt,'txt')
        export_comments(self.record,csvfile,'csv')
        self.assertIn('弹幕连接中断',txt.read_text())
        with csvfile.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.reader(f))
        self.assertEqual(rows[1][1],"'=NAME")
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertEqual(rows[2][2],'多少钱？\n第二行')
        before=txt.read_bytes()
        with self.assertRaises(LiveError):export_comments(self.record,txt,'txt')
        self.assertEqual(txt.read_bytes(),before)
        link=self.root/'linked.txt';link.symlink_to(txt)
        with self.assertRaises(LiveError):export_comments(self.record,link,'txt')
        with self.assertRaises(LiveError):
            export_comments(self.record,self.root/'cancel.txt','txt',cancelled=lambda:True)
        self.assertFalse((self.root/'cancel.txt').exists())

    def test_partial_file_never_overwritten_or_claimed_empty(self):
        self.log.append([chat()])
        path=log_path(self.root)
        with path.open('ab') as f:f.write(b'{"partial"')
        before=path.read_bytes()
        with self.assertRaises(LiveError):read_comments(self.record)
        with self.assertRaises(LiveError):self.log.append([chat(2)])
        self.assertEqual(path.read_bytes(),before)

    def test_hardlink_symlink_and_limit_protect_existing_data(self):
        self.log.append([chat()])
        path=log_path(self.root);before=path.read_bytes()
        alias=self.root/'alias';os.link(path,alias)
        with self.assertRaises(LiveError):read_comments(self.record)
        alias.unlink()
        with patch('src.core.live_recording.comments.MAX_BYTES',len(before)+10):
            with self.assertRaisesRegex(LiveError,'256 MiB'):self.log.append([chat(2)])
        self.assertEqual(path.read_bytes(),before)
        path.rename(path.with_suffix('.kept'));path.symlink_to(path.with_suffix('.kept'))
        with self.assertRaises(OSError):self.log.append([chat(2)])
        self.assertEqual(path.with_suffix('.kept').read_bytes(),before)

    def test_existing_inventory_upgrades_without_changing_original_recordings(self):
        root=self.root/'inventory';root.mkdir()
        db=sqlite3.connect(root/'index.sqlite3')
        db.execute('''CREATE TABLE recordings(id TEXT PRIMARY KEY,host_id TEXT NOT NULL,
            name TEXT NOT NULL,job_id TEXT NOT NULL,directory TEXT NOT NULL UNIQUE,
            started REAL NOT NULL,ended REAL,reason TEXT NOT NULL DEFAULT '',
            gaps TEXT NOT NULL DEFAULT '[]')''')
        values=(self.record['id'],'old-host','原主播🙂','old-job',str(self.root/'旧录像'),1.0,2.0,'offline','[]')
        db.execute('INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?)',values)
        db.commit();db.close()
        for _ in range(2):
            index=LiveIndex(root)
            try:
                row=index.one('SELECT * FROM recordings')
                self.assertEqual(tuple(row[k] for k in ('id','host_id','name','job_id','directory',
                                                       'started','ended','reason','gaps')),values)
                self.assertEqual((row['comments_enabled'],row['comments_count'],row['comments_state']),
                                 (0,0,'legacy'))
            finally:index.close()

class ProtocolTests(unittest.TestCase):
    def test_optional_comment_range_is_closed_finite_and_ordered(self):
        base={'session':uid('session'),'recording_id':uid('recording'),'query':'','page':1}
        validate_request('comments',base)
        validate_request('comments',{**base,'range_start':0,'range_end':5.5})
        for extra in ({'range_start':0},{'range_start':5,'range_end':5},
                      {'range_start':True,'range_end':5},{'range_start':-1,'range_end':5},
                      {'range_start':0,'range_end':float('inf')},
                      {'range_start':0,'range_end':float('nan')},
                      {'range_start':0,'range_end':10**1000}):
            with self.subTest(extra=extra),self.assertRaises(LiveError):
                validate_request('comments',{**base,**extra})

    def test_unicode_decoder_and_ack(self):
        chats,ack=decode_frame(frame([chat()]))
        self.assertEqual(chats,[{'nickname':'观众🙂','text':'多少钱？\n第二行','message_id':'1'}])
        fields=protobuf(ack)
        self.assertEqual((fields[2],fields[7],fields[8]),([1234],[b'ack'],[b'fixture']))

    def test_bad_varint_lengths_and_compression_bomb_are_bounded(self):
        for value in (b'\x0a\xff',b'\x80'*12,b'\x00'):
            with self.subTest(value=value),self.assertRaises(ValueError):protobuf(value)
        with patch('src.core.live_recording.danmaku.MAX_PAYLOAD',2000):
            with self.assertRaises(ValueError):decode_frame(encode_field(8,gzip.compress(b'x'*2001)))

    def test_room_ids_and_closed_boolean_contract(self):
        reply=fixtures.payload();reply['data']['data'][0]['id_str']='7697000000000000000'
        value=parse_room(reply,'123')
        self.assertEqual((value.web_rid,value.room_id),('123','7697000000000000000'))
        url=connection_url(value.room_id,0)
        self.assertTrue(url.startswith('wss://webcast100-ws-web-hl.douyin.com/'))
        self.assertNotIn('sub_room_id=',url)
        for room in ('','abc','1?x=1','1'*21):
            with self.assertRaises(Exception):connection_url(room,0)
        for enabled in (1,'true',None):
            with self.assertRaises(LiveError):
                validate_request('comments_setting',{'session':uid('session'),'enabled':enabled})

    def test_existing_dependency_tls_and_cookie_stay_on_connection(self):
        from types import SimpleNamespace
        socket=SimpleNamespace(settimeout=lambda _:None,shutdown=lambda:None)
        with patch('websocket.create_connection',return_value=socket) as connect:
            conn=DouyinCommentConnection('7697000000000000000','fixture-only')
            conn.connect(threading.Event())
            kwargs=connect.call_args.kwargs
            self.assertTrue(kwargs['sslopt']['check_hostname'])
            self.assertTrue(kwargs['sslopt']['cert_reqs'])
            self.assertEqual(kwargs['cookie'],'fixture-only')
            self.assertEqual(kwargs['http_no_proxy'],['*'])
            conn.close()

class FakeConnection:
    def __init__(self,batches,block=None,release=None):
        self.batches=list(batches);self.block,self.release=block,release
        self.aborted=threading.Event()
    def connect(self,cancel):pass
    def receive(self):
        if self.block:self.block.set();self.release.wait(3);self.block=None
        if self.batches:
            item=self.batches.pop(0)
            if isinstance(item,Exception):raise item
            return item
        self.aborted.wait(.01)
        return None
    def heartbeat(self):pass
    def abort(self):self.aborted.set()
    def close(self):self.aborted.set()

class RecorderTests(unittest.TestCase):
    setUp=FileTests.setUp
    def recorder(self,factory):
        value=CommentRecorder(self.record,'7697000000000000000',connection_factory=factory,
                              retry_delays=(.01,),origin=time.monotonic()-5)
        self.addCleanup(lambda:(value.request_stop(),value.thread.join(4) if value.thread else None))
        return value

    def test_reconnect_deduplicates_ids_preserves_same_text_and_continuous_time(self):
        choices=iter([FakeConnection([[chat(1,text='一样'),chat(2,nickname='别人',text='一样')],OSError()]),
                      FakeConnection([[chat(1,text='一样'),chat(3)]])])
        value=self.recorder(lambda *a:next(choices));value.start()
        fixtures.until(lambda:value.snapshot()['comments_count']==3)
        value.request_stop();value.thread.join(3)
        result=read_comments(self.record)
        self.assertEqual([r['message_id'] for r in result['messages']],['1','2','3'])
        self.assertIn('reconnecting',[e['state'] for e in result['events']])
        self.assertEqual(value.snapshot()['comments_state'],'ended')
        self.assertGreaterEqual(result['messages'][0]['elapsed_seconds'],5)
        self.assertGreater(result['messages'][-1]['elapsed_seconds'],result['messages'][0]['elapsed_seconds'])

    def test_inflight_message_after_cancel_is_rejected(self):
        entered,release=threading.Event(),threading.Event()
        value=self.recorder(lambda *a:FakeConnection([[chat()]],entered,release));value.start()
        self.assertTrue(entered.wait(2));value.request_stop();release.set();value.thread.join(3)
        self.assertEqual(read_comments(self.record)['total'],0)
        self.assertFalse(value.alive())

    def test_slow_disk_keeps_state_and_stop_responsive_until_actual_settlement(self):
        entered,release=threading.Event(),threading.Event()
        value=self.recorder(lambda *a:FakeConnection([]));original=value.log.append
        def slow(rows):entered.set();release.wait(3);return original(rows)
        with patch.object(value.log,'append',side_effect=slow):
            value.start();self.assertTrue(entered.wait(2))
            began=time.monotonic();value.snapshot();value.request_stop()
            self.assertLess(time.monotonic()-began,.2);self.assertTrue(value.alive())
            release.set();value.thread.join(3)
        self.assertFalse(value.alive())

    def test_save_error_is_visible_and_never_connects(self):
        called=[];value=self.recorder(lambda *a:called.append(True))
        with patch.object(value.log,'append',side_effect=OSError('fixture')):
            value.start();value.thread.join(3)
        self.assertEqual(value.snapshot()['comments_state'],'write_error')
        self.assertEqual(called,[])

class ServiceTests(unittest.TestCase):
    setUp=fixtures.ServiceTests.setUp
    call,add=fixtures.ServiceTests.call,fixtures.ServiceTests.add

    def start_recording(self):
        from tests.test_live_recording_regressions import HeldProducer
        from src.core.live_recording.contracts import Observation,Stream
        self.service.producer_factory=HeldProducer
        self.provider.check=lambda *a:Observation('live',web_rid='123',room_id='7697000000000000000',
            streams=(Stream('https://pull.douyincdn.com/fixture.flv'),))
        self.service.comments_factory=lambda record,room,credential,**kw:CommentRecorder(
            record,room,credential,connection_factory=lambda *a:FakeConnection([[chat()]]),**kw)
        hid=self.add();self.call('start',host_id=hid)
        fixtures.until(lambda:self.service.comments and
                       next(iter(self.service.comments.values())).snapshot()['comments_count']==1)
        return hid,self.service.index.one('SELECT * FROM recordings WHERE host_id=?',(hid,))

    def test_pause_stops_both_and_keeps_saved_comment_file(self):
        hid,row=self.start_recording()
        self.assertEqual(self.call('comments',recording_id=row['id'],query='',page=1)['total'],1)
        self.call('pause',host_id=hid);fixtures.until(lambda:not self.service.busy())
        final=self.service._recording(row['id'])
        self.assertEqual(final['comments_state'],'ended')
        self.assertIsNotNone(final['ended'])
        self.assertTrue(log_path(final['directory']).is_file())

    def test_toggle_is_saved_and_does_not_change_an_existing_scene(self):
        self.assertTrue(self.call('snapshot',tab='hosts',query='',filter='all',page=1)['comments_enabled'])
        hid,row=self.start_recording()
        self.call('comments_setting',enabled=False)
        self.assertEqual(self.service.index.setting('comments_enabled'),'0')
        self.assertIn(row['id'],self.service.comments)
        self.call('pause_all');fixtures.until(lambda:not self.service.busy())
        self.call('start',host_id=hid)
        fixtures.until(lambda:self.service.monitors[hid].get('producer'))
        latest=self.service.index.one('SELECT * FROM recordings WHERE host_id=? ORDER BY started DESC',(hid,))
        self.assertEqual(latest['comments_state'],'disabled')
        self.assertFalse(self.service.comments)
        self.call('pause_all');fixtures.until(lambda:not self.service.busy())

    def test_slow_read_keeps_controls_free_and_reserves_only_its_recording(self):
        hid,row=self.start_recording()
        self.call('pause_all');fixtures.until(lambda:not self.service.busy())
        entered,release=threading.Event(),threading.Event()
        def slow(*a,**kw):entered.set();release.wait(3);return read_comments(*a,**kw)
        errors=[]
        def read():
            try:self.call('comments',recording_id=row['id'],query='',page=1)
            except Exception as exc:errors.append(exc)
        with patch('src.core.live_recording.service.read_comments',side_effect=slow):
            worker=threading.Thread(target=read);worker.start()
            try:
                self.assertTrue(entered.wait(2));began=time.monotonic()
                self.call('heartbeat');self.call('pause_all')
                self.assertLess(time.monotonic()-began,.3)
                self.assertIn('弹幕',self.service._delete_blocked(self.service._recording(row['id'])))
            finally:release.set();worker.join(4)
        self.assertFalse(errors)
        self.assertFalse(self.service.comments_io)

    def test_save_failure_does_not_stop_the_video(self):
        hid,row=self.start_recording()
        self.call('pause_all');fixtures.until(lambda:not self.service.busy())
        with patch('src.core.live_recording.danmaku.CommentLog.append',side_effect=OSError('disk fixture')):
            self.call('start',host_id=hid)
            fixtures.until(lambda:self.service.monitors[hid].get('producer'))
            current=self.service.monitors[hid]
            fixtures.until(lambda:self.service._comment_summary(
                self.service._recording(current['recording_id']))['comments_state']=='write_error')
            self.assertFalse(current['producer'].stopped.is_set())
            self.call('heartbeat')
            self.call('pause_all')
            fixtures.until(lambda:not self.service.busy())

    def test_reader_waits_for_complete_batch_without_blocking_recording_controls(self):
        hid,row=self.start_recording()
        recorder=self.service.comments[row['id']]
        entered,done=threading.Event(),threading.Event()
        replies=[]
        def read():
            entered.set()
            replies.append(self.call('comments',recording_id=row['id'],query='',page=1))
            done.set()
        with recorder.log.lock:
            worker=threading.Thread(target=read);worker.start()
            self.assertTrue(entered.wait(2))
            fixtures.until(lambda:bool(self.service.comments_io))
            self.call('heartbeat')
            self.assertFalse(done.is_set())
        worker.join(3)
        self.assertTrue(done.is_set())
        self.assertFalse(replies[0]['read_error'])
        self.call('pause_all');fixtures.until(lambda:not self.service.busy())

    def test_expired_session_stops_comment_receiver_and_rejects_late_reads(self):
        hid,row=self.start_recording()
        self.service.deadline=time.monotonic()-1
        self.call('heartbeat')
        fixtures.until(lambda:not self.service.busy())
        with self.assertRaises(LiveError):
            self.call('comments',recording_id=row['id'],query='',page=1)
        self.assertTrue(log_path(row['directory']).exists())
