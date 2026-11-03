#!/usr/bin/env python3
"""Thirty bundled request processes plus GUI/persistence, only in a temporary library.

Uses fake credentials and loopback HTTP, never production business or cloud APIs.
"""
import argparse
import json
from pathlib import Path

from verify_workspace_runtime import BUNDLED_IMPORTS, verify


PROBE = BUNDLED_IMPORTS + r'''
import multiprocessing, threading, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
import psutil
os.environ['QT_PLUGIN_PATH']=str(root/'PySide6/Qt/plugins')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox
from src.core import library,secrets
from src.core.agent_workspace.batches import BatchService
from src.core.agent_workspace.service import WorkspaceService
from src.core.engines.cloud_limits import RESOURCES
from src.ui.cloud_batch import CloudBatchDialog
app=QApplication([])
# The host runs with -I; start its own stdlib tracker before selecting the frozen
# worker executable, whose interpreter flags differ from the build Python.
from multiprocessing import resource_tracker
resource_tracker.ensure_running()
sys.frozen=True;sys._MEIPASS=str(root)
sys.executable=str(bundle/'Contents/MacOS/lulu-agent-core')
multiprocessing.set_executable(sys.executable)
library.LIBRARY_DIR=Path.cwd()/'library';library.FOLDERS_FILE=library.LIBRARY_DIR/'folders.json'
original=library.save_document('原稿首尾完整保留。','原稿')
original_text=library.load_markdown(original)
state=dict(calls=0,active=0,peak=0,texts=set(),release=threading.Event(),lock=threading.Lock())
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        assert self.path=='/v1/audio/transcriptions' and b'RIFF' in body
        with state['lock']:
            state['calls']+=1;number=state['calls'];state['active']+=1
            state['peak']=max(state['peak'],state['active'])
        state['release'].wait(40)
        text=f'第{number}份完整结果。'+('完整中段文字。'*100)+'结束标记。'
        with state['lock']:state['texts'].add(text)
        raw=json.dumps(dict(text=text,segments=[dict(text=text,start=0.,end=.1)]),ensure_ascii=False).encode()
        try:
            self.send_response(200);self.send_header('Content-Length',str(len(raw)))
            self.send_header('X-Request-Id','local-fixture-'+str(number));self.end_headers();self.wfile.write(raw)
        finally:
            with state['lock']:state['active']-=1
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
config=dict(api_key='isolated-fixture-only',model='packaged-batch-fixture',base_url=f'http://127.0.0.1:{server.server_port}/v1')
key_reads=[]
def fake_config(provider,**kwargs):
    assert provider=='custom_openai';key_reads.append(provider);return dict(config)
secrets.get_config=fake_config
batch_service=BatchService(object())
service=WorkspaceService(runtime=SimpleNamespace(expansion_batches=lambda:batch_service))
from src.core.agent_workspace import client
client.dispatch=lambda operation,**args:getattr(service,operation)(**args)
paths=[]
for n in range(30):
    path=Path.cwd()/f'speech-{n}.wav'
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,16000,0,'NONE','not compressed'));audio.writeframes((1000+n).to_bytes(2,'little')*1600)
    paths.append(str(path))
main=QWidget();main.cfg=SimpleNamespace(engine_name='custom_openai');opened=[]
main.open_material_entry=lambda *args:opened.append(args)
dialog=CloudBatchDialog(main);dialog.timer.stop();dialog.show()
def until(predicate,seconds=120):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        app.processEvents();time.sleep(.005)
        if predicate():return
    raise AssertionError('Bundled batch/GUI did not settle')
until(lambda:dialog.worker is None)
dialog.files=paths;dialog.file_names.setPlainText('\n'.join(paths))
dialog.quota.setChecked(True);dialog.limit_fields['requests_per_second'].setText('1000')
before={p.pid for p in multiprocessing.active_children()}
capacity=RESOURCES.capacity(30);assert capacity>2,'Insufficient host resources for overlap acceptance'
metrics=dict(peak_children=0,peak_child_mib=0);sampling=threading.Event()
def sample():
    while not sampling.wait(.15):
        children=multiprocessing.active_children();rss=0
        for child in children:
            try:rss+=psutil.Process(child.pid).memory_info().rss
            except psutil.Error:pass
        metrics['peak_children']=max(metrics['peak_children'],len(children))
        metrics['peak_child_mib']=max(metrics['peak_child_mib'],rss//1024**2)
sampler=threading.Thread(target=sample,daemon=True);sampler.start()
gaps=[];previous=[time.monotonic()]
heartbeat=QTimer();heartbeat.setInterval(20)
def beat():
    now=time.monotonic();gaps.append(now-previous[0]);previous[0]=now
heartbeat.timeout.connect(beat);heartbeat.start()
started=time.monotonic()
try:
    with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):dialog.create()
    until(lambda:bool(batch_service.workers))
    while batch_service.workers:
        app.processEvents();time.sleep(.005)
        if state['calls']>=min(8,capacity) or time.monotonic()-started>20:state['release'].set()
        assert time.monotonic()-started<150
    heartbeat.stop();dialog.timer.stop();sampling.set();sampler.join(3)
    until(lambda:dialog.worker is None)
    dialog.refresh();until(lambda:dialog.worker is None)
    result=dialog.batch
    assert result['state']=='succeeded' and result['counts']['succeeded']==30,result
    assert state['calls']==30 and state['peak']>2,state
    saved=[library.load(item['document_id']) for item in result['items']]
    assert len(saved)==30 and {doc.segments[0].text for doc in saved}==state['texts']
    # Finished documents add the normal Markdown heading; full service text must
    # still appear exactly once and timestamps must retain the original value.
    assert all(doc.text.count(doc.segments[0].text)==1 for doc in saved)
    assert all(library.load(item['document_id']).segments[0].end==.1 for item in result['items'])
    assert library.load_markdown(original)==original_text
    assert len(key_reads)==1 and dialog.completed.count()==30
    assert not dialog.files and not dialog.sources.toPlainText()
    dialog.completed.setCurrentIndex(29);chosen=dialog.completed.currentData();dialog.open_document()
    assert opened==[(chosen,'transcript')]
    assert not any(config['api_key'] in p.read_text() for p in batch_service.root.glob('*.json'))
    assert {p.pid for p in multiprocessing.active_children()}==before
    assert gaps and max(gaps)<.5,dict(max_gui_gap_seconds=max(gaps,default=0))
    receipt=dict(ok=True,items=30,actual_frozen_children=True,peak_http_requests=state['peak'],
        **metrics,host_admission_capacity=capacity,max_gui_gap_ms=round(max(gaps)*1000,1),
        elapsed_seconds=round(time.monotonic()-started,2),all_full_results_saved=True,
        original_document_unchanged=True,credentials_reads=1,last_document_opened=True,
        scope='temporary library and loopback only; no real key or provider access')
    # A large intake is distinct from simultaneous uploads. Preserve every row
    # and verify its last page, without spending or producing test manuscripts.
    large=[];dialog.begin=lambda plan,*args,**kwargs:large.append(plan)
    links=[f'https://example.com/large-{n}.wav' for n in range(10001)]
    gaps.clear();previous[0]=time.monotonic();heartbeat.start()
    dialog.sources.setPlainText('\n'.join(links))
    with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):dialog.create()
    until(lambda:dialog.worker is None and bool(large))
    app.processEvents();heartbeat.stop()
    last=batch_service.show(large[0]['batch_id'],offset=10000,limit=1)
    assert large[0]['total']==10001 and last['items'][0]['source']==links[-1]
    assert not batch_service.workers and state['calls']==30 and len(key_reads)==1
    assert library.load_markdown(original)==original_text
    receipt.update(large_intake_items=10001,large_intake_last_page_verified=True,
        large_intake_gui_gap_ms=round(max(gaps,default=0)*1000,1),large_intake_uploads=0)
finally:
    state['release'].set();heartbeat.stop();sampling.set();sampler.join(3)
    dialog.shutdown();server.shutdown();server.server_close();thread.join(3)
print(json.dumps(receipt))
'''


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    receipt=verify(args.app,probe=PROBE,timeout=180)
    args.output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print('CLOUD_BATCH_RUNTIME_OK: 30项包内子进程、本机HTTP、界面响应和完整落盘均通过')
