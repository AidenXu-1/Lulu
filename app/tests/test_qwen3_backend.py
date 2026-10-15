"""Persistence and subprocess cancellation without running a neural model."""
import copy,json,sys,tempfile,time,unittest,wave
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis import qwen3_runtime as runtime
from src.core.synthesis.qwen3_worker import prompt_key
from src.core.synthesis.qwen3_subprocess import QwenProcess,Qwen3Engine
from src.core.synthesis.base import VoiceProfile,SynthesisRequest,SynthesisError
from src.core.synthesis.voxcpm2 import _Cancelled

class BackendTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_cancel_terminates_child_and_releases_busy_state(self):
        import subprocess
        worker=self.root/'slow.py';worker.write_text("import sys,time\nsys.stdin.readline()\nprint('{\"type\":\"progress\",\"message\":\"generating\"}',flush=True)\ntime.sleep(60)\n")
        original=subprocess.Popen;children=[];cancel={'value':False};manager=QwenProcess()
        def start(args,**kw):
            args=list(args);args[-1]=str(worker);proc=original(args,**kw);children.append(proc);return proc
        def progress(*_):cancel['value']=True
        try:
            with patch.object(runtime,'readiness',return_value=(True,'')),patch.object(runtime,'root',return_value=self.root),patch.object(runtime,'python_path',return_value=Path(sys.executable)),patch('src.core.synthesis.qwen3_subprocess.subprocess.Popen',side_effect=start):
                with self.assertRaises(_Cancelled):manager.run({'model_path':'test','revision':'1','mode':'clone'},progress,lambda:cancel['value'])
            self.assertIsNotNone(children[0].poll());self.assertFalse(manager.status()['alive']);self.assertFalse(manager.status()['busy'])
        finally:manager.release()
