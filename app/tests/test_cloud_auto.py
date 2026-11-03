"""No real keys or public traffic: real media preparation and a local HTTP fixture."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
import wave

from src.core import library
from src.core.agent_workspace.batches import BatchService
from src.core.engines.cloud_http import CloudFailure, UploadBody, failure, multipart, volcano_body
from src.core.engines.cloud_limits import CloudPolicy, RateGate, ResourceAdmission, policy_for
from src.core.engines.cloud_requests import CloudBatchEngine
from src.core.models import Transcript, Segment
from tests.test_agent_workspace import AgentWorkspaceTests
from tests.test_cli_expansion import ExpansionTests
from tests.test_live_recording import until

SPAWN = multiprocessing.get_context('spawn')


class ThreadProcess:
    """Only the local HTTP matrix replaces process startup, not the network/pipeline."""
    def __init__(self, target, args, daemon=True):
        # A real spawned process has its own handle; mirror that ownership.
        args = (Connection(os.dup(args[0].fileno())),*args[1:])
        self.thread = threading.Thread(target=target,args=args,daemon=True)
        self.pid = None
    def start(self):
        self.thread.start();self.pid=1
    def is_alive(self):return self.thread.is_alive()
    def join(self,seconds=None):self.thread.join(seconds)
    def terminate(self):pass
    def kill(self):pass


class ThreadContext:
    Process = ThreadProcess
    @staticmethod
    def Pipe():return SPAWN.Pipe()


def successful_process(connection):
    args=connection.recv();connection.send({'event':'ready'})
    if connection.recv()=='send':
        time.sleep(.1)
        connection.send(dict(ok=True,transcript=dict(text='子进程完整结果',segments=[dict(text='子进程完整结果',start=0.,end=.1)],language='zh',engine=args['provider'])))
    connection.close()


def blocked_process(connection):
    connection.recv();connection.send({'event':'ready'})
    if connection.recv()=='send':time.sleep(30)
    connection.close()


class CloudAutoTests(unittest.TestCase):
    setUp = AgentWorkspaceTests.setUp
    runtime = ExpansionTests.runtime

    def audio(self, index=0):
        path=self.root/f'speech-{index}.wav'
        with wave.open(str(path),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'))
            f.writeframes((1000+index).to_bytes(2,'little')*int(1600+index%3*800))
        return path

    def fixture(self, hold=False, status=200):
        state=dict(calls=0,active=0,peak=0,times=[],bodies=[],status=status,release=threading.Event(),lock=threading.Lock())
        if not hold:state['release'].set()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=self.rfile.read(int(self.headers['Content-Length']))
                with state['lock']:
                    state['calls']+=1;number=state['calls'];state['active']+=1
                    state['peak']=max(state['peak'],state['active']);state['times'].append(time.monotonic());state['bodies'].append(data)
                    status=state['status']
                state['release'].wait(30)
                if state.get('fire'):
                    payload={'result':{'text':'火山完整正文','utterances':[dict(text='火山完整正文',start_time=0,end_time=100)]}}
                elif status==429:
                    payload={'error':{'code':state.get('error_code','rate_limit_exceeded'),'message':'fixture refusal'}}
                elif status>=400:payload={'error':{'code':'fixture-error'}}
                else:payload={'text':'完整服务器正文'+str(number)+'结束标记','segments':[{'text':'完整服务器正文'+str(number)+'结束标记','start':0.,'end':.1}]}
                raw=json.dumps(payload,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                if state.get('fire'):
                    self.send_header('x-api-status-code',state.get('fire_code','20000000'))
                self.send_header('Retry-After','.01');self.end_headers();self.wfile.write(raw)
                with state['lock']:state['active']-=1
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():
            state['release'].set();server.shutdown();server.server_close();thread.join(3)
        self.addCleanup(cleanup)
        return state,dict(api_key='fixture-never-real',model='fixture-model',base_url=f'http://127.0.0.1:{server.server_port}/v1')

    def environment(self, config):
        from src.core.engines import cloud_limits
        cloud_limits._gates.clear()
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch('src.core.secrets.get_config',return_value=config))
        stack.enter_context(patch('src.core.engines.cloud_requests.multiprocessing.get_context',return_value=ThreadContext()))
        stack.enter_context(patch('src.core.engines.cloud_limits.memory_available',return_value=(16*1024**3,32*1024**3)))
        return stack

    def prepare(self, service, name, count=1, **options):
        return service.prepare(name,[dict(source=str(self.audio(i)),title=f'公开测试{i}') for i in range(count)],'custom_openai',cloud_limits={'requests_per_second':1000,**options.pop('cloud_limits',{})},**options)

    def start(self, service, plan, **args):
        return service.start(plan['batch_id'],plan['plan_hash'],True,cloud_upload_consent=True,**args)

    def settled(self, service):
        deadline=time.monotonic()+35
        while service.workers and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(service.workers,'owned tasks must settle')

    def test_30_real_preparations_http_requests_pages_and_full_saved_results(self):
        state,config=self.fixture(hold=True);self.environment(config)
        service=self.runtime().expansion_batches();before=library.load_markdown(self.eid)
        plan=self.prepare(service,'thirty',30)
        self.start(service,plan)
        deadline=time.monotonic()+25
        while state['calls']<30 and time.monotonic()<deadline:time.sleep(.02)
        state['release'].set();self.settled(service)
        self.assertEqual(state['calls'],30);self.assertGreater(state['peak'],2)
        result=service.show(plan['batch_id'],offset=7,limit=7)
        self.assertEqual(result['total'],30);self.assertEqual([i['index'] for i in result['items']],list(range(7,14)))
        self.assertEqual(result['counts']['succeeded'],30,
            [item for item in service.show(plan['batch_id'])['items'] if item['state']!='succeeded'])
        self.assertTrue(result['has_more'])
        for item in service.show(plan['batch_id'])['items']:
            doc=library.load(item['document_id']);self.assertIn('结束标记',doc.text);self.assertEqual(doc.segments[0].end,.1)
            attempt=item['cloud_attempt'];self.assertGreaterEqual(attempt['finished_at'],attempt['sent_at'])
        self.assertEqual(library.load_markdown(self.eid),before)
        self.assertFalse(list(service.root.glob('*.state.json')))
        self.assertFalse(any('fixture-never-real' in p.read_text() for p in service.root.glob('*.json')))
        self.assertTrue(all(b'RIFF' in b and b'fixture-model' in b for b in state['bodies']))
        reopened=BatchService(self.runtime());self.assertEqual(reopened.show(plan['batch_id'])['counts']['succeeded'],30)
        self.start(service,plan,resume=True);self.assertEqual(state['calls'],30)

    def test_two_batches_share_account_inflight_slots(self):
        state,config=self.fixture(hold=True);self.environment(config)
        service=self.runtime().expansion_batches()
        first=self.prepare(service,'one',3,cloud_limits={'max_inflight':2})
        second=self.prepare(service,'two',3,cloud_limits={'max_inflight':2})
        self.start(service,first);self.start(service,second)
        until(lambda:state['calls']>=2);time.sleep(.2)
        self.assertEqual(state['calls'],2)
        state['release'].set();self.settled(service)
        self.assertEqual(state['calls'],6);self.assertLessEqual(state['peak'],2)

    def test_refused_429_waits_and_retries_but_500_remains_unknown(self):
        state,config=self.fixture(status=429);self.environment(config)
        service=self.runtime().expansion_batches();plan=self.prepare(service,'rate')
        self.start(service,plan)
        until(lambda:state['calls']==1);state['status']=200
        self.settled(service)
        self.assertEqual(state['calls'],2);self.assertGreaterEqual(state['times'][1]-state['times'][0],1.9)
        self.assertEqual(service.show(plan['batch_id'])['state'],'succeeded')
        state['status']=500
        bad=self.prepare(service,'unknown');self.start(service,bad);self.settled(service)
        self.assertEqual(state['calls'],3)
        self.assertEqual(service.show(bad['batch_id'])['unconfirmed_count'],1)
        self.start(service,bad,resume=True);self.settled(service);self.assertEqual(state['calls'],3)

    def test_account_refusal_is_known_and_stops_unsent_members(self):
        state,config=self.fixture(status=429);state['error_code']='insufficient_quota';self.environment(config)
        service=self.runtime().expansion_batches();plan=self.prepare(service,'account',4,cloud_concurrency=1)
        self.start(service,plan);self.settled(service)
        result=service.show(plan['batch_id'])
        self.assertEqual(state['calls'],1);self.assertEqual(result['unconfirmed_count'],0)
        self.assertEqual(result['state'],'interrupted');self.assertEqual(result['items'][0]['cloud_attempt']['state'],'rejected')
        self.assertEqual(result['counts']['pending'],3)

    def test_unknown_retry_scope_does_not_upload_other_cancelled_members(self):
        state,config=self.fixture(status=500);self.environment(config)
        service=self.runtime().expansion_batches();plan=self.prepare(service,'scope',3,cloud_concurrency=1)
        batch=service._read(plan['batch_id']);batch['state']='interrupted'
        batch['items'][0].update(state='recovery_required',cloud_attempt={'state':'unknown'})
        batch['items'][1].update(state='cancelled');batch['items'][2].update(state='cancelled');service._save(batch)
        state['status']=200
        self.start(service,plan,resume=True,retry_unknown=True);self.settled(service)
        result=service.show(plan['batch_id'])
        self.assertEqual(state['calls'],1);self.assertEqual(result['counts']['succeeded'],1);self.assertEqual(result['counts']['cancelled'],2)

    def test_cancel_at_success_preserves_paid_result_through_real_pipeline(self):
        state,config=self.fixture(hold=True);self.environment(config)
        service=self.runtime().expansion_batches();plan=self.prepare(service,'cancel-late',3,cloud_concurrency=1)
        self.start(service,plan);until(lambda:state['calls']==1)
        service.cancel(plan['batch_id']);state['release'].set();self.settled(service)
        result=service.show(plan['batch_id'])
        self.assertEqual(state['calls'],1);self.assertEqual(result['state'],'cancelled');self.assertEqual(result['counts']['succeeded'],1)
        self.assertIn('结束标记',library.load(result['items'][0]['document_id']).text)
        self.assertEqual(result['unconfirmed_count'],0)

    def test_saved_result_recovery_never_reads_credentials_or_resends(self):
        state,config=self.fixture();self.environment(config)
        service=self.runtime().expansion_batches();plan=self.prepare(service,'save-failure')
        with patch.object(library,'save',side_effect=OSError('fixture disk write')):
            self.start(service,plan);self.settled(service)
        self.assertEqual(service.show(plan['batch_id'])['counts']['recovery_required'],1)
        with patch('src.core.secrets.get_config',side_effect=AssertionError('must not read key')):
            self.start(service,plan,resume=True);self.settled(service)
        self.assertEqual(state['calls'],1);self.assertEqual(service.show(plan['batch_id'])['state'],'succeeded')

    def test_legacy_cloud_running_and_failed_are_not_assumed_unsent(self):
        from src.core.agent_workspace.documents import digest
        from src.core.agent_workspace.store import encode
        service=self.runtime().expansion_batches();plan=self.prepare(service,'legacy',2)
        batch=service._read(plan['batch_id']);batch.pop('cloud_tracking_version');batch['plan'].pop('cloud_tracking_version')
        batch['plan_hash']=digest(encode(batch['plan']));batch['state']='running';batch['items'][0]['state']='running';batch['items'][1]['state']='failed';service._save(batch)
        result=service.show(plan['batch_id']);self.assertEqual(result['unconfirmed_count'],2)
        with patch('src.core.secrets.get_config',side_effect=AssertionError('unknown cannot upload')):
            service.start(plan['batch_id'],batch['plan_hash'],True,resume=True,cloud_upload_consent=True);self.settled(service)
        self.assertEqual(service.show(plan['batch_id'])['unconfirmed_count'],2)

    def test_lazy_501_item_plan_and_progress_journal_do_not_rewrite_manifest(self):
        service=self.runtime(lambda item,*a:Transcript('正文',[],engine='custom_openai')).expansion_batches()
        plan=service.prepare('many',[dict(source=f'https://example.com/{i}.mp3') for i in range(501)],'custom_openai')
        batch=service._read(plan['batch_id']);manifest=service._path(plan['batch_id']);before=manifest.read_bytes()
        batch['items'][0].update(state='running',progress=40);service._save(batch,batch['items'][0])
        self.assertEqual(manifest.read_bytes(),before)
        self.assertEqual(service.show(plan['batch_id'],limit=5)['items'][0]['progress'],40)
        self.assertLess(service._state_path(plan['batch_id'],0).stat().st_size,1000)
        self.assertEqual(service.list()[0]['items'],[])

    def test_streamed_multipart_and_fire_base64_preserve_exact_audio_bytes(self):
        path=self.audio();source=path.read_bytes()
        body,kind=multipart(path,{'model':'fixture','response_format':'json'})
        chunks=[]
        while part:=body.read(1000000):chunks.append(part)
        body.close();self.assertTrue(all(len(c)<=65536 for c in chunks));self.assertIn(source,b''.join(chunks))
        body=volcano_body(path);length=len(body);raw=b''
        while part:=body.read(19):raw+=part
        body.close();data=json.loads(raw)
        self.assertEqual(len(raw),length);self.assertEqual(base64.b64decode(data['audio']['data']),source)
        self.assertEqual(data['audio']['format'],'wav');self.assertEqual(data['request']['model_name'],'bigmodel')

    def test_policy_rate_feedback_audio_budget_and_resource_refusal(self):
        gate=RateGate();token=gate.attach(policy_for('dashscope'));before=gate.delay();gate.refused(0)
        self.assertGreater(gate.delay(),before);gate.detach(token)
        self.assertEqual(policy_for('siliconflow').max_seconds,3600)
        self.assertEqual(policy_for('dashscope').rps,20)
        self.assertEqual(policy_for('custom_openai').response_format,'json')
        with self.assertRaises(ValueError):policy_for('dashscope',overrides={'api_key':'not-allowed'})
        r=ResourceAdmission()
        with patch('src.core.engines.cloud_limits.memory_available',return_value=(16*1024**2,1024**3)):
            with self.assertRaises(ValueError):
                with r.payload(5*1024**2,'siliconflow',lambda:False,lambda *_:None):self.fail('insufficient memory must not admit upload')
        entered, release, cancelled, waiting = (threading.Event() for _ in range(4))
        active = []
        lock = threading.Lock()
        def hold_preparation():
            with r.preparation(lambda:False):
                with lock:
                    active.append(True)
                    if len(active)==5:entered.set()
                self.assertTrue(release.wait(5))
        def wait_for_preparation():
            waiting.set()
            with r.preparation(cancelled.is_set):
                self.fail('sixth preparation must wait for one of the five slots')
        with patch.object(r,'check_disk'), ThreadPoolExecutor(max_workers=6) as pool:
            holders=[pool.submit(hold_preparation) for _ in range(5)]
            try:
                self.assertTrue(entered.wait(3),'five preparations must enter together')
                sixth=pool.submit(wait_for_preparation)
                self.assertTrue(waiting.wait(1))
                time.sleep(.15)
                self.assertFalse(sixth.done())
                cancelled.set()
                with self.assertRaises(InterruptedError):sixth.result(2)
            finally:
                release.set()
            for holder in holders:holder.result(2)
            with r.preparation(lambda:False):pass
        clock=[0.];gate=RateGate(clock=lambda:clock[0]);gate.attach(CloudPolicy(audio_seconds_per_hour=2,audio_seconds_per_day=5))
        with gate.audio_budget(2,lambda:False,lambda *_:None) as record:gate.audio_sent(record)
        cancelled=threading.Event();messages=[]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(lambda:self._reserve(gate,cancelled,messages))
            until(lambda:bool(messages));cancelled.set()
            with self.assertRaises(InterruptedError):future.result(3)
        clock[0]=3601
        with gate.audio_budget(1,lambda:False,lambda *_:None):pass
        gate=RateGate();gate.attach(CloudPolicy(requests_per_day=1))
        with gate.audio_budget(1,lambda:False,lambda *_:None) as record:gate.audio_sent(record)
        with self.assertRaises(InterruptedError):
            with gate.audio_budget(1,lambda:True,lambda *_:None):pass

    @staticmethod
    def _reserve(gate,cancelled,messages):
        with gate.audio_budget(1,cancelled.is_set,lambda *x:messages.append(x)):pass

    def test_owned_process_success_cancel_race_and_forced_unknown_are_reaped(self):
        before={p.pid for p in multiprocessing.active_children()}
        engine=CloudBatchEngine('custom_openai',{'api_key':'fixture-only','model':'fixture','base_url':'https://example.com/v1'},
                                {'requests_per_second':1000},worker=successful_process)
        self.addCleanup(engine.close);cancelled=threading.Event()
        result=engine.transcribe_bounded(self.audio(),is_cancelled=cancelled.is_set,
            cloud_observer=lambda event,data:cancelled.set() if event=='sending' else None)
        self.assertEqual(result.text,'子进程完整结果')
        engine.worker=blocked_process;forced=threading.Event()
        with self.assertRaises(CloudFailure) as error:
            engine.transcribe_bounded(self.audio(),cloud_force_cancel=forced.is_set,
                cloud_observer=lambda event,data:forced.set() if event=='sending' else None)
        self.assertEqual(error.exception.kind,'unknown')
        self.assertEqual({p.pid for p in multiprocessing.active_children()},before)

    def test_fire_local_upload_metadata_timestamps_and_busy_no_replay(self):
        from src.core.engines import volcano_engine as fire
        state,config=self.fixture();state['fire']=True
        self.environment(config)
        engine=CloudBatchEngine('volcengine',{'api_key':'fixture-only'},{'requests_per_second':1000})
        self.addCleanup(engine.close)
        with patch.object(fire,'ENDPOINT',config['base_url']+'/flash'):
            result=engine.transcribe_bounded(self.audio())
            self.assertEqual(result.engine,'volcengine');self.assertEqual(result.segments[0].end,.1)
            request=json.loads(state['bodies'][0]);self.assertEqual(request['audio']['format'],'wav')
            self.assertEqual(base64.b64decode(request['audio']['data']),self.audio().read_bytes())
            state['fire_code']='55000031'
            with self.assertRaises(CloudFailure) as error:engine.transcribe_bounded(self.audio())
            self.assertEqual(error.exception.kind,'unknown')
        self.assertEqual(state['calls'],2)

    def test_gui_provider_selection_and_bulk_import_do_not_create_duplicate_old_tasks(self):
        from PySide6.QtWidgets import QApplication,QWidget,QMessageBox
        from src.ui.cloud_batch import CloudBatchDialog
        from src.ui.extraction.widget import ExtractionWorkspace
        from src.core.agent_workspace.service import WorkspaceService
        app=QApplication.instance() or QApplication([])
        runtime=self.runtime(lambda item,batch,*args:Transcript('云界面完整结果',[],engine=batch['engine']))
        service=WorkspaceService(runtime=runtime)
        main=QWidget();main.cfg=SimpleNamespace(engine_name='volcengine');main.open_material_entry=Mock()
        main._add_task=Mock()
        proxy=SimpleNamespace(main=main,closing=False,open_cloud_batch=Mock())
        paths=[str(self.audio(i)) for i in range(3)]
        ExtractionWorkspace.add_files(proxy,paths)
        proxy.open_cloud_batch.assert_called_once_with(files=paths);main._add_task.assert_not_called()
        def settled(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                app.processEvents()
                if predicate():return
                time.sleep(.01)
            self.fail('GUI action did not settle')
        with patch('src.core.agent_workspace.client.dispatch',side_effect=lambda operation,**args:getattr(service,operation)(**args)), \
             patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):
            dialog=CloudBatchDialog(main);settled(lambda:dialog.worker is None)
            self.assertEqual(dialog.provider.currentData(),'volcengine')
            dialog.sources.setPlainText('https://example.com/a.mp3\nhttps://example.com/b.mp3')
            dialog.create();settled(lambda:getattr(dialog,'batch',{}).get('state')=='succeeded' and dialog.worker is None)
            self.assertEqual(dialog.batch['engine'],'volcengine');self.assertEqual(dialog.batch['counts']['succeeded'],2)
            self.assertEqual(dialog.sources.toPlainText(),'');self.assertFalse(dialog.files)
            self.assertTrue(dialog.start.isEnabled())
            self.assertFalse(dialog.quota.isChecked())
            self.assertEqual(dialog.completed.count(),2)
            dialog.completed.setCurrentIndex(1);dialog.open_document()
            main.open_material_entry.assert_called_once_with(dialog.batch['items'][1]['document_id'],'transcript')
            self.assertIn('2',dialog.page_label.text());dialog.timer.stop()
            settled(lambda:dialog.worker is None)
        dialog.deleteLater();main.deleteLater();app.processEvents()

    def test_input_limits_and_unconfigured_endpoint_stop_before_network(self):
        from src.core.engines.cloud_limits import RESOURCES
        with self.assertRaises(ValueError):
            CloudBatchEngine('custom_openai',{'api_key':'fixture','base_url':'not-url','model':'fixture'})
        engine=CloudBatchEngine('siliconflow',{'api_key':'fixture'})
        self.addCleanup(engine.close)
        audio=self.audio()
        with patch.object(engine,'_once') as network,patch.object(Path,'stat',return_value=SimpleNamespace(st_size=51*1024**2)):
            with self.assertRaises(ValueError):engine.transcribe_bounded(audio)
        network.assert_not_called()
        with patch('src.core.engines.cloud_limits.shutil.disk_usage',return_value=SimpleNamespace(free=1)):
            with self.assertRaises(ValueError):
                with RESOURCES.preparation(lambda:False):self.fail('low disk must not start preparation')

    def test_restart_restores_known_audio_budget_without_double_counting(self):
        from src.core.engines import cloud_limits
        state,config=self.fixture();self.environment(config)
        service=self.runtime().expansion_batches()
        first=self.prepare(service,'before-restart',cloud_limits={'audio_seconds_per_hour':10})
        self.start(service,first);self.settled(service)
        old=service.show(first['batch_id'])['items'][0]['cloud_attempt']
        cloud_limits._gates.clear()
        for number in range(2):
            reopened=BatchService(self.runtime())
            plan=self.prepare(reopened,'after-restart-'+str(number),cloud_limits={'audio_seconds_per_hour':10})
            self.start(reopened,plan);self.settled(reopened)
        gate=cloud_limits.shared_gate('custom_openai',config['model'],config['base_url'])
        self.assertEqual(len(gate.audio),3)
        self.assertEqual(len({r['attempt_id'] for r in gate.audio}),3)
        self.assertIn(old['attempt_id'],{r['attempt_id'] for r in gate.audio})
        self.assertEqual(state['calls'],3)

    def test_dashscope_empty_and_sdk_exception_keep_safe_diagnostics_without_replay(self):
        from http import HTTPStatus
        from src.core.engines.dashscope_engine import DashScopeEngine
        engine=DashScopeEngine(engine_id='dashscope',display_name='测试',model='paraformer-realtime-v2',api_key='fixture-secret',attempt_limit=1)
        response=Mock(status_code=HTTPStatus.OK)
        response.get_sentence.return_value=[]
        response.get_request_id.return_value='public-fixture-id'
        with patch('dashscope.audio.asr.Recognition') as recognition:
            recognition.return_value.call.return_value=response
            with self.assertRaises(CloudFailure) as error:engine.transcribe(self.audio())
            self.assertEqual(error.exception.public(),dict(kind='unknown',http_status=200,service_code='EMPTY_TRANSCRIPT',request_id='public-fixture-id',retry_after=0.))
            recognition.return_value.call.assert_called_once()
        with patch('dashscope.audio.asr.Recognition') as recognition:
            recognition.return_value.call.side_effect=RuntimeError('fixture-secret must never escape')
            with self.assertRaises(CloudFailure) as error:engine.transcribe(self.audio())
            self.assertEqual(error.exception.kind,'unknown')
            self.assertEqual(error.exception.service_code,'SDK_EXCEPTION')
            self.assertNotIn('fixture-secret',str(error.exception.public()))
            recognition.return_value.call.assert_called_once()

    def test_http_status_enum_keeps_numeric_status_but_bool_is_not_a_status(self):
        from http import HTTPStatus
        self.assertEqual(failure(HTTPStatus.TOO_MANY_REQUESTS).public()['http_status'],429)
        self.assertEqual(CloudFailure('unknown',status=True).status,0)

    def test_finished_worker_reply_wins_over_late_process_exit_observation(self):
        # The child can send a result and exit between poll(timeout) and
        # is_alive(). A final nonblocking read must retain that paid result.
        class Parent:
            polls=0
            replies=[{'event':'ready'}]
            def send(self,value):pass
            def recv(self):return self.replies.pop(0)
            def close(self):pass
            def poll(self,timeout=0):
                self.polls+=1
                if self.polls==2:
                    self.replies.append(dict(ok=True,transcript=dict(text='完整已返回结果',segments=[],engine='custom_openai')))
                    return False
                return bool(self.replies)
        parent=Parent()
        class DeadChild:
            pid=None
            def __init__(self,**kwargs):pass
            def start(self):self.pid=1
            def is_alive(self):return False
            def join(self,*args):pass
        context=SimpleNamespace(Pipe=lambda:(parent,SimpleNamespace(close=lambda:None)),Process=DeadChild)
        engine=CloudBatchEngine('custom_openai',{'api_key':'fixture-only','model':'fixture','base_url':'https://example.com/v1'},{'requests_per_second':1000})
        self.addCleanup(engine.close)
        with patch('src.core.engines.cloud_requests.multiprocessing.get_context',return_value=context):
            result=engine.transcribe_bounded(self.audio())
        self.assertEqual(result.text,'完整已返回结果')
