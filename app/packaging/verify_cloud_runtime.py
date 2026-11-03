#!/usr/bin/env python3
"""Exercise bundled cloud code and its actual frozen child, using only loopback HTTP.

No production entrypoint, current-user stores, credentials or provider requests.
"""
import argparse
import json
from pathlib import Path

from verify_workspace_runtime import BUNDLED_IMPORTS, verify


PROBE = BUNDLED_IMPORTS + r'''
import multiprocessing, threading, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from src.core.engines.cloud_requests import CloudBatchEngine
from src.core.engines.cloud_http import CloudFailure
from src.core.engines.volcano_engine import RESOURCE
assert RESOURCE == 'volc.bigasr.auc_turbo'
from multiprocessing import resource_tracker
resource_tracker.ensure_running()
# The parent loads compiled product modules; spawn starts the real bundled Core
# executable through its existing multiprocessing.freeze_support entry.
sys.frozen = True
sys._MEIPASS = str(root)
sys.executable = str(bundle/'Contents/MacOS/lulu-agent-core')
multiprocessing.set_executable(sys.executable)
state = dict(calls=0, mode='success', arrived=threading.Event(), release=threading.Event())
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        assert self.path == '/v1/audio/transcriptions' and b'RIFF' in body
        assert b'packaged-fixture' in body
        state['calls'] += 1
        state['arrived'].set()
        if state['mode'] == 'blocked': state['release'].wait(10)
        status = 500 if state['mode'] == 'unknown' else 200
        data = json.dumps({'error':{'code':'fixture'}} if status == 500 else
             {'text':'包内云端完整结果','segments':[{'text':'包内云端完整结果','start':0.,'end':.1}]},ensure_ascii=False).encode()
        try:
            self.send_response(status); self.send_header('Content-Length',str(len(data)))
            self.end_headers(); self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError): pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
path=Path.cwd()/'speech.wav'
with wave.open(str(path),'wb') as audio:
    audio.setparams((1,2,16000,0,'NONE','not compressed'));audio.writeframes(b'\x01\x00'*1600)
before={p.pid for p in multiprocessing.active_children()}
engine=CloudBatchEngine('custom_openai',dict(api_key='fixture-only-never-real',model='packaged-fixture',
    base_url=f'http://127.0.0.1:{server.server_port}/v1'),{'requests_per_second':1000})
try:
    cancelled=threading.Event()
    result=engine.transcribe_bounded(path,is_cancelled=cancelled.is_set,
        cloud_observer=lambda event,data:cancelled.set() if event=='sending' else None)
    assert result.text == '包内云端完整结果' and result.segments[0].end == .1
    assert state['calls'] == 1
    state['mode']='unknown'
    try: engine.transcribe_bounded(path)
    except CloudFailure as error: assert error.kind == 'unknown'
    else: raise AssertionError('500 must remain unknown')
    assert state['calls'] == 2
    state['mode']='blocked';state['arrived'].clear()
    try: engine.transcribe_bounded(path,cloud_force_cancel=state['arrived'].is_set)
    except CloudFailure as error: assert error.kind == 'unknown'
    else: raise AssertionError('Force stop must mark result unknown')
    assert state['calls'] == 3
finally:
    engine.close();state['release'].set();server.shutdown();server.server_close();thread.join(3)
assert {p.pid for p in multiprocessing.active_children()} == before
print(json.dumps(dict(ok=True,actual_frozen_owned_process=True,loopback_requests=3,
    late_cancel_success_kept=True,unknown_not_replayed=True,forced_child_reaped=True,
    compiled_fire_module=True,scope='isolated bundled cloud; no provider or real credential access')))
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    receipt=verify(args.app,probe=PROBE,timeout=90)
    args.output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print('CLOUD_RUNTIME_OK: 包内进程、晚到结果保留、未知结果停止重试、强制退出均通过')
